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

import datetime as dt
import hmac
import hashlib
import json
import logging
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

from fleet_orchestrator.config import OrchConfig, get_redis_sync
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
from fleet_orchestrator.easy_setup import api_host
from fleet_orchestrator.version import __version__ as RUNNING_VERSION
from fleet_orchestrator.evidence_contract import REQUEST_TERMINAL_EVIDENCE_KEYS, TERMINAL_STATUSES
from fleet_orchestrator.feature_flags import TRUE_ENV_VALUES, chat_enabled, wake_packet_endpoint_enabled
from fleet_orchestrator.handoff_validation import ensure_handoff_index_backfilled
from fleet_orchestrator.notify_state import redis_connect as notify_redis_connect
from fleet_orchestrator.loop_engine import (
    ArtifactNotObservedError,
    ArtifactStore,
    Loop,
    LoopDeclarationError,
    LoopPersistenceError,
    Neo4jCycleStateStore,
    advance_loop_step,
    declare_loop,
    disabled_loop_response,
    loops_enabled,
)
from fleet_orchestrator.shippability import evaluate_shippability
from fleet_orchestrator.dispatch import (
    BugLockActive,
    HooksNotInstalled,
    OrchTaskNotReady,
    WorkerBusy,
    bind_current_task,
    dispatch as dispatch_task,
    record_outcome,
)
from fleet_orchestrator.orch_schema import (
    CompletionEvidenceError,
    ConditionValidationError,
    PauseValidationError,
    PriorityAuditError,
    ProjectNotFoundError,
    ReadyWorkConflictError,
    add_project_condition,
    assign_task_to_phase,
    answer_question,
    check_phase_complete,
    clear_project_stop_reason,
    clear_session_pause,
    complete_project,
    complete_human_review_gate,
    create_human_review_gate,
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
    get_session_liveness,
    list_dashboard_sessions,
    resolve_task_id,
    get_task as load_task_record,
    get_session_stop_decision,
    get_task_phase,
    reset_project,
    supervisor_access_resolution,
    session_registration_error_detail,
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

LOGGER = logging.getLogger("uvicorn.error")

AUTH_FAILURE_DETAIL = (
    "invalid or missing API credential. Send `Authorization: Bearer $ORCH_AUTH_TOKEN` "
    "or `X-API-Key: $ORCH_AUTH_TOKEN`. Local loopback/no-token mode is only for local "
    "trusted runs when ORCH_AUTH_TOKEN is unset."
)
COMPLETED_TASK_NEXT_STEP = (
    'Use `taey-task update %TASK_ID% completed --evidence '
    '\'{"commit_sha":"<sha>","production_observation":"<what you verified>"}\'` '
    'or PATCH /api/task/%TASK_ID% with body '
    '{"status":"completed","evidence":{"commit_sha":"<sha>",'
    '"production_observation":"<what you verified>"}}.'
)
FAILED_TASK_NEXT_STEP = (
    'Use `taey-task update %TASK_ID% %STATUS% --evidence \'{"reason":"<why>"}\'` '
    'or PATCH /api/task/%TASK_ID% with body '
    '{"status":"%STATUS%","evidence":{"reason":"<why>"}}.'
)
PROJECT_FORCE_COMPLETE_BODY = '{"force":true,"closure_reason":"<why>","completed_by":"<session-id>"}'
HUMAN_GATE_CREATE_BODY = (
    '{"phase_id":"<phase-id>","task_id":"<task-id>","prompt":"<review question>",'
    '"reviewer":"<session-id>"}'
)
LOOP_DECLARATION_NEXT_STEP = (
    'Declare a loop with POST /api/loops/declare body {"id":"<loop-id>",'
    '"owner":"<session-id>","step_bundle":[{"name":"<step>","definition":"<what to do>"}],'
    '"trigger":{"kind":"manual"},"cycle_state":{"current_step":"<step>"},'
    '"stop_condition":{"kind":"manual","description":"<when to stop>"}}.'
)
TASK_CREATE_NEXT_STEP = (
    'Retry POST /api/task/create body {"description":"<task description>",'
    '"from":"<session-id>","phase_id":"<phase-id>"}; use `taey-task create '
    "'<task description>'` for default-project tasks."
)
PROJECT_CREATE_NEXT_STEP = (
    'Retry POST /api/projects body {"id":"<project-id>","name":"<name>",'
    '"supervisor":"<session-id>","priority":0}; or ingest markdown with '
    "`taey-plan ingest <plan.md> --supervisor <session-id>`."
)
PROJECT_USER_STOP_CONDITIONS_NEXT_STEP = (
    'Use POST /api/projects/{project_id}/user-stop-conditions body '
    '{"conditions":["<stop condition>"]}; inspect with GET '
    "/api/projects/{project_id}/user-stop-conditions."
)
PHASE_CREATE_NEXT_STEP = (
    'Retry POST /api/projects/{project_id}/phases body {"id":"<plain-phase-id>",'
    '"name":"<phase name>","order":0}; declared ids must be plain, not '
    "project::phase."
)
PLAN_LOAD_NEXT_STEP = (
    'Retry POST /api/projects/load-md body {"md_text":"# Project: <project-id> - '
    '<name>\\n...","supervisor":"<session-id>","priority":0}; or run '
    "`taey-plan ingest <plan.md> --supervisor <session-id>`."
)
PROJECT_STOP_REASON_NEXT_STEP = (
    "Use `taey-stop-reason set <project-id> --condition <label> --detail <why> "
    '--session <session-id>` or POST /api/projects/{project_id}/stop-reason body '
    '{"condition_id":"<id>","condition_version":1,"detail":"<why>",'
    '"set_by":"<session-id>"}; inspect stop conditions with '
    "`taey-plan stop-conditions <project-id> get`."
)
PROJECT_CONDITION_EDIT_NEXT_STEP = (
    'Use PATCH /api/projects/{project_id}/conditions/{condition_id} body '
    '{"label":"<replacement stop condition label>","edited_by":"<session-id>"}; '
    "inspect condition ids with `taey-plan stop-conditions <project-id> get`."
)
LOOP_LOOKUP_NEXT_STEP = (
    'Declare a loop with POST /api/loops/declare body {"id":"<loop-id>",...}; '
    "then retry the loop endpoint with that loop_id."
)
LOOP_ADVANCE_NEXT_STEP = (
    'Record the required artifact for the loop step, then retry POST '
    '/api/loops/{loop_id}/advance body {"step":"<step name>"}; inspect loop state '
    "with GET /api/loops/{loop_id}/should-stop."
)
WAKE_PACKET_CLI_NEXT_STEP = (
    "Use GET /api/sessions/{session_id}/wake-packet?cli=claude or "
    "GET /api/sessions/{session_id}/wake-packet?cli=codex."
)


def _terminal_evidence_next_step(task_id: str, status: str) -> str:
    if status == "completed":
        return COMPLETED_TASK_NEXT_STEP.replace("%TASK_ID%", task_id)
    if status in {"failed", "interrupted"}:
        return FAILED_TASK_NEXT_STEP.replace("%TASK_ID%", task_id).replace("%STATUS%", status)
    return (
        f"Set status to completed, failed, or interrupted with matching evidence; for completed, "
        f"{COMPLETED_TASK_NEXT_STEP.replace('%TASK_ID%', task_id)}"
    )


def _task_update_body_next_step(task_id: str) -> str:
    return (
        f"Send PATCH /api/task/{task_id} with a JSON object body, for example "
        '{"status":"in_progress","from":"<session-id>"}; terminal statuses require evidence.'
    )


def _human_gate_next_step(question_id: str = "{question_id}") -> str:
    return (
        f"Create with POST /api/human-review-gates body {HUMAN_GATE_CREATE_BODY}; "
        f"answer ordinary questions with POST /api/questions/{question_id}/answer body "
        '{"answer":"<answer text>","answered_by":"<session-id>"}; '
        f"complete human-review gates through `/ui/` or POST /api/ui/questions/{question_id}/answer body "
        '{"answer":"<verdict>","answered_by":"<reviewer>"}.'
    )


def _required_body_detail(field: str, body: Dict[str, Any], *, endpoint: str,
                          command: Optional[str] = None) -> str:
    detail = (
        f"{field} is required. Minimal accepted JSON body: "
        f"{json.dumps(body, separators=(',', ':'), sort_keys=True)}. Endpoint: {endpoint}."
    )
    if command:
        detail = f"{detail} CLI: {command}."
    return detail


def _non_empty_path_detail(field: str, *, endpoint: str, example: str) -> str:
    return f"{field} must be non-empty. Use {endpoint}; example: {example}."


def _task_not_found_detail(task_id: str) -> str:
    return (
        f"Task {task_id} not found. List candidate tasks with `taey-task list` "
        "or `GET /api/tasks`; inspect a known task with "
        f"`taey-task status {task_id}` or `GET /api/tasks/{task_id}`; inspect "
        "project context with `taey-plan show <project-id>`."
    )


def _project_not_found_detail(project_id: str) -> str:
    return (
        f"Project {project_id} not found. List valid projects with "
        "`taey-plan list` or `GET /api/projects`; inspect a known project with "
        f"`taey-plan show {project_id}` or `GET /api/projects/{project_id}`."
    )


def _question_not_found_detail(question_id: str, *, endpoint: str, ui: bool = False) -> str:
    surface = "dashboard `/ui/` human-review action" if ui else "owning project or task context"
    return (
        f"Question {question_id} not found. Inspect the {surface}, or inspect "
        "candidate projects with `taey-plan list` / `GET /api/projects` and "
        "`taey-plan show <project-id>` / `GET /api/projects/{project_id}`. "
        f"Retry with `{endpoint}` and body "
        "`{\"answer\":\"<answer text>\",\"answered_by\":\"<session-id>\"}`."
    )

app = FastAPI(title="Fleet Orchestrator API", version=RUNNING_VERSION)
# SECURITY: chat is an injection vector (posts become content an AI session reads). It defaults
# ON because it is a promised local/trusted-LAN capability. Non-loopback mutable API startup
# fails closed unless ORCH_AUTH_TOKEN is set or ORCH_ALLOW_UNAUTH_NON_LOOPBACK acknowledges
# the trusted-LAN exposure.
if chat_enabled():
    app.include_router(chat_router)
_UI_ROOT = Path(__file__).resolve().parent.parent / "ui"
ALLOWED_NOTIFY_TYPES = {
    "standard": "message",
    "escalation": "escalation",
    "command": "command",
    "response_ready": "response_ready",
}
MUTABLE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}


def _auth_token() -> Optional[str]:
    token = os.environ.get("ORCH_AUTH_TOKEN")
    if token is None:
        return None
    token = token.strip()
    return token or None


def _bearer_token(authorization: Optional[str]) -> Optional[str]:
    if not authorization:
        return None
    scheme, _, value = authorization.partition(" ")
    if scheme.lower() != "bearer" or not value:
        return None
    return value.strip() or None


def _request_credential(request: Request) -> Optional[str]:
    return _bearer_token(request.headers.get("authorization")) or request.headers.get("x-api-key")


def _credential_matches(expected: str, supplied: Optional[str]) -> bool:
    return supplied is not None and hmac.compare_digest(supplied, expected)


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower().strip("[]")
    return normalized == "localhost" or normalized == "::1" or normalized.startswith("127.")


def _allow_unauth_non_loopback() -> bool:
    return os.environ.get("ORCH_ALLOW_UNAUTH_NON_LOOPBACK", "").strip().lower() in TRUE_ENV_VALUES


def _enforce_mutable_api_exposure() -> None:
    host = api_host()
    if _is_loopback_host(host) or _auth_token():
        return
    if _allow_unauth_non_loopback():
        LOGGER.warning(
            "Fleet Orchestrator mutable API is bound to %s without ORCH_AUTH_TOKEN because "
            "ORCH_ALLOW_UNAUTH_NON_LOOPBACK is set; unauthenticated non-loopback exposure is "
            "explicitly acknowledged for this trusted single-user network.",
            host,
        )
        return
    message = (
        f"Fleet Orchestrator refuses to start mutable API on non-loopback host {host!r} "
        "without ORCH_AUTH_TOKEN. Set ORCH_AUTH_TOKEN, bind ORCH_HOST=127.0.0.1, "
        "or set ORCH_ALLOW_UNAUTH_NON_LOOPBACK=1 to explicitly acknowledge trusted-LAN "
        "unauthenticated exposure."
    )
    LOGGER.warning(
        "%s POST/PUT/PATCH/DELETE endpoints would otherwise be reachable without credentials.",
        message,
    )
    raise SystemExit(message)


@app.middleware("http")
async def _optional_mutable_auth(request: Request, call_next):
    token = _auth_token()
    if token and request.method.upper() in MUTABLE_METHODS:
        if not _credential_matches(token, _request_credential(request)):
            return JSONResponse(status_code=401, content={"detail": AUTH_FAILURE_DETAIL})
    return await call_next(request)


@app.on_event("startup")
def _init_schema_on_startup() -> None:
    _enforce_mutable_api_exposure()
    result = init_schema(config=_cfg())
    errors = result.get("errors") or []
    if errors:
        raise RuntimeError(f"orchestrator schema initialization failed: {errors}")
    try:
        cfg = _cfg()
        count = ensure_handoff_index_backfilled(
            notify_redis_connect(),
            prefix=os.environ.get("NOTIFY_KEY_PREFIX", "taey"),
        )
        if count:
            LOGGER.info("Backfilled Redis handoff dispatcher index with %s record(s)", count)
    except Exception as exc:
        LOGGER.warning("Redis handoff dispatcher index backfill failed at startup: %s", exc)


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
    forced_closure = bool(project.get("forced_closure"))
    return {
        "id": project.get("id"),
        "name": project.get("name"),
        "description": project.get("description"),
        "status": project.get("status"),
        "forced_closure": forced_closure,
        "closure_reason": project.get("closure_reason") if forced_closure else None,
        "completed_by": project.get("completed_by"),
        "source_path": project.get("source_path"),
        "source_kind": project.get("source_kind"),
        "source_sha256": project.get("source_sha256"),
        "user_stop_conditions": project.get("user_stop_conditions", []),
        "supervisor": project.get("supervisor"),
        "priority": project.get("priority"),
        "created_at": project.get("created_at"),
        "migration_exempt": bool(project.get("migration_exempt")),
        "stop_reason_current": project.get("stop_reason_current"),
        "stop_reason_history": project.get("stop_reason_history", []),
        "priority_history": project.get("priority_history", []),
        "stop_reason_orphaned": bool(project.get("stop_reason_orphaned")),
        **counts,
    }


def _created_at_epoch(project: Dict[str, Any]) -> Optional[float]:
    raw = project.get("created_at")
    if raw in (None, ""):
        return None
    to_native = getattr(raw, "to_native", None)
    if callable(to_native):
        raw = to_native()
    if isinstance(raw, dt.datetime):
        value = raw
    elif isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        try:
            value = dt.datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt.timezone.utc)
    return value.timestamp()


def _newest_project_sort_key(project: Dict[str, Any]) -> tuple[int, float]:
    epoch = _created_at_epoch(project)
    if epoch is None:
        return (1, 0.0)
    return (0, -epoch)


def _newest_project_rows(projects: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [_project_row(project) for project in sorted(projects, key=_newest_project_sort_key)]


def _strict_force_flag(data: Dict[str, Any]) -> bool:
    if "force" not in data:
        return False
    value = data["force"]
    if isinstance(value, bool):
        return value
    raise HTTPException(
        status_code=422,
        detail=f"force must be a JSON boolean. Minimal force-close body: {PROJECT_FORCE_COMPLETE_BODY}.",
    )


def _validated_source_path(
    source_path: Optional[str],
    refs_present: bool,
    session_id: Optional[str] = None,
    config: Optional[OrchConfig] = None,
) -> Optional[str]:
    normalized, error = validate_source_path_for_refs(
        source_path,
        refs_present=refs_present,
        session_id=session_id,
        config=config,
    )
    if error:
        raise HTTPException(status_code=422, detail=error)
    return normalized


def _ensure_registered_session(session_id: str, cfg: OrchConfig) -> None:
    configured = set(cfg.session_ids)
    if configured and not supervisor_access_resolution(session_id, config=cfg)["registered"]:
        raise HTTPException(
            status_code=400,
            detail=session_registration_error_detail(session_id, cfg),
        )


def _infer_dispatch_supervisor(target: str, data: Dict[str, Any]) -> Optional[str]:
    for key in ("from", "sender", "supervisor", "dispatcher"):
        value = str(data.get(key) or "").strip()
        if value:
            return value
    for suffix in ("-codex", "-gemini", "-grok", "-claude"):
        if target.endswith(suffix):
            return target[: -len(suffix)]
    return None


def _dispatch_description(task_id: str, message: str, cfg: OrchConfig) -> str:
    task = load_task_record(task_id, config=cfg)
    if task and task.get("description"):
        return str(task.get("description"))
    first_line = (message or "").splitlines()[0] if message else ""
    marker = task_id
    _, _, after = first_line.partition(marker)
    fallback = after.strip(" \t-:").lstrip("\u2014").strip()
    return fallback or task_id


def _dispatch_task_id_from_payload(data: Dict[str, Any], target: str) -> Optional[str]:
    if data.get("dispatch") is not True:
        return None
    task_id = str(data.get("task_id") or "").strip()
    if task_id:
        return task_id
    raise HTTPException(
        status_code=400,
        detail=_required_body_detail(
            "task_id",
            {"dispatch": True, "task_id": "<task-id>", "message": "<optional dispatch prompt>"},
            endpoint=f"POST /api/sessions/{target}/notify",
            command=(
                f"curl -X POST /api/sessions/{target}/notify "
                "-d '{\"dispatch\":true,\"task_id\":\"<task-id>\"}'"
            ),
        ),
    )


@app.get("/api/tasks")
def list_tasks() -> Dict[str, Any]:
    tasks = get_ready_tasks(_cfg())
    return {"tasks": tasks}


@app.get("/api/tasks/ranked")
def list_ranked() -> Dict[str, Any]:
    tasks = get_ready_tasks(_cfg())
    # Sort by priority ASC (lowest number = highest priority per project convention).
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
        raise HTTPException(status_code=404, detail=_task_not_found_detail(task_id))
    return _serialize_node(result["t"])


@app.get("/api/tasks/{task_id}")
def get_task(task_id: str) -> Dict[str, Any]:
    cfg = _cfg()
    task_id = resolve_task_id(task_id, config=cfg)  # bare id -> canonical namespaced node
    task = load_task_record(task_id, config=cfg)
    if not task:
        raise HTTPException(status_code=404, detail=_task_not_found_detail(task_id))
    return task


@app.post("/api/task/create")
async def create(req: Request) -> Dict[str, Any]:
    data = await req.json()
    description = data.get("description") or data.get("title")
    if not description:
        raise HTTPException(
            status_code=400,
            detail=_required_body_detail(
                "description",
                {"description": "<task description>", "from": "<session-id>"},
                endpoint="POST /api/task/create",
                command="taey-task create '<task description>'",
            ),
        )

    priority = int(data.get("priority", 50))
    # External audit amendment #3: refuse negative priority values.
    # A prior migration script wrote priority = -<unix_timestamp> on 33 projects
    # (data-corruption fix applied as one-off Cypher at 2026-05-31 01:51Z). This
    # guard prevents any future write of negative-epoch-style values via the API.
    if priority < 0:
        raise HTTPException(
            status_code=400,
            detail=(
                f"priority must be >= 0 (got {priority}). Negative values were a 2026-05 "
                f"migration artifact and are no longer accepted. Next step: {TASK_CREATE_NEXT_STEP}"
            ),
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
        raise HTTPException(status_code=400, detail=f"{exc} Next step: {TASK_CREATE_NEXT_STEP}")
    except TaskParentNotFoundError as exc:
        raise HTTPException(status_code=404, detail=f"{exc} Next step: {TASK_CREATE_NEXT_STEP}")
    except TaskIdCollisionError as exc:
        # Fail-closed (bad/orphan/fused phase_id, or an owned id) -> 409, not a raw 500 (R5 audit:
        # match the /phases + /plan routes which already map this to 4xx).
        raise HTTPException(status_code=409, detail=f"{exc} Next step: {TASK_CREATE_NEXT_STEP}")

    return {"ok": True, "task_id": task_id, "from": sender, "owner": owner, "task_type": task_type}


def _terminal_evidence_from_request(data: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    evidence = data.get("evidence")
    if evidence is not None:
        return evidence
    lifted = {
        key: data[key]
        for key in REQUEST_TERMINAL_EVIDENCE_KEYS
        if key in data
    }
    return lifted or None


def _outcome_details(result: str, evidence: Optional[Dict[str, Any]]) -> Optional[str]:
    if result:
        return result
    if not isinstance(evidence, dict):
        return None
    for key in REQUEST_TERMINAL_EVIDENCE_KEYS:
        value = evidence.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None


def _origin_allowed_for_ui(req: Request) -> bool:
    origin = req.headers.get("origin") or ""
    if not origin:
        return True
    host = req.headers.get("host") or ""
    return bool(host and origin.rstrip("/").endswith(f"://{host}"))


@app.patch("/api/task/{task_id}")
async def update(task_id: str, req: Request) -> Dict[str, Any]:
    status = "pending"
    sender = ""
    try:
        try:
            data = await req.json()
        except Exception as exc:
            return JSONResponse(
                status_code=400,
                content={
                    "ok": False,
                    "error": f"request body must be valid JSON: {exc}",
                    "next_step": _task_update_body_next_step(task_id),
                },
            )
        if not isinstance(data, dict):
            return JSONResponse(
                status_code=422,
                content={
                    "ok": False,
                    "error": f"request body must be a JSON object, got {type(data).__name__}",
                    "next_step": _task_update_body_next_step(task_id),
                },
            )
        status = data.get("status", "pending")
        sender = data.get("from", "")
        result = data.get("result", "")

        cfg = _cfg()
        task_id = resolve_task_id(task_id, config=cfg)  # bare id -> canonical namespaced node
        task_before = _load_task(task_id, cfg)
        owner = data.get("owner")
        if owner is None:
            owner = task_before.get("owner", "")
        blocked_on = data["blocked_on"] if "blocked_on" in data else None
        completion_evidence = _terminal_evidence_from_request(data)
        from fleet_orchestrator.completion_guard import peer_self_completion_rejection

        rejection = peer_self_completion_rejection(
            task_id,
            task_before,
            sender,
            status,
            config=cfg,
        )
        if rejection:
            return JSONResponse(status_code=409, content=rejection)

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
            "completion_evidence": completion_evidence if status in TERMINAL_STATUSES else task_before.get("completion_evidence"),
            "phase_completed": phase_completed,
        }
    except CompletionEvidenceError as e:
        return JSONResponse(
            status_code=400,
            content={
                "ok": False,
                "error": str(e),
                "next_step": _terminal_evidence_next_step(task_id, str(status or "").strip() or "completed"),
            },
        )
    except HTTPException:
        raise
    except Exception as e:
        LOGGER.exception(
            "Unhandled task update failed task=%s status=%s sender=%s",
            task_id,
            status,
            sender,
        )
        return JSONResponse(
            status_code=500,
            content={"ok": False, "error": str(e)},
        )


@app.post("/api/human-review-gates")
async def create_human_review_gate_endpoint(req: Request) -> Dict[str, Any]:
    data = await req.json()
    try:
        phase_id = str(data.get("phase_id") or "").strip()
        task_id = str(data.get("task_id") or "").strip()
        question_id = str(data.get("question_id") or f"question-{uuid.uuid4().hex[:8]}").strip()
        prompt = str(data.get("prompt") or data.get("question") or "").strip()
        reviewer = str(data.get("reviewer") or data.get("human") or "operator").strip()
        requested_by = str(data.get("from") or data.get("requested_by") or "orch-human-review").strip()
        if not phase_id:
            raise HTTPException(
                status_code=422,
                detail=_required_body_detail(
                    "phase_id",
                    {"phase_id": "<phase-id>", "task_id": "<task-id>", "prompt": "<review question>"},
                    endpoint="POST /api/human-review-gates",
                ),
            )
        if not task_id:
            raise HTTPException(
                status_code=422,
                detail=_required_body_detail(
                    "task_id",
                    {"phase_id": "<phase-id>", "task_id": "<task-id>", "prompt": "<review question>"},
                    endpoint="POST /api/human-review-gates",
                ),
            )
        if not prompt:
            raise HTTPException(
                status_code=422,
                detail=_required_body_detail(
                    "prompt",
                    {"phase_id": "<phase-id>", "task_id": "<task-id>", "prompt": "<review question>"},
                    endpoint="POST /api/human-review-gates",
                ),
            )
        result = create_human_review_gate(
            phase_id=phase_id,
            task_id=task_id,
            question_id=question_id,
            prompt=prompt,
            reviewer=reviewer,
            requested_by=requested_by,
            refs=data.get("refs") if isinstance(data.get("refs"), list) else None,
            priority=int(data.get("priority", 50)),
            notify=bool(data.get("notify", True)),
            config=_cfg(),
        )
        return {"ok": True, **result}
    except HTTPException:
        raise
    except (TaskParentNotFoundError, TaskIdCollisionError, CompletionEvidenceError, ValueError) as exc:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": str(exc), "next_step": _human_gate_next_step(question_id)},
        )
    except Exception as exc:
        LOGGER.exception(
            "Unhandled human-review gate creation failed phase=%s task=%s question=%s",
            data.get("phase_id"),
            data.get("task_id"),
            data.get("question_id"),
        )
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})


@app.post("/api/questions/{question_id}/answer")
async def answer_question_endpoint(question_id: str, req: Request) -> Dict[str, Any]:
    data = await req.json()
    answer = str(data.get("answer") or data.get("verdict") or "").strip()
    answered_by = str(data.get("answered_by") or data.get("from") or "unauthenticated-api").strip()
    if not answer:
        raise HTTPException(
            status_code=422,
            detail=_required_body_detail(
                "answer",
                {"answer": "<answer text>", "answered_by": "<session-id>"},
                endpoint=f"POST /api/questions/{question_id}/answer",
            ),
        )
    try:
        result = answer_question(question_id, answer, answered_by, config=_cfg())
        if not result.get("ok"):
            raise HTTPException(
                status_code=404,
                detail=_question_not_found_detail(
                    question_id,
                    endpoint=f"POST /api/questions/{question_id}/answer",
                ),
            )
        return result
    except HTTPException:
        raise
    except (CompletionEvidenceError, ValueError) as exc:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": str(exc), "next_step": _human_gate_next_step(question_id)},
        )
    except Exception as exc:
        LOGGER.exception("Unhandled question answer failed question=%s", question_id)
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})


@app.post("/api/ui/questions/{question_id}/answer")
async def ui_answer_human_review_gate_endpoint(question_id: str, req: Request) -> Dict[str, Any]:
    if not _origin_allowed_for_ui(req):
        raise HTTPException(status_code=403, detail="origin does not match dashboard host")
    data = await req.json()
    answer = str(data.get("answer") or data.get("verdict") or "").strip()
    answered_by = str(data.get("answered_by") or data.get("from") or "operator").strip()
    if not answer:
        raise HTTPException(
            status_code=422,
            detail=_required_body_detail(
                "answer",
                {"answer": "<answer text>", "answered_by": "<session-id>"},
                endpoint=f"POST /api/ui/questions/{question_id}/answer",
            ),
        )
    try:
        result = complete_human_review_gate(question_id, answer, answered_by, config=_cfg())
        if not result.get("ok"):
            raise HTTPException(
                status_code=404,
                detail=_question_not_found_detail(
                    question_id,
                    endpoint=f"POST /api/ui/questions/{question_id}/answer",
                    ui=True,
                ),
            )
        return result
    except HTTPException:
        raise
    except (CompletionEvidenceError, ValueError) as exc:
        return JSONResponse(
            status_code=400,
            content={"ok": False, "error": str(exc), "next_step": _human_gate_next_step(question_id)},
        )
    except Exception as exc:
        LOGGER.exception("Unhandled UI human-review answer failed question=%s", question_id)
        return JSONResponse(status_code=500, content={"ok": False, "error": str(exc)})


@app.get("/api/projects")
def list_projects() -> Dict[str, Any]:
    """List all OrchProject nodes with decoded Stage A fields and aggregate task status counts."""
    cfg = _cfg()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        result = session.run(
            """
            MATCH (p:OrchProject)
            RETURN p
            ORDER BY CASE WHEN p.created_at IS NULL THEN 1 ELSE 0 END ASC,
                     p.created_at DESC
            """
        )
        projects = []
        for record in result:
            project = _serialize_node(record["p"])
            project["user_stop_conditions"] = _decode_json(project.get("user_stop_conditions"), [])
            project["stop_reason_current"] = _decode_json(project.get("stop_reason_current"), None)
            project["stop_reason_history"] = _decode_json(project.get("stop_reason_history"), [])
            project["priority_history"] = _decode_json(project.get("priority_history"), [])
            project["stop_reason_orphaned"] = False
            projects.append(project)
    return {"projects": _newest_project_rows(projects)}


@app.get("/api/projects/{project_id}")
def get_project(project_id: str) -> Dict[str, Any]:
    """Project summary with phase task counts."""
    summary = get_project_summary(project_id, config=_cfg())
    if not summary:
        raise HTTPException(status_code=404, detail=_project_not_found_detail(project_id))
    summary.update(_project_row(summary.get("project") or {}))
    return summary


@app.post("/api/projects")
async def create_project_endpoint(req: Request) -> Dict[str, Any]:
    """Create an OrchProject."""
    data = await req.json()
    project_id = data.get("id")
    name = data.get("name", project_id)
    if not project_id:
        raise HTTPException(
            status_code=400,
            detail=_required_body_detail(
                "id",
                {"id": "<project-id>", "name": "<project name>", "supervisor": "<supervisor-session>"},
                endpoint="POST /api/projects",
                command="taey-plan ingest <plan.md> --supervisor <supervisor-session>",
            ),
        )
    supervisor = (data.get("supervisor") or "").strip()
    if not supervisor or supervisor == "unassigned":
        raise HTTPException(
            status_code=400,
            detail=f"supervisor must be non-empty and not 'unassigned'. Next step: {PROJECT_CREATE_NEXT_STEP}",
        )
    # External audit amendment #3: refuse negative project priority.
    project_priority = data.get("priority")
    if project_priority is not None and int(project_priority) < 0:
        raise HTTPException(
            status_code=400,
            detail=(
                f"priority must be >= 0 (got {project_priority}). Negative values were a "
                f"2026-05 migration artifact and are no longer accepted. Next step: {PROJECT_CREATE_NEXT_STEP}"
            ),
        )
    cfg = _cfg()
    refs = data.get("refs") if isinstance(data.get("refs"), list) else None
    source_path = _validated_source_path(
        data.get("source_path"),
        refs_present=bool(refs),
        session_id=data.get("supervisor") or data.get("from") or "",
        config=cfg,
    )
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
        config=cfg,
    )
    return {"ok": True, "project_id": pid}


@app.get("/api/projects/{project_id}/user-stop-conditions")
def get_project_user_stop_conditions_endpoint(project_id: str) -> Dict[str, Any]:
    """Backward-compatible surface returning active condition labels only."""
    conditions = get_project_user_stop_conditions(project_id, config=_cfg())
    if conditions is None:
        raise HTTPException(status_code=404, detail=_project_not_found_detail(project_id))
    return {"project_id": project_id, "conditions": [condition["label"] for condition in conditions if not condition.get("deprecated_at")]}


@app.post("/api/projects/{project_id}/user-stop-conditions")
async def set_project_user_stop_conditions_endpoint(project_id: str, req: Request) -> Dict[str, Any]:
    data = await req.json()
    conditions = data.get("conditions")
    if not isinstance(conditions, list) or any(not isinstance(item, str) for item in conditions):
        raise HTTPException(
            status_code=400,
            detail=f"conditions must be a list of strings. Next step: {PROJECT_USER_STOP_CONDITIONS_NEXT_STEP}",
        )
    try:
        saved = set_project_user_stop_conditions(
            project_id,
            conditions,
            config=_cfg(),
            created_by=data.get("from", "legacy-api"),
        )
    except ProjectNotFoundError:
        raise HTTPException(status_code=404, detail=_project_not_found_detail(project_id))
    return {"ok": True, "project_id": project_id, "conditions": [condition["label"] for condition in saved if not condition.get("deprecated_at")]}


@app.post("/api/projects/{project_id}/phases")
async def create_phase_endpoint(project_id: str, req: Request) -> Dict[str, Any]:
    """Create an OrchPhase under a project."""
    data = await req.json()
    phase_id = data.get("id")
    name = data.get("name", phase_id)
    if not phase_id:
        raise HTTPException(
            status_code=400,
            detail=_required_body_detail(
                "id",
                {"id": "<phase-id>", "name": "<phase name>"},
                endpoint=f"POST /api/projects/{project_id}/phases",
            ),
        )
    # Caller-supplied phase id MUST go through the same scoping chokepoint as plan ingest (R3 audit
    # CRITICAL: this route fed a bare phase_id straight to create_phase's MERGE). Scope to <project>::<id>
    # + reject a declared '::' / bad charset; the create_phase ownership guard is the second layer.
    try:
        scoped_phase_id = scope_declared_id(project_id, phase_id)
    except PlanIdError as exc:
        raise HTTPException(status_code=400, detail=f"{exc} Next step: {PHASE_CREATE_NEXT_STEP}")
    cfg = _cfg()
    refs = data.get("refs") if isinstance(data.get("refs"), list) else None
    source_path = _validated_source_path(
        data.get("source_path"),
        refs_present=bool(refs),
        session_id=data.get("supervisor") or data.get("from") or "",
        config=cfg,
    )
    try:
        pid = create_phase(
            project_id=project_id,
            phase_id=scoped_phase_id,
            name=name,
            order=int(data.get("order", 0)),
            refs=refs,
            source_path=source_path,
            config=cfg,
        )
    except TaskIdCollisionError as exc:
        raise HTTPException(status_code=409, detail=f"{exc} Next step: {PHASE_CREATE_NEXT_STEP}")
    return {"ok": True, "phase_id": pid}


@app.post("/api/projects/load-md")
async def load_plan_md(req: Request) -> Dict[str, Any]:
    """Ingest a markdown plan into Neo4j as OrchProject/Phase/Task nodes."""
    data = await req.json()
    md_text = data.get("md_text")
    if not md_text:
        raise HTTPException(
            status_code=400,
            detail=_required_body_detail(
                "md_text",
                {
                    "md_text": "# Project: <project-id> - <name>\n\n## Phase: <phase>\n- [ ] <task>",
                    "supervisor": "<supervisor-session>",
                },
                endpoint="POST /api/projects/load-md",
                command="taey-plan ingest <plan.md> --supervisor <supervisor-session>",
            ),
        )
    supervisor = (data.get("supervisor") or "").strip()
    if supervisor in {"", "unassigned", "unknown"}:
        raise HTTPException(
            status_code=400,
            detail=(
                "supervisor required for non-exempt project ingest (must not be unassigned "
                f"or unknown). Next step: {PLAN_LOAD_NEXT_STEP}"
            ),
        )
    # External audit amendment #3: refuse negative project priority on plan ingest.
    ingest_priority = data.get("priority")
    if ingest_priority is not None and int(ingest_priority) < 0:
        raise HTTPException(
            status_code=400,
            detail=(
                f"priority must be >= 0 (got {ingest_priority}). Negative values were a "
                f"2026-05 migration artifact and are no longer accepted. Next step: {PLAN_LOAD_NEXT_STEP}"
            ),
        )
    cfg = _cfg()
    try:
        refs_present = plan_declares_refs(md_text)
        source_path = _validated_source_path(
            data.get("source_path", ""),
            refs_present=refs_present,
            session_id=supervisor,
            config=cfg,
        )
        return load_plan_from_text(
            md=md_text,
            source_path=source_path or "",
            source_kind=data.get("source_kind", "markdown"),
            ingested_by=data.get("ingested_by", "unknown"),
            supervisor=supervisor,
            priority=ingest_priority,
            migration_exempt=bool(data.get("migration_exempt", False)),
            config=cfg,
        )
    except (PlanIdError, PlanTerminalStatusError) as exc:
        raise HTTPException(status_code=400, detail=f"{exc} Next step: {PLAN_LOAD_NEXT_STEP}")
    except TaskIdCollisionError as exc:        # id owned by another project — refuse adoption
        raise HTTPException(status_code=409, detail=f"{exc} Next step: {PLAN_LOAD_NEXT_STEP}")


@app.post("/api/projects/{project_id}/complete")
async def complete_project_endpoint(project_id: str, req: Request) -> Dict[str, Any]:
    data = await req.json() if req.headers.get("content-type", "").startswith("application/json") else {}
    if not isinstance(data, dict):
        raise HTTPException(
            status_code=422,
            detail=f"request body must be a JSON object. Minimal force-close body: {PROJECT_FORCE_COMPLETE_BODY}.",
        )
    force = _strict_force_flag(data)
    closure_reason = data.get("closure_reason")
    if closure_reason is None:
        closure_reason = data.get("reason")
    try:
        return complete_project(
            project_id,
            force=force,
            completed_by=data.get("completed_by") or data.get("from") or "unknown",
            closure_reason=closure_reason,
            config=_cfg(),
        )
    except ReadyWorkConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail={
                "error": str(exc),
                "next_step": (
                    f"Inspect remaining work with `taey-plan show {project_id}` or GET /api/projects/{project_id}; "
                    "complete tasks with "
                    + _terminal_evidence_next_step("<task-id>", "completed")
                    + f" To force-close, POST /api/projects/{project_id}/complete with body {PROJECT_FORCE_COMPLETE_BODY}."
                ),
            },
        )
    except ProjectNotFoundError:
        raise HTTPException(status_code=404, detail=_project_not_found_detail(project_id))
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "error": str(exc),
                "next_step": f"POST /api/projects/{project_id}/complete with body {PROJECT_FORCE_COMPLETE_BODY}.",
            },
        )


@app.get("/api/projects/{project_id}/shippability")
async def project_shippability_endpoint(project_id: str) -> Dict[str, Any]:
    """Ship-gate verdict: shippable only when every -prodtest/-audit gate is completed."""
    return evaluate_shippability(project_id, config=_cfg())


@app.post("/api/projects/{project_id}/ship")
async def ship_project_endpoint(project_id: str) -> Dict[str, Any]:
    """Return a ship-gate verdict only.

    This endpoint refuses unshippable projects, but it does not persist shipped
    state or mutate the project into a shipped lifecycle state.
    """
    verdict = evaluate_shippability(project_id, config=_cfg())
    if not verdict.get("shippable"):
        raise HTTPException(status_code=409, detail=verdict)
    return {
        "ok": True,
        "action": "verdict",
        "shipped": False,
        "shippable": True,
        "verdict": verdict,
    }


@app.post("/api/projects/{project_id}/reset")
async def reset_project_endpoint(project_id: str, req: Request) -> Dict[str, Any]:
    data = await req.json() if req.headers.get("content-type", "").startswith("application/json") else {}
    try:
        return reset_project(
            project_id,
            reset_by=data.get("reset_by") or data.get("from") or "unknown",
            config=_cfg(),
        )
    except ProjectNotFoundError:
        raise HTTPException(status_code=404, detail=_project_not_found_detail(project_id))


@app.get("/api/sessions")
def sessions() -> Dict[str, Any]:
    """Sessions to render as dashboard cards — canonical supervisors from the configured allowlist."""
    return {"sessions": list_dashboard_sessions(config=_cfg())}


@app.get("/api/sessions/{session_id}/current")
def session_current(session_id: str) -> Dict[str, Any]:
    """What this session is currently executing — top in_progress task with project/phase context."""
    cfg = _cfg()
    activity = get_session_liveness(session_id, config=cfg)
    work = get_session_current_work(session_id, config=cfg)
    if not work:
        return {
            "session": session_id,
            "current": None,
            "activity": activity,
            "liveness": None,
            "next_action": (
                f"No current task is bound for {session_id}. Run `taey-plan next {session_id}` "
                f"or GET /api/sessions/{session_id}/next-ready to find ready work; inspect projects with "
                f"GET /api/sessions/{session_id}/projects."
            ),
        }
    from fleet_orchestrator.current_liveness import safe_current_task_liveness

    liveness = safe_current_task_liveness(session_id, work, config=cfg)
    current = {**work, "liveness": liveness}
    return {"session": session_id, "current": current, "activity": activity, "liveness": liveness}


@app.get("/api/sessions/{session_id}/next-ready")
def session_next_ready(session_id: str) -> Dict[str, Any]:
    """Top pending task owned-by this session only — under single-supervisor scope there is no claim-from-unowned-pool path."""
    cfg = _cfg()
    result = get_session_next_ready(session_id, config=cfg)
    if not result:
        return {
            "session": session_id,
            "next": None,
            "next_action": (
                f"No ready owned task for {session_id}. Run `taey-plan current {session_id}` "
                f"and GET /api/sessions/{session_id}/stop-status to distinguish idle, paused, "
                "awaiting human review, or stop-reason-required states."
            ),
        }
    return {"session": session_id, "next": result}


@app.get("/api/sessions/{session_id}/projects")
def session_projects(session_id: str) -> Dict[str, Any]:
    """Supervisor-based listing; replaces the earlier task-owner-based semantics."""
    projects = _newest_project_rows(get_session_supervised_projects(session_id, config=_cfg()))
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
        raise HTTPException(status_code=409, detail=f"{exc} Next step: {PROJECT_STOP_REASON_NEXT_STEP}")
    except (ConditionValidationError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"{exc} Next step: {PROJECT_STOP_REASON_NEXT_STEP}")
    except ProjectNotFoundError:
        raise HTTPException(status_code=404, detail=_project_not_found_detail(project_id))
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
    except ProjectNotFoundError:
        raise HTTPException(status_code=404, detail=_project_not_found_detail(project_id))
    return {"ok": ok}


@app.patch("/api/projects/{project_id}")
async def patch_project_endpoint(project_id: str, req: Request) -> Dict[str, Any]:
    data = await req.json()
    if "priority" not in data:
        raise HTTPException(
            status_code=400,
            detail=_required_body_detail(
                "priority",
                {"priority": 10, "reason": "<why>", "set_by": "<session-id>", "source_surface": "api"},
                endpoint=f"PATCH /api/projects/{project_id}",
            ),
        )
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
    except ProjectNotFoundError:
        raise HTTPException(status_code=404, detail=_project_not_found_detail(project_id))
    return {"ok": True, "project_id": project_id, **updated}


@app.post("/api/projects/{project_id}/conditions")
async def add_project_condition_endpoint(project_id: str, req: Request) -> Dict[str, Any]:
    data = await req.json()
    label = (data.get("label") or "").strip()
    if not label:
        raise HTTPException(
            status_code=400,
            detail=_required_body_detail(
                "label",
                {"created_by": "<session-id>", "label": "<stop condition label>"},
                endpoint=f"POST /api/projects/{project_id}/conditions",
            ),
        )
    try:
        condition = add_project_condition(project_id, label, created_by=data.get("created_by") or data.get("from") or "unknown", config=_cfg())
    except ProjectNotFoundError:
        raise HTTPException(status_code=404, detail=_project_not_found_detail(project_id))
    return {"ok": True, "condition": condition}


@app.patch("/api/projects/{project_id}/conditions/{condition_id}")
async def edit_project_condition_endpoint(project_id: str, condition_id: str, req: Request) -> Dict[str, Any]:
    data = await req.json()
    label = (data.get("label") or "").strip()
    if not label:
        raise HTTPException(
            status_code=400,
            detail=_required_body_detail(
                "label",
                {"edited_by": "<session-id>", "label": "<replacement stop condition label>"},
                endpoint=f"PATCH /api/projects/{project_id}/conditions/{condition_id}",
                command=f"taey-plan stop-conditions {project_id} get",
            ),
        )
    try:
        condition = edit_project_condition(project_id, condition_id, label, edited_by=data.get("edited_by") or data.get("from") or "unknown", config=_cfg())
    except ConditionValidationError as exc:
        raise HTTPException(status_code=400, detail=f"{exc} Next step: {PROJECT_CONDITION_EDIT_NEXT_STEP}")
    except ProjectNotFoundError:
        raise HTTPException(status_code=404, detail=_project_not_found_detail(project_id))
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
    if not target.strip():
        raise HTTPException(
            status_code=400,
            detail=_non_empty_path_detail(
                "target",
                endpoint="POST /api/sessions/{target}/notify",
                example="POST /api/sessions/session-1/notify",
            ),
        )

    data = await req.json()
    command_task_id = _dispatch_task_id_from_payload(data, target)
    notify_type = data.get("type", "command" if command_task_id else "standard")
    message = (data.get("message") or "").strip()

    if notify_type not in ALLOWED_NOTIFY_TYPES:
        raise HTTPException(
            status_code=400,
            detail=(
                "type must be one of standard, escalation, command, response_ready. "
                f"Use `taey-notify {target} '<notification text>' --type standard` "
                f"or POST /api/sessions/{target}/notify body "
                '{"message":"<notification text>","type":"standard"}.'
            ),
        )
    if not message and not command_task_id:
        raise HTTPException(
            status_code=400,
            detail=_required_body_detail(
                "message",
                {"message": "<notification text>", "type": "standard"},
                endpoint=f"POST /api/sessions/{target}/notify",
                command="taey-notify <target> '<notification text>' --type standard",
            ),
        )
    cfg = _cfg()
    _ensure_registered_session(target, cfg)

    if command_task_id:
        supervisor = _infer_dispatch_supervisor(target, data)
        description = _dispatch_description(command_task_id, message, cfg)
        try:
            dispatch_task(
                target,
                command_task_id,
                description,
                supervisor=supervisor,
                prompt_body=message or None,
                priority=str(data.get("priority") or "normal"),
                force=bool(data.get("force")),
            )
        except (BugLockActive, HooksNotInstalled, OrchTaskNotReady, WorkerBusy) as exc:
            raise HTTPException(
                status_code=409,
                detail={
                    "error": "dispatch command rejected",
                    "reason": str(exc),
                    "task_id": command_task_id,
                    "next_step": (
                        "Inspect task readiness with GET /api/tasks/{task_id} or "
                        f"`taey-task status {command_task_id}` before retrying dispatch."
                    ),
                },
            )
        except RuntimeError as exc:
            raise HTTPException(
                status_code=502,
                detail={
                    "error": "taey-notify failed",
                    "notify_stderr": str(exc),
                    "next_step": (
                        f"Run `taey-notify {target} '<notification text>' --type command` "
                        "and inspect stderr; verify target session registration with GET /api/sessions."
                    ),
                },
            )
        return {"ok": True, "dispatch_registered": True, "task_id": command_task_id}

    result = subprocess.run(
        ["taey-notify", target, message, "--type", ALLOWED_NOTIFY_TYPES[notify_type]],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise HTTPException(
            status_code=502,
            detail={
                "error": "taey-notify failed",
                "notify_stderr": stderr,
                "next_step": (
                    f"Run `taey-notify {target} '<notification text>' --type {notify_type}` "
                    "and inspect stderr; verify target session registration with GET /api/sessions."
                ),
            },
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
        return disabled_loop_response()
    data = await req.json()
    raw_loop = data.get("loop") if isinstance(data, dict) and "loop" in data else data
    try:
        return declare_loop(raw_loop, persistence=Neo4jCycleStateStore(config=_cfg()))
    except LoopDeclarationError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc), "next_step": LOOP_DECLARATION_NEXT_STEP})
    except LoopPersistenceError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.post("/api/loops/{loop_id}/advance")
async def loop_advance(loop_id: str, req: Request) -> Dict[str, Any]:
    if not loops_enabled():
        return disabled_loop_response()
    data = await req.json()
    step_name = str(data.get("step") or "").strip()
    if not step_name:
        raise HTTPException(
            status_code=400,
            detail=_required_body_detail(
                "step",
                {"step": "<step name>"},
                endpoint=f"POST /api/loops/{loop_id}/advance",
            ),
        )
    cfg = _cfg()
    store = Neo4jCycleStateStore(config=cfg)
    raw_loop = store.load(loop_id)
    if raw_loop is None:
        raise HTTPException(status_code=404, detail=f"Loop {loop_id} not found. Next step: {LOOP_LOOKUP_NEXT_STEP}")
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
        raise HTTPException(status_code=409, detail=f"{exc} Next step: {LOOP_ADVANCE_NEXT_STEP}")
    except LoopDeclarationError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc), "next_step": LOOP_DECLARATION_NEXT_STEP})
    except LoopPersistenceError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/api/loops/{loop_id}/should-stop")
def loop_should_stop(loop_id: str) -> Dict[str, Any]:
    if not loops_enabled():
        return disabled_loop_response()
    raw_loop = Neo4jCycleStateStore(config=_cfg()).load(loop_id)
    if raw_loop is None:
        raise HTTPException(status_code=404, detail=f"Loop {loop_id} not found. Next step: {LOOP_LOOKUP_NEXT_STEP}")
    try:
        loop = Loop.declare(raw_loop)
    except LoopDeclarationError as exc:
        raise HTTPException(status_code=400, detail={"error": str(exc), "next_step": LOOP_DECLARATION_NEXT_STEP})
    return {"ok": True, "enabled": True, "loop_id": loop_id, "should_stop": loop.should_stop()}


@app.get("/api/sessions/{session_id}/wake-packet")
def session_wake_packet(
    session_id: str,
    cli: str = Query("claude"),
    task_id: Optional[str] = Query(None),
    budget_bytes: int = Query(CORE_BUDGET_BYTES, ge=1024, le=128 * 1024),
) -> Dict[str, Any]:
    """Assemble a session's wake-state packet.

    CONSUMER BANNER: consumers MUST check body[ok]; HTTP 200 alone does not
    imply context was assembled.

    CONTRACT — this endpoint is **fail-open by design**: a wake-state packet is
    OPTIONAL context, and an assembly error must never break or block a wake. So
    on any assembler exception it returns HTTP 200 with ``{"ok": false, "enabled":
    true, "operation": "wake_packet_assembly", "error": ..., "next_step": ...}``
    rather than a 5xx, and this endpoint's disabled flag returns ``{"ok": true,
    "enabled": false, "reason": ..., "enable_with": ...}``. **Consumers MUST gate on the body
    (`ok` AND `enabled` AND a non-empty `packet`) — never on the HTTP status
    alone**, or they will inject an empty/error body as if it were context. The
    shipped consumer (`claude-code-fleet-notify` `_fetch_wake_packet`) does this
    correctly; any new consumer must too.
    """
    if not wake_packet_endpoint_enabled():
        return {
            "ok": True,
            "enabled": False,
            "reason": "wake packet endpoint disabled",
            "enable_with": "ORCH_WAKE_PACKET_ENDPOINT_ENABLED=1",
            "next_step": (
                "Set ORCH_WAKE_PACKET_ENDPOINT_ENABLED=1 and restart the API, "
                "then retry GET /api/sessions/{session_id}/wake-packet?cli=<claude|codex>."
            ),
        }

    cli_key = cli.lower().strip()
    if cli_key not in VALID_CLIS:
        raise HTTPException(
            status_code=400,
            detail=f"cli must be one of {', '.join(sorted(VALID_CLIS))}. Next step: {WAKE_PACKET_CLI_NEXT_STEP}",
        )
    if not session_id.strip():
        raise HTTPException(
            status_code=400,
            detail=_non_empty_path_detail(
                "session_id",
                endpoint="GET /api/sessions/{session_id}/wake-packet?cli=codex",
                example="GET /api/sessions/session-1-codex/wake-packet?cli=codex",
            ),
        )

    try:
        cfg = _cfg()
        _ensure_registered_session(session_id, cfg)
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
            "operation": "wake_packet_assembly",
            "error": str(exc),
            "next_step": (
                "Wake continues without a packet. Inspect orchestrator API logs for "
                "wake_packet_assembly and validate context-assembler inputs for this "
                f"session, then retry GET /api/sessions/{session_id}/wake-packet?cli={cli_key}."
            ),
        }


@app.get("/health")
def health() -> Dict[str, Any]:
    try:
        _ = get_ready_tasks(_cfg())
        return {
            "ok": True,
            "service": "fleet-orchestrator-api",
            "version": RUNNING_VERSION,
            "api_base": os.environ.get("ORCH_API_BASE", "http://127.0.0.1:5002"),
            "ts": time.time(),
        }
    except Exception as e:
        return JSONResponse(
            status_code=503,
            content={
                "ok": False,
                "service": "fleet-orchestrator-api",
                "version": RUNNING_VERSION,
                "api_base": os.environ.get("ORCH_API_BASE", "http://127.0.0.1:5002"),
                "dependency": "neo4j",
                "operation": "get_ready_tasks",
                "error": str(e),
                "next_step": (
                    "Check ORCH_NEO4J_URI / ORCH_NEO4J_DB and that Neo4j is reachable, "
                    "then retry GET /health."
                ),
                "ts": time.time(),
            },
        )


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
