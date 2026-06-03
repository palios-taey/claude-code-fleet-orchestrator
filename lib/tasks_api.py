"""Minimal standalone API for OrchTask and project operations.

Endpoints:
  GET  /api/tasks                  — all pending tasks
  GET  /api/tasks/ranked           — same, LVP-ranked (falls back to priority)
  GET  /api/tasks/{task_id}        — one task's full state
  POST /api/task/create            — create a task
  PATCH /api/task/{task_id}        — update status/owner

Run:
  python3 -m uvicorn lib.tasks_api:app --host 127.0.0.1 --port 5002
"""
from __future__ import annotations

import json
import os
import sys
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from lib.config import OrchConfig
from lib.chat_layer import router as chat_router
from lib.easy_setup import package_version
from lib.shippability import evaluate_shippability
from lib.dispatch import bind_current_task, record_outcome
from lib.orch_schema import (
    ConditionValidationError,
    PauseValidationError,
    PriorityAuditError,
    ProjectNotFoundError,
    ReadyWorkConflictError,
    add_project_condition,
    assign_task_to_phase,
    check_phase_complete,
    clear_project_stop_reason,
    clear_session_pause,
    complete_project,
    create_phase,
    create_project,
    create_task,
    edit_project_condition,
    ensure_default_project,
    get_agent_tasks,
    get_neo4j_driver,
    get_project_user_stop_conditions,
    get_session_stop_status,
    get_session_supervised_projects,
    get_session_next_ready,
    get_project_summary,
    get_ready_tasks,
    get_session_current_work,
    get_task as load_task_record,
    get_session_stop_decision,
    get_task_phase,
    reset_project,
    set_project_stop_reason,
    set_session_pause,
    set_project_user_stop_conditions,
    update_project_priority,
    update_task_status,
    validate_source_path_for_refs,
)
from lib.plan_loader import load_plan_from_text, plan_declares_refs

app = FastAPI(title="Fleet Orchestrator API", version=package_version())
# SECURITY: chat is an injection vector (posts become content an AI session reads). It is the
# same class as the session-notify endpoint, which already lives on this app. The router stays
# OFF by default so a fresh install on an UNTRUSTED network never exposes it. Operators enable
# it (ORCH_CHAT_ENABLED) only on a trusted/contained network or a loopback-only deployment,
# where the network — not an app-route check — is the security boundary. (Operator decision,
# 2026-06-03: the fleet's internal LAN is contained, no port-forward, so chat is enabled there.)
if os.environ.get("ORCH_CHAT_ENABLED", "").strip().lower() in ("1", "true", "yes"):
    app.include_router(chat_router)
_UI_ROOT = Path(__file__).resolve().parent.parent / "ui"
ALLOWED_NOTIFY_TYPES = {
    "standard": "message",
    "escalation": "escalation",
    "command": "command",
    "response_ready": "response_ready",
}


def _cfg() -> OrchConfig:
    return OrchConfig()


def _decode_json(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


def _project_counts(project_id: str) -> Dict[str, int]:
    summary = get_project_summary(project_id, config=_cfg())
    phases = summary.get("phases", []) if summary else []
    counts = {"phase_count": len(phases), "task_total": 0, "pending": 0, "in_progress": 0, "completed": 0, "failed": 0}
    for phase in phases:
        task_counts = phase.get("task_counts", {})
        counts["task_total"] += int(task_counts.get("total", 0) or 0)
        counts["pending"] += int(task_counts.get("pending", 0) or 0)
        counts["in_progress"] += int(task_counts.get("in_progress", 0) or 0)
        counts["completed"] += int(task_counts.get("completed", 0) or 0)
        counts["failed"] += int(task_counts.get("failed", 0) or 0)
    return counts


def _project_row(project: Dict[str, Any]) -> Dict[str, Any]:
    counts = _project_counts(project["id"])
    return {
        "id": project.get("id"),
        "name": project.get("name"),
        "description": project.get("description"),
        "status": project.get("status"),
        "source_path": project.get("source_path"),
        "source_kind": project.get("source_kind"),
        "source_sha256": project.get("source_sha256"),
        "user_stop_conditions": project.get("user_stop_conditions", []),
        "supervisor": project.get("supervisor"),
        "priority": project.get("priority"),
        "migration_exempt": bool(project.get("migration_exempt")),
        "stop_reason_current": project.get("stop_reason_current"),
        "stop_reason_history": project.get("stop_reason_history", []),
        "priority_history": project.get("priority_history", []),
        "stop_reason_orphaned": bool(project.get("stop_reason_orphaned")),
        **counts,
    }


def _strict_force_flag(data: Dict[str, Any]) -> bool:
    if "force" not in data:
        return False
    value = data["force"]
    if isinstance(value, bool):
        return value
    raise HTTPException(status_code=422, detail="force must be a JSON boolean")


def _validated_source_path(source_path: Optional[str], refs_present: bool) -> Optional[str]:
    normalized, error = validate_source_path_for_refs(source_path, refs_present=refs_present)
    if error:
        raise HTTPException(status_code=422, detail=error)
    return normalized


@app.get("/api/tasks")
def list_tasks() -> Dict[str, Any]:
    tasks = get_ready_tasks(_cfg())
    return {"tasks": tasks}


@app.get("/api/tasks/ranked")
def list_ranked() -> Dict[str, Any]:
    tasks = get_ready_tasks(_cfg())
    # Sort by priority ASC (lowest number = highest priority per Jesse convention).
    # Schema helper already does this; this is belt-and-suspenders for any path that
    # bypasses the helper. Missing priority sorts last via large default.
    tasks.sort(key=lambda t: t.get("priority") if t.get("priority") is not None else 999999999)
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
    # Horizon v1.3.0 full audit amendment #3: refuse negative priority values.
    # A prior migration script wrote priority = -<unix_timestamp> on 33 projects
    # (data-corruption fix applied as one-off Cypher at 2026-05-31 01:51Z). This
    # guard prevents any future write of negative-epoch-style values via the API.
    if priority < 0:
        raise HTTPException(
            status_code=400,
            detail=f"priority must be >= 0 (got {priority}). Negative values were a 2026-05 migration artifact and are no longer accepted.",
        )
    sender = data.get("from", "unknown")
    # If owner not explicitly set, default to creator so the task is not orphaned.
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
        owner=owner,
        created_by=sender,
        task_type=task_type,
        capability_tags=capability_tags,
        file_blast_radius=file_blast_radius,
        estimated_tokens=estimated_tokens,
        config=cfg,
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
    """List all OrchProject nodes with decoded Stage A fields and aggregate task status counts."""
    cfg = _cfg()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        result = session.run("MATCH (p:OrchProject) RETURN p ORDER BY p.id")
        projects = []
        for record in result:
            project = _serialize_node(record["p"])
            project["user_stop_conditions"] = _decode_json(project.get("user_stop_conditions"), [])
            project["stop_reason_current"] = _decode_json(project.get("stop_reason_current"), None)
            project["stop_reason_history"] = _decode_json(project.get("stop_reason_history"), [])
            project["priority_history"] = _decode_json(project.get("priority_history"), [])
            project["stop_reason_orphaned"] = False
            projects.append(_project_row(project))
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
    supervisor = (data.get("supervisor") or "").strip()
    if not supervisor or supervisor == "unassigned":
        raise HTTPException(status_code=400, detail="supervisor must be non-empty and not 'unassigned'")
    # Horizon v1.3.0 full audit amendment #3: refuse negative project priority.
    project_priority = data.get("priority")
    if project_priority is not None and int(project_priority) < 0:
        raise HTTPException(
            status_code=400,
            detail=f"priority must be >= 0 (got {project_priority}). Negative values were a 2026-05 migration artifact and are no longer accepted.",
        )
    refs = data.get("refs") if isinstance(data.get("refs"), list) else None
    source_path = _validated_source_path(data.get("source_path"), refs_present=bool(refs))
    pid = create_project(
        project_id=project_id,
        name=name,
        description=data.get("description", ""),
        refs=refs,
        user_stop_conditions=data.get("user_stop_conditions"),
        supervisor=supervisor,
        priority=project_priority,
        source_path=source_path,
        source_sha256=data.get("source_sha256"),
        source_kind=data.get("source_kind"),
        ingested_by=data.get("ingested_by"),
        config=_cfg(),
    )
    return {"ok": True, "project_id": pid}


@app.get("/api/projects/{project_id}/user-stop-conditions")
def get_project_user_stop_conditions_endpoint(project_id: str) -> Dict[str, Any]:
    """Backward-compatible surface returning active condition labels only."""
    conditions = get_project_user_stop_conditions(project_id, config=_cfg())
    if conditions is None:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    return {"project_id": project_id, "conditions": [condition["label"] for condition in conditions if not condition.get("deprecated_at")]}


@app.post("/api/projects/{project_id}/user-stop-conditions")
async def set_project_user_stop_conditions_endpoint(project_id: str, req: Request) -> Dict[str, Any]:
    data = await req.json()
    conditions = data.get("conditions")
    if not isinstance(conditions, list) or any(not isinstance(item, str) for item in conditions):
        raise HTTPException(status_code=400, detail="conditions must be a list of strings")
    saved = set_project_user_stop_conditions(
        project_id,
        conditions,
        config=_cfg(),
        created_by=data.get("from", "legacy-api"),
    )
    return {"ok": True, "project_id": project_id, "conditions": [condition["label"] for condition in saved if not condition.get("deprecated_at")]}


@app.post("/api/projects/{project_id}/phases")
async def create_phase_endpoint(project_id: str, req: Request) -> Dict[str, Any]:
    """Create an OrchPhase under a project."""
    data = await req.json()
    phase_id = data.get("id")
    name = data.get("name", phase_id)
    if not phase_id:
        raise HTTPException(status_code=400, detail="id required")
    refs = data.get("refs") if isinstance(data.get("refs"), list) else None
    source_path = _validated_source_path(data.get("source_path"), refs_present=bool(refs))
    pid = create_phase(
        project_id=project_id,
        phase_id=phase_id,
        name=name,
        order=int(data.get("order", 0)),
        refs=refs,
        source_path=source_path,
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
    supervisor = (data.get("supervisor") or "").strip()
    if supervisor in {"", "unassigned", "unknown"}:
        raise HTTPException(status_code=400, detail="supervisor required for non-exempt project ingest (must not be unassigned or unknown)")
    # Horizon v1.3.0 full audit amendment #3: refuse negative project priority on plan ingest.
    ingest_priority = data.get("priority")
    if ingest_priority is not None and int(ingest_priority) < 0:
        raise HTTPException(
            status_code=400,
            detail=f"priority must be >= 0 (got {ingest_priority}). Negative values were a 2026-05 migration artifact and are no longer accepted.",
        )
    refs_present = plan_declares_refs(md_text)
    source_path = _validated_source_path(data.get("source_path", ""), refs_present=refs_present)
    return load_plan_from_text(
        md=md_text,
        source_path=source_path or "",
        source_kind=data.get("source_kind", "markdown"),
        ingested_by=data.get("ingested_by", "unknown"),
        supervisor=supervisor,
        priority=ingest_priority,
        migration_exempt=bool(data.get("migration_exempt", False)),
    )


@app.post("/api/projects/{project_id}/complete")
async def complete_project_endpoint(project_id: str, req: Request) -> Dict[str, Any]:
    data = await req.json() if req.headers.get("content-type", "").startswith("application/json") else {}
    if not isinstance(data, dict):
        raise HTTPException(status_code=422, detail="request body must be a JSON object")
    try:
        return complete_project(
            project_id,
            force=_strict_force_flag(data),
            completed_by=data.get("completed_by") or data.get("from") or "unknown",
            config=_cfg(),
        )
    except ReadyWorkConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/api/projects/{project_id}/shippability")
async def project_shippability_endpoint(project_id: str) -> Dict[str, Any]:
    """Ship-gate verdict: shippable only when every -prodtest/-audit gate is completed."""
    return evaluate_shippability(project_id, config=_cfg())


@app.post("/api/projects/{project_id}/ship")
async def ship_project_endpoint(project_id: str) -> Dict[str, Any]:
    """ENGINE SHIP GATE (rp0): refuse the ship transition unless all ship-gates are
    completed with evidence. No human-approval override — the gates are the authority."""
    verdict = evaluate_shippability(project_id, config=_cfg())
    if not verdict.get("shippable"):
        raise HTTPException(status_code=409, detail=verdict)
    return {"ok": True, "shippable": True, "verdict": verdict}


@app.post("/api/projects/{project_id}/reset")
async def reset_project_endpoint(project_id: str, req: Request) -> Dict[str, Any]:
    data = await req.json() if req.headers.get("content-type", "").startswith("application/json") else {}
    try:
        return reset_project(
            project_id,
            reset_by=data.get("reset_by") or data.get("from") or "unknown",
            config=_cfg(),
        )
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))


@app.get("/api/sessions/{session_id}/current")
def session_current(session_id: str) -> Dict[str, Any]:
    """What this session is currently executing — top in_progress task with project/phase context."""
    work = get_session_current_work(session_id, config=_cfg())
    if not work:
        return {"session": session_id, "current": None}
    return {"session": session_id, "current": work}


@app.get("/api/sessions/{session_id}/next-ready")
def session_next_ready(session_id: str) -> Dict[str, Any]:
    """Top pending task owned-by this session only — under single-supervisor scope there is no claim-from-unowned-pool path."""
    result = get_session_next_ready(session_id, config=_cfg())
    if not result:
        return {"session": session_id, "next": None}
    return {"session": session_id, "next": result}


@app.get("/api/sessions/{session_id}/projects")
def session_projects(session_id: str) -> Dict[str, Any]:
    """Supervisor-based listing; replaces the earlier task-owner-based semantics."""
    projects = [_project_row(project) for project in get_session_supervised_projects(session_id, config=_cfg())]
    return {"session": session_id, "projects": projects}


@app.get("/api/sessions/{session_id}/stop-status")
def session_stop_status(session_id: str) -> Dict[str, Any]:
    result = get_session_stop_status(session_id, config=_cfg())
    return {"session": session_id, **result}


@app.get("/api/sessions/{session_id}/stop-decision")
def session_stop_decision(session_id: str, stop_hook_active: bool = Query(default=False)) -> Dict[str, Any]:
    result = get_session_stop_decision(session_id, stop_hook_active=stop_hook_active, config=_cfg())
    return {"session": session_id, **result}


@app.post("/api/projects/{project_id}/stop-reason")
async def set_project_stop_reason_endpoint(project_id: str, req: Request) -> Dict[str, Any]:
    data = await req.json()
    try:
        current = set_project_stop_reason(
            project_id,
            condition_id=str(data.get("condition_id") or ""),
            condition_version=int(data.get("condition_version")),
            detail=data.get("detail", ""),
            set_by=data.get("set_by") or data.get("from") or "unknown",
            config=_cfg(),
        )
    except ReadyWorkConflictError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except (ConditionValidationError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {
        "ok": True,
        "cited_condition_label": current.get("label_snapshot"),
        "history_id": f"{project_id}:{current.get('condition_id')}:{current.get('condition_version')}",
    }


@app.delete("/api/projects/{project_id}/stop-reason")
async def clear_project_stop_reason_endpoint(project_id: str, req: Request) -> Dict[str, Any]:
    data = await req.json() if req.headers.get("content-type", "").startswith("application/json") else {}
    try:
        ok = clear_project_stop_reason(project_id, cleared_by=data.get("cleared_by") or data.get("from") or "unknown", config=_cfg())
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"ok": ok}


@app.patch("/api/projects/{project_id}")
async def patch_project_endpoint(project_id: str, req: Request) -> Dict[str, Any]:
    data = await req.json()
    if "priority" not in data:
        raise HTTPException(status_code=400, detail="priority required")
    try:
        updated = update_project_priority(
            project_id,
            int(data["priority"]),
            set_by=data.get("set_by") or data.get("from") or "",
            source_surface=data.get("source_surface") or "",
            reason=data.get("reason") or "",
            config=_cfg(),
        )
    except PriorityAuditError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"ok": True, "project_id": project_id, **updated}


@app.post("/api/projects/{project_id}/conditions")
async def add_project_condition_endpoint(project_id: str, req: Request) -> Dict[str, Any]:
    data = await req.json()
    label = (data.get("label") or "").strip()
    if not label:
        raise HTTPException(status_code=400, detail="label required")
    try:
        condition = add_project_condition(project_id, label, created_by=data.get("created_by") or data.get("from") or "unknown", config=_cfg())
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"ok": True, "condition": condition}


@app.patch("/api/projects/{project_id}/conditions/{condition_id}")
async def edit_project_condition_endpoint(project_id: str, condition_id: str, req: Request) -> Dict[str, Any]:
    data = await req.json()
    label = (data.get("label") or "").strip()
    if not label:
        raise HTTPException(status_code=400, detail="label required")
    try:
        condition = edit_project_condition(project_id, condition_id, label, edited_by=data.get("edited_by") or data.get("from") or "unknown", config=_cfg())
    except ConditionValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except ProjectNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    return {"ok": True, "condition": condition}


@app.post("/api/sessions/{session_id}/pause")
async def pause_session_endpoint(session_id: str, req: Request) -> Dict[str, Any]:
    data = await req.json()
    try:
        meta = set_session_pause(
            session_id,
            pause_source=data.get("pause_source") or "",
            pause_reason=data.get("pause_reason") or "",
            pause_expires_at=data.get("pause_expires_at"),
            paused_by=data.get("paused_by") or data.get("from") or session_id,
            config=_cfg(),
        )
    except PauseValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "pause_meta": meta}


@app.delete("/api/sessions/{session_id}/pause")
async def clear_pause_session_endpoint(session_id: str, req: Request) -> Dict[str, Any]:
    data = await req.json() if req.headers.get("content-type", "").startswith("application/json") else {}
    meta = clear_session_pause(session_id, cleared_by=data.get("cleared_by") or data.get("from") or session_id, config=_cfg())
    return {"ok": True, "pause_meta": meta}


@app.post("/api/sessions/{target}/notify")
async def session_notify(target: str, req: Request) -> Dict[str, Any]:
    configured_targets = set(_cfg().session_ids)
    if configured_targets and target not in configured_targets:
        raise HTTPException(status_code=400, detail="target must be listed in ORCH_SESSION_IDS")
    if not target.strip():
        raise HTTPException(status_code=400, detail="target must be non-empty")

    data = await req.json()
    notify_type = data.get("type", "standard")
    message = (data.get("message") or "").strip()

    if notify_type not in ALLOWED_NOTIFY_TYPES:
        raise HTTPException(status_code=400, detail="type must be one of standard, escalation, command, response_ready")
    if not message:
        raise HTTPException(status_code=400, detail="message must be non-empty")

    result = subprocess.run(
        ["taey-notify", target, message, "--type", ALLOWED_NOTIFY_TYPES[notify_type]],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise HTTPException(
            status_code=502,
            detail=result.stderr.strip() or "taey-notify failed",
        )
    return {"ok": True}


@app.get("/health")
def health() -> Dict[str, Any]:
    try:
        _ = get_ready_tasks(_cfg())
        return {
            "ok": True,
            "service": "fleet-orchestrator-api",
            "version": package_version(),
            "api_base": os.environ.get("ORCH_API_BASE", "http://127.0.0.1:5002"),
            "ts": time.time(),
        }
    except Exception as e:
        return JSONResponse(status_code=503, content={"ok": False, "error": str(e)})


@app.api_route("/", methods=["GET", "HEAD"], include_in_schema=False)
def root_redirect() -> RedirectResponse:
    return RedirectResponse(url="/ui/", status_code=302)


class _RevalidateStatic(StaticFiles):
    """Serve UI assets with Cache-Control: no-cache so the browser always
    revalidates (cheap 304 via ETag when unchanged, fresh 200 when updated).
    Without this the dashboard can render a stale CSS/JS after an update."""

    async def get_response(self, path, scope):
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


app.mount("/ui/static", _RevalidateStatic(directory=_UI_ROOT / "static"), name="ui-static")
app.mount("/ui", _RevalidateStatic(directory=_UI_ROOT, html=True), name="ui")
