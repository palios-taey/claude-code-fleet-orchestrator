"""Minimal standalone API for OrchTask and project operations.

Endpoints:
  GET  /api/tasks                  — all pending tasks
  GET  /api/tasks/ranked           — same, LVP-ranked (falls back to priority)
  GET  /api/tasks/{task_id}        — one task's full state
  POST /api/task/create            — create a task
  PATCH /api/task/{task_id}        — update status/owner

Run:
  python3 -m uvicorn fleet_orchestrator.tasks_api:app --host 127.0.0.1 --port 5002
"""
from __future__ import annotations

import hashlib
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

from fleet_orchestrator.config import OrchConfig
from fleet_orchestrator.chat_layer import router as chat_router
from fleet_orchestrator.context_assembler import (
    CORE_BUDGET_BYTES,
    VALID_CLIS,
    assemble as assemble_wake_packet,
    build_packet as build_wake_packet,
    select_context as select_wake_context,
    size_report as wake_size_report,
)
from fleet_orchestrator.decision_receipt import maybe_emit_receipt as maybe_emit_decision_receipt
from fleet_orchestrator.easy_setup import package_version
from fleet_orchestrator.loop_engine import (
    ArtifactNotObservedError,
    ArtifactStore,
    Loop,
    LoopDeclarationError,
    LoopPersistenceError,
    Neo4jCycleStateStore,
    advance_loop_step,
    declare_loop,
    loops_enabled,
)
from fleet_orchestrator.shippability import evaluate_shippability
from fleet_orchestrator.dispatch import bind_current_task, record_outcome
from fleet_orchestrator.orch_schema import (
    CompletionEvidenceError,
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
    init_schema,
    get_neo4j_driver,
    get_project_user_stop_conditions,
    get_session_stop_status,
    get_session_supervised_projects,
    get_session_next_ready,
    get_project_summary,
    get_ready_tasks,
    get_session_current_work,
    list_dashboard_sessions,
    resolve_task_id,
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
from fleet_orchestrator.plan_loader import (
    PlanIdError,
    PlanTerminalStatusError,
    load_plan_from_text,
    plan_declares_refs,
    scope_declared_id,
)
from fleet_orchestrator.orch_schema import TaskIdCollisionError, TaskParentNotFoundError

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
TRUE_ENV_VALUES = {"1", "true", "yes", "on"}


@app.on_event("startup")
def _init_schema_on_startup() -> None:
    result = init_schema(config=_cfg())
    errors = result.get("errors") or []
    if errors:
        raise RuntimeError(f"orchestrator schema initialization failed: {errors}")


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
    cfg = _cfg()
    task_id = resolve_task_id(task_id, config=cfg)  # bare id -> canonical namespaced node
    task = load_task_record(task_id, config=cfg)
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
    initial_status = data.get("initial_status", data.get("status", "pending"))

    cfg = _cfg()
    requested_phase_id = data.get("phase_id")
    phase_id = requested_phase_id if requested_phase_id else ensure_default_project(cfg)
    task_id = f"task-{uuid.uuid4().hex[:8]}"
    try:
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
            initial_status=initial_status,
            config=cfg,
        )
    except CompletionEvidenceError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except TaskParentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except TaskIdCollisionError as exc:
        # Fail-closed (bad/orphan/fused phase_id, or an owned id) -> 409, not a raw 500 (R5 audit:
        # match the /phases + /plan routes which already map this to 4xx).
        raise HTTPException(status_code=409, detail=str(exc))

    return {"ok": True, "task_id": task_id, "from": sender, "owner": owner, "task_type": task_type}


_TERMINAL_STATUSES = {"completed", "failed", "interrupted"}
_REQUEST_TERMINAL_EVIDENCE_KEYS = ("reason", "error", "production_observation")


def _terminal_evidence_from_request(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    evidence = data.get("evidence")
    if evidence is not None:
        return evidence
    lifted = {
        key: data[key]
        for key in _REQUEST_TERMINAL_EVIDENCE_KEYS
        if key in data
    }
    return lifted or None


def _outcome_details(result: str, evidence: Optional[Dict[str, Any]]) -> Optional[str]:
    if result:
        return result
    if not isinstance(evidence, dict):
        return None
    for key in _REQUEST_TERMINAL_EVIDENCE_KEYS:
        value = evidence.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


@app.patch("/api/task/{task_id}")
async def update(task_id: str, req: Request) -> Dict[str, Any]:
    data = await req.json()
    status = data.get("status", "pending")
    sender = data.get("from", "")
    result = data.get("result", "")

    try:
        cfg = _cfg()
        task_id = resolve_task_id(task_id, config=cfg)  # bare id -> canonical namespaced node
        task_before = _load_task(task_id, cfg)
        owner = data.get("owner")
        if owner is None:
            owner = task_before.get("owner", "")
        blocked_on = data["blocked_on"] if "blocked_on" in data else None
        completion_evidence = _terminal_evidence_from_request(data)

        update_task_status(
            task_id,
            status,
            owner=owner,
            result=result,
            blocked_on=blocked_on,
            completion_evidence=completion_evidence,
            completed_by=sender or owner or "",
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
                record_outcome(sender, "done", _outcome_details(result, completion_evidence))
            elif status == "failed":
                record_outcome(sender, "error", _outcome_details(result, completion_evidence))
            elif status == "interrupted":
                record_outcome(sender, "interrupted", _outcome_details(result, completion_evidence))

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
            "completion_evidence": completion_evidence if status in _TERMINAL_STATUSES else task_before.get("completion_evidence"),
            "phase_completed": phase_completed,
        }
    except CompletionEvidenceError as e:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": str(e)},
        )
    except HTTPException:
        raise
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
    summary.update(_project_row(summary.get("project") or {}))
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
    # Caller-supplied phase id MUST go through the same scoping chokepoint as plan ingest (R3 audit
    # CRITICAL: this route fed a bare phase_id straight to create_phase's MERGE). Scope to <project>::<id>
    # + reject a declared '::' / bad charset; the create_phase ownership guard is the second layer.
    try:
        scoped_phase_id = scope_declared_id(project_id, phase_id)
    except PlanIdError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    refs = data.get("refs") if isinstance(data.get("refs"), list) else None
    source_path = _validated_source_path(data.get("source_path"), refs_present=bool(refs))
    try:
        pid = create_phase(
            project_id=project_id,
            phase_id=scoped_phase_id,
            name=name,
            order=int(data.get("order", 0)),
            refs=refs,
            source_path=source_path,
            config=_cfg(),
        )
    except TaskIdCollisionError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
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
    try:
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
    except (PlanIdError, PlanTerminalStatusError) as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except TaskIdCollisionError as exc:        # id owned by another project — refuse adoption
        raise HTTPException(status_code=409, detail=str(exc))


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


@app.get("/api/sessions")
def sessions() -> Dict[str, Any]:
    """Sessions to render as dashboard cards — canonical supervisors from the configured allowlist."""
    return {"sessions": list_dashboard_sessions(config=_cfg())}


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
    maybe_emit_decision_receipt(
        "wake",
        {
            "why_this_context": "session notify endpoint delivered a wake through taey-notify",
            "refs_used": [],
            "rule_tier_applied": "notify",
            "observable_state": {
                "source": "session_notify",
                "target": target,
                "notify_type": notify_type,
                "message_sha256": hashlib.sha256(message.encode("utf-8")).hexdigest(),
            },
            "target": target,
            "next_contract": "fleet-notify daemon delivers the queued message to the target session",
        },
    )
    return {"ok": True}


@app.post("/api/loops/declare")
async def loop_declare(req: Request) -> Dict[str, Any]:
    if not loops_enabled():
        return {"ok": True, "enabled": False}
    data = await req.json()
    raw_loop = data.get("loop") if isinstance(data, dict) and "loop" in data else data
    try:
        return declare_loop(raw_loop, persistence=Neo4jCycleStateStore(config=_cfg()))
    except LoopDeclarationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except LoopPersistenceError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/api/loops/{loop_id}/advance")
async def loop_advance(loop_id: str, req: Request) -> Dict[str, Any]:
    if not loops_enabled():
        return {"ok": True, "enabled": False}
    data = await req.json()
    step_name = str(data.get("step") or "").strip()
    if not step_name:
        raise HTTPException(status_code=400, detail="step is required")
    cfg = _cfg()
    store = Neo4jCycleStateStore(config=cfg)
    raw_loop = store.load(loop_id)
    if raw_loop is None:
        raise HTTPException(status_code=404, detail=f"Loop {loop_id} not found")
    try:
        return advance_loop_step(
            raw_loop,
            step_name,
            artifact_store=ArtifactStore(config=cfg),
            persistence=store,
            wake_target=data.get("wake_target"),
            wake_message=data.get("wake_message"),
        )
    except ArtifactNotObservedError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except LoopDeclarationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except LoopPersistenceError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/api/loops/{loop_id}/should-stop")
def loop_should_stop(loop_id: str) -> Dict[str, Any]:
    if not loops_enabled():
        return {"ok": True, "enabled": False}
    raw_loop = Neo4jCycleStateStore(config=_cfg()).load(loop_id)
    if raw_loop is None:
        raise HTTPException(status_code=404, detail=f"Loop {loop_id} not found")
    try:
        loop = Loop.declare(raw_loop)
    except LoopDeclarationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"ok": True, "enabled": True, "loop_id": loop_id, "should_stop": loop.should_stop()}


@app.get("/api/sessions/{session_id}/wake-packet")
def session_wake_packet(
    session_id: str,
    cli: str = Query("claude"),
    task_id: Optional[str] = Query(None),
    budget_bytes: int = Query(CORE_BUDGET_BYTES, ge=1024, le=128 * 1024),
) -> Dict[str, Any]:
    if os.environ.get("ORCH_WAKE_PACKET_ENABLED", "").strip().lower() not in TRUE_ENV_VALUES:
        return {"ok": True, "enabled": False}

    cli_key = cli.lower().strip()
    if cli_key not in VALID_CLIS:
        raise HTTPException(status_code=400, detail=f"cli must be one of {', '.join(sorted(VALID_CLIS))}")
    if not session_id.strip():
        raise HTTPException(status_code=400, detail="session_id must be non-empty")

    try:
        configured_targets = set(_cfg().session_ids)
        if configured_targets and session_id not in configured_targets:
            raise HTTPException(status_code=400, detail="session_id must be listed in ORCH_SESSION_IDS")
        context = select_wake_context(session_id, task_id=task_id, cli=cli_key)
        packet = build_wake_packet(session_id, context)
        rendered = assemble_wake_packet(packet, cli_key, budget_bytes=budget_bytes)
        report = wake_size_report(rendered, packet, budget_bytes=budget_bytes)
        maybe_emit_decision_receipt(
            "wake_packet_assembly",
            {
                "why_this_context": "wake packet assembled for session wake",
                "context": context,
                "rule_tier_applied": [
                    {"scope": rule.get("scope", ""), "path": rule.get("path", "")}
                    for rule in context.get("rules") or []
                ],
                "observable_state": {
                    "session_id": session_id,
                    "cli": cli_key,
                    "task_id": task_id,
                    "packet_id": packet.get("packet_id", ""),
                    "provenance_hash": packet.get("provenance_hash", ""),
                    "size_report": report,
                    "snapshot": packet.get("snapshot") or {},
                },
                "session": session_id,
                "task_id": task_id,
                "packet_id": packet.get("packet_id", ""),
                "provenance_hash": packet.get("provenance_hash", ""),
                "blocked_on": (packet.get("stop") or {}).get("blocked_on"),
                "next_contract": (packet.get("stop") or {}).get("next_contract"),
            },
        )
        return {
            "ok": True,
            "enabled": True,
            "session_id": session_id,
            "cli": cli_key,
            "packet": rendered,
            "packet_meta": {
                "packet_id": packet.get("packet_id", ""),
                "provenance_hash": packet.get("provenance_hash", ""),
                "generated_at_commit": packet.get("generated_at_commit", ""),
                "snapshot": packet.get("snapshot") or {},
                "size_report": report,
            },
        }
    except HTTPException:
        raise
    except Exception as exc:
        return {
            "ok": False,
            "enabled": True,
            "session_id": session_id,
            "cli": cli_key,
            "error": str(exc),
        }


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
