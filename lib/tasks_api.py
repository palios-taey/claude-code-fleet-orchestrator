"""Minimal OrchTask API — standalone service on port 5002.

Separate from the Taey dashboard (infra-soul v3.1 on :5001). This service
only exposes the endpoints the taey-task CLI calls, backed by the same
Neo4j OrchTask nodes.

Endpoints:
  GET  /api/tasks                  — all pending tasks
  GET  /api/tasks/ranked           — same, LVP-ranked (falls back to priority)
  GET  /api/tasks/{task_id}        — one task's full state
  POST /api/task/create            — create a task
  PATCH /api/task/{task_id}        — update status/owner

Run:
  python3 -m uvicorn conductor.tasks_api:app --host 0.0.0.0 --port 5002
"""
from __future__ import annotations

import sys
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.config import OrchConfig
from lib.dispatch import bind_current_task, record_outcome
from lib.orch_schema import (
    assign_task_to_phase,
    check_phase_complete,
    create_phase,
    create_project,
    create_task,
    ensure_default_project,
    get_agent_tasks,
    get_neo4j_driver,
    get_session_next_ready,
    get_project_summary,
    get_ready_tasks,
    get_session_current_work,
    get_task as load_task_record,
    get_task_phase,
    update_task_status,
)
from lib.plan_loader import load_plan_from_text

app = FastAPI(title="Conductor Tasks API", version="2.0")


def _cfg() -> OrchConfig:
    return OrchConfig()


@app.get("/api/tasks")
def list_tasks() -> Dict[str, Any]:
    tasks = get_ready_tasks(_cfg())
    return {"tasks": tasks}


@app.get("/api/tasks/ranked")
def list_ranked() -> Dict[str, Any]:
    tasks = get_ready_tasks(_cfg())
    # Sort by priority desc (schema helper already does this, but belt-and-suspenders)
    tasks.sort(key=lambda t: t.get("priority", 0), reverse=True)
    # Normalize field name the CLI expects
    for t in tasks:
        t["task_id"] = t.get("id", t.get("task_id"))
    return {"tasks": tasks}


def _coerce_neo4j_value(v: Any) -> Any:
    """Convert neo4j temporal types to ISO strings for JSON serialization."""
    # neo4j.time.DateTime / Date / Time all expose .iso_format()
    iso = getattr(v, "iso_format", None)
    if callable(iso):
        return iso()
    return v


def _serialize_node(node: Any) -> Dict[str, Any]:
    return {k: _coerce_neo4j_value(v) for k, v in dict(node).items()}


def _load_task(task_id: str, cfg: OrchConfig) -> Dict[str, Any]:
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        result = session.run(
            "MATCH (t:OrchTask {id: $tid}) RETURN t",
            tid=task_id,
        ).single()
    if not result:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return _serialize_node(result["t"])


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str) -> Dict[str, Any]:
    task = load_task_record(task_id, config=_cfg())
    if not task:
        raise HTTPException(status_code=404, detail=f"Task {task_id} not found")
    return task


@app.post("/api/task/create")
async def create(req: Request) -> Dict[str, Any]:
    data = await req.json()
    description = data.get("description") or data.get("title")
    if not description:
        raise HTTPException(status_code=400, detail="description required")

    priority = int(data.get("priority", 50))
    sender = data.get("from", "unknown")
    # If owner not explicitly set, default to creator. Without this default,
    # tasks land with owner='' and the watchloop surfaces them to every session
    # (the unassigned-pending fallback only conductor sees), so nobody owns the
    # work — orphans accumulate. Explicit `owner` in the request body still
    # wins for cross-session task creation (e.g. weaver creating a task for
    # codex-1 to do).
    owner = data.get("owner") or sender
    task_type = data.get("task_type", "standard")
    capability_tags = data.get("capability_tags", [])
    file_blast_radius = data.get("file_blast_radius", [])
    estimated_tokens = int(data.get("estimated_tokens", 50_000))

    cfg = _cfg()
    requested_phase_id = data.get("phase_id")
    phase_id = requested_phase_id if requested_phase_id else ensure_default_project(cfg)
    task_id = f"task-{uuid.uuid4().hex[:8]}"
    create_task(
        phase_id=phase_id,
        task_id=task_id,
        description=description,
        priority=priority,
        capability_tags=capability_tags,
        file_blast_radius=file_blast_radius,
        estimated_tokens=estimated_tokens,
        config=cfg,
    )

    # Stamp sender + owner on the node so the watchloop / UI can route correctly.
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        session.run(
            "MATCH (t:OrchTask {id: $tid}) SET t.created_by = $sender, t.owner = $owner, t.task_type = $ttype",
            tid=task_id, sender=sender, owner=owner, ttype=task_type,
        )

    return {"ok": True, "task_id": task_id, "from": sender, "owner": owner, "task_type": task_type}


@app.patch("/api/task/{task_id}")
async def update(task_id: str, req: Request) -> Dict[str, Any]:
    data = await req.json()
    status = data.get("status", "pending")
    sender = data.get("from", "")
    result = data.get("result", "")

    try:
        cfg = _cfg()
        task_before = _load_task(task_id, cfg)
        owner = data.get("owner")
        if owner is None:
            owner = task_before.get("owner", "")
        blocked_on = data["blocked_on"] if "blocked_on" in data else None

        update_task_status(
            task_id,
            status,
            owner=owner,
            result=result,
            blocked_on=blocked_on,
            config=cfg,
        )

        if sender and owner == sender:
            if status == "in_progress":
                bind_current_task(
                    worker=sender,
                    task_id=task_id,
                    description=task_before.get("description", ""),
                    supervisor=sender,
                    set_parent=True,
                )
            elif status == "completed":
                record_outcome(sender, "done", result or None)
            elif status == "failed":
                record_outcome(sender, "error", result or None)
            elif status == "interrupted":
                record_outcome(sender, "interrupted", result or None)

        # Transitive completion: if task finished, check if its parent phase is now done.
        phase_completed = False
        if status == "completed":
            phase_id = get_task_phase(task_id, config=cfg)
            if phase_id:
                phase_completed = check_phase_complete(phase_id, config=cfg)

        return {
            "ok": True,
            "task_id": task_id,
            "status": status,
            "owner": owner,
            "blocked_on": blocked_on if blocked_on is not None else task_before.get("blocked_on"),
            "phase_completed": phase_completed,
        }
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(e)},
        )


@app.get("/api/projects")
def list_projects() -> Dict[str, Any]:
    """List all OrchProject nodes with phase counts and aggregate task status counts."""
    cfg = _cfg()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        result = session.run("""
            MATCH (p:OrchProject)
            OPTIONAL MATCH (p)-[:HAS_PHASE]->(ph:OrchPhase)
            OPTIONAL MATCH (ph)-[:HAS_TASK]->(t:OrchTask)
            WITH p, count(DISTINCT ph) AS phase_count,
                 count(DISTINCT t) AS task_total,
                 count(DISTINCT CASE WHEN t.status = 'pending' THEN t END) AS pending,
                 count(DISTINCT CASE WHEN t.status = 'in_progress' THEN t END) AS in_progress,
                 count(DISTINCT CASE WHEN t.status = 'completed' THEN t END) AS completed,
                 count(DISTINCT CASE WHEN t.status = 'failed' THEN t END) AS failed
            RETURN p.id AS id, p.name AS name, p.description AS description,
                   p.status AS status, p.source_path AS source_path,
                   p.source_kind AS source_kind, p.source_sha256 AS source_sha256,
                   phase_count, task_total, pending, in_progress, completed, failed
            ORDER BY p.id
        """)
        projects = [dict(r) for r in result]
    return {"projects": projects}


@app.get("/api/projects/{project_id}")
def get_project(project_id: str) -> Dict[str, Any]:
    """Project summary with phase task counts."""
    summary = get_project_summary(project_id, config=_cfg())
    if not summary:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    return summary


@app.post("/api/projects")
async def create_project_endpoint(req: Request) -> Dict[str, Any]:
    """Create an OrchProject."""
    data = await req.json()
    project_id = data.get("id")
    name = data.get("name", project_id)
    if not project_id:
        raise HTTPException(status_code=400, detail="id required")
    pid = create_project(
        project_id=project_id,
        name=name,
        description=data.get("description", ""),
        source_path=data.get("source_path"),
        source_sha256=data.get("source_sha256"),
        source_kind=data.get("source_kind"),
        ingested_by=data.get("ingested_by"),
        config=_cfg(),
    )
    return {"ok": True, "project_id": pid}


@app.post("/api/projects/{project_id}/phases")
async def create_phase_endpoint(project_id: str, req: Request) -> Dict[str, Any]:
    """Create an OrchPhase under a project."""
    data = await req.json()
    phase_id = data.get("id")
    name = data.get("name", phase_id)
    if not phase_id:
        raise HTTPException(status_code=400, detail="id required")
    pid = create_phase(
        project_id=project_id,
        phase_id=phase_id,
        name=name,
        order=int(data.get("order", 0)),
        config=_cfg(),
    )
    return {"ok": True, "phase_id": pid}


@app.post("/api/projects/load-md")
async def load_plan_md(req: Request) -> Dict[str, Any]:
    """Ingest a markdown plan into Neo4j as OrchProject/Phase/Task nodes."""
    data = await req.json()
    md_text = data.get("md_text")
    if not md_text:
        raise HTTPException(status_code=400, detail="md_text required")
    return load_plan_from_text(
        md=md_text,
        source_path=data.get("source_path", ""),
        source_kind=data.get("source_kind", "markdown"),
        ingested_by=data.get("ingested_by", "unknown"),
    )


@app.get("/api/sessions/{session_id}/current")
def session_current(session_id: str) -> Dict[str, Any]:
    """What this session is currently executing — top in_progress task with project/phase context."""
    work = get_session_current_work(session_id, config=_cfg())
    if not work:
        return {"session": session_id, "current": None}
    return {"session": session_id, "current": work}


@app.get("/api/sessions/{session_id}/next-ready")
def session_next_ready(session_id: str) -> Dict[str, Any]:
    """Top pending task owned-by this session OR unowned-and-team-matched."""
    result = get_session_next_ready(session_id, config=_cfg())
    if not result:
        return {"session": session_id, "next": None}
    return {"session": session_id, "next": result}


@app.get("/health")
def health() -> Dict[str, Any]:
    try:
        _ = get_ready_tasks(_cfg())
        return {"ok": True, "service": "conductor-tasks-api", "ts": time.time()}
    except Exception as e:
        return JSONResponse(status_code=503, content={"ok": False, "error": str(e)})
