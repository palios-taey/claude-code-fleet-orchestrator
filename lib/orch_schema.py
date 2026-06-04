"""
Neo4j Orchestration Schema

Creates task DAG schema in the neo4j database with Orch-prefixed labels
to isolate from memory infrastructure (ISMA, HMM, Weaviate).

Label convention: OrchProject, OrchPhase, OrchTask, OrchFileOwnership
(memory labels: ISMAExchange, HMMTile, HMMMotif, Message, ChatSession)
"""

import copy
import datetime as dt
import itertools
import json
import logging
import os
import stat
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import OrchConfig, ensure_notify_importable, get_neo4j_driver
from .handoff_validation import flags_for_session, validate_stop_handoff


SCHEMA_CONSTRAINTS = [
    "CREATE CONSTRAINT orch_task_id IF NOT EXISTS FOR (t:OrchTask) REQUIRE t.id IS UNIQUE",
    "CREATE CONSTRAINT orch_project_id IF NOT EXISTS FOR (p:OrchProject) REQUIRE p.id IS UNIQUE",
    "CREATE CONSTRAINT orch_phase_id IF NOT EXISTS FOR (ph:OrchPhase) REQUIRE ph.id IS UNIQUE",
    "CREATE CONSTRAINT orch_question_id IF NOT EXISTS FOR (q:OrchQuestion) REQUIRE q.id IS UNIQUE",
    "CREATE CONSTRAINT orch_stop_convergence_audit_id IF NOT EXISTS FOR (a:OrchStopConvergenceAudit) REQUIRE a.id IS UNIQUE",
]

SCHEMA_INDEXES = [
    "CREATE INDEX orch_task_status IF NOT EXISTS FOR (t:OrchTask) ON (t.status)",
    "CREATE INDEX orch_task_owner IF NOT EXISTS FOR (t:OrchTask) ON (t.owner)",
    "CREATE INDEX orch_file_path IF NOT EXISTS FOR (f:OrchFileOwnership) ON (f.path)",
    "CREATE INDEX orch_question_status IF NOT EXISTS FOR (q:OrchQuestion) ON (q.status)",
    "CREATE INDEX orch_project_supervisor IF NOT EXISTS FOR (p:OrchProject) ON (p.supervisor)",
    "CREATE INDEX orch_project_priority IF NOT EXISTS FOR (p:OrchProject) ON (p.priority)",
    "CREATE INDEX orch_project_supervisor_status_priority IF NOT EXISTS FOR (p:OrchProject) ON (p.supervisor, p.status, p.priority)",
]


class ProjectNotFoundError(ValueError):
    pass


class ReadyWorkConflictError(ValueError):
    pass


class ConditionValidationError(ValueError):
    pass


class PriorityAuditError(ValueError):
    pass


class PauseValidationError(ValueError):
    pass


class CompletionEvidenceError(ValueError):
    pass


_PAUSE_SOURCES = {"ui", "cli", "api", "user_command_explicit"}
_REF_READ_BYTE_CAP = 1024 * 1024
_COMPLETION_EVIDENCE_KEYS = ("commit_sha", "gate_run_id", "production_observation")


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _json_encode(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _decode_json_field(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return copy.deepcopy(default)
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return copy.deepcopy(default)
    return raw


def _normalize_completion_evidence(evidence: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    if evidence is None:
        return None
    if not isinstance(evidence, dict):
        raise CompletionEvidenceError("completion evidence must be a JSON object")
    normalized: Dict[str, str] = {}
    for key in _COMPLETION_EVIDENCE_KEYS:
        value = evidence.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            normalized[key] = text
    if not normalized:
        raise CompletionEvidenceError(
            "completed status requires evidence with at least one of: commit_sha, gate_run_id, production_observation"
        )
    return normalized


def _normalize_owner_session(owner: str) -> str:
    owner = (owner or "").strip()
    for suffix in ("-codex", "-gemini", "-grok"):
        if owner.endswith(suffix):
            return owner[: -len(suffix)]
    return owner


def _condition_view(condition: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "condition_id": condition["id"],
        "version": int(condition["version"]),
        "label": condition["label"],
    }


def _build_condition(label: str, created_by: str, *, condition_id: Optional[str] = None,
                     version: int = 1, deprecated_at: Optional[str] = None,
                     replaces_id: Optional[str] = None,
                     created_at: Optional[str] = None) -> Dict[str, Any]:
    cond_id = condition_id or uuid.uuid4().hex
    return {
        "id": cond_id,
        "label": label,
        "version": int(version),
        "created_at": created_at or _utc_now_iso(),
        "created_by": created_by or "unknown",
        "deprecated_at": deprecated_at,
        "replaces_id": replaces_id,
    }


def _normalize_user_stop_conditions(conditions: Optional[List[Any]], created_by: str) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for item in conditions or []:
        if isinstance(item, str):
            normalized.append(_build_condition(item, created_by))
            continue
        if isinstance(item, dict):
            label = str(item.get("label") or "").strip()
            if not label:
                continue
            normalized.append(_build_condition(
                label,
                str(item.get("created_by") or created_by or "unknown"),
                condition_id=str(item.get("id") or item.get("condition_id") or uuid.uuid4().hex),
                version=int(item.get("version", 1)),
                deprecated_at=item.get("deprecated_at"),
                replaces_id=item.get("replaces_id"),
                created_at=item.get("created_at"),
            ))
    return normalized


def _active_conditions(conditions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [cond for cond in conditions if not cond.get("deprecated_at")]


def _decode_project_node(node: Dict[str, Any]) -> Dict[str, Any]:
    project = _normalize_map(node)
    project["user_stop_conditions"] = _normalize_user_stop_conditions(
        _decode_json_field(project.get("user_stop_conditions"), []),
        created_by="decoded",
    )
    project["refs"] = _decode_json_field(project.get("refs"), [])
    project["stop_reason_current"] = _decode_json_field(project.get("stop_reason_current"), None)
    project["stop_reason_history"] = _decode_json_field(project.get("stop_reason_history"), [])
    project["priority_history"] = _decode_json_field(project.get("priority_history"), [])
    stop_state = _project_stop_reason_state(project)
    project["stop_reason_orphaned"] = stop_state["orphaned"]
    return project


def _normalize_refs(raw_refs: Any) -> List[Dict[str, Any]]:
    refs = _decode_json_field(raw_refs, [])
    if not isinstance(refs, list):
        return []
    normalized: List[Dict[str, Any]] = []
    for item in refs:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path") or "").strip()
        if _has_control_chars(path):
            continue
        try:
            l_start = int(item.get("l_start"))
            l_end = int(item.get("l_end"))
        except Exception:
            continue
        if not path or l_start <= 0 or l_end < l_start:
            continue
        entry = {"path": path, "l_start": l_start, "l_end": l_end}
        label = str(item.get("label") or "").strip()
        if label:
            entry["label"] = label
        normalized.append(entry)
    return normalized


def _encode_refs_or_none(refs: Optional[List[Dict[str, Any]]]) -> Optional[str]:
    if refs is None:
        return None
    return _json_encode(_normalize_refs(refs))


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 32 for ch in value)


def _allowed_ref_roots() -> List[Path]:
    raw = str(os.environ.get("ORCH_REF_ALLOWED_ROOT") or "").strip()
    if not raw:
        return []
    candidates: List[str]
    if raw.startswith("["):
        try:
            parsed = json.loads(raw)
        except Exception:
            parsed = []
        candidates = [str(item).strip() for item in parsed if str(item).strip()]
    else:
        normalized = raw.replace(os.pathsep, ",")
        candidates = [item.strip() for item in normalized.split(",") if item.strip()]
    return [Path(item).expanduser().resolve(strict=False) for item in candidates]


def _path_within_any_root(path: Path, roots: List[Path]) -> bool:
    for root in roots:
        try:
            path.relative_to(root)
            return True
        except ValueError:
            continue
    return False


def _ref_allowed_root(source_path: Optional[str]) -> Optional[Path]:
    if not source_path:
        return None
    try:
        return Path(source_path).resolve(strict=False).parent
    except Exception:
        return None


def validate_source_path_for_refs(source_path: Optional[str], refs_present: bool) -> tuple[Optional[str], Optional[str]]:
    raw_path = str(source_path or "").strip()
    if not refs_present:
        if not raw_path:
            return None, None
        try:
            return str(Path(raw_path).resolve(strict=False)), None
        except Exception as exc:
            return None, f"invalid source_path ({exc.__class__.__name__})"
    if not raw_path:
        return None, "refs require source_path"
    allowed_roots = _allowed_ref_roots()
    if not allowed_roots:
        return None, "refs require ORCH_REF_ALLOWED_ROOT"
    try:
        resolved_source = Path(raw_path).resolve(strict=False)
    except Exception as exc:
        return None, f"invalid source_path ({exc.__class__.__name__})"
    if not _path_within_any_root(resolved_source, allowed_roots):
        return None, f"source_path outside ORCH_REF_ALLOWED_ROOT: {raw_path}"
    return str(resolved_source), None


def resolve_ref_path(ref_path: str, source_path: Optional[str]) -> tuple[Optional[Path], Optional[str]]:
    raw_path = str(ref_path or "").strip()
    if not raw_path:
        return None, "ref unreadable: empty path"
    if _has_control_chars(raw_path):
        return None, "ref unreadable: control characters in path"
    if raw_path.startswith("~"):
        return None, f"ref outside allowed root: {raw_path}"
    candidate = Path(raw_path)
    if candidate.is_absolute():
        return None, f"ref outside allowed root: {raw_path}"

    root = _ref_allowed_root(source_path)
    if root is None:
        return None, "ref has no plan-source root (sandbox undefined)"
    allowed_roots = _allowed_ref_roots()
    if not allowed_roots:
        return None, "ref disabled: ORCH_REF_ALLOWED_ROOT is unset"
    try:
        if not _path_within_any_root(root, allowed_roots):
            return None, f"ref outside allowed root: {raw_path}"
        resolved = (root / candidate).resolve(strict=False)
        if not _path_within_any_root(resolved, [root]):
            return None, f"ref outside allowed root: {raw_path}"
        if not _path_within_any_root(resolved, allowed_roots):
            return None, f"ref outside allowed root: {raw_path}"
        return resolved, None
    except Exception as exc:
        return None, f"ref unreadable: {raw_path} ({exc.__class__.__name__})"


def _read_ref_context(refs: List[Dict[str, Any]], source_path: Optional[str],
                      line_cap: int = 200) -> Dict[str, Any]:
    resolved: List[Dict[str, Any]] = []
    warnings: List[str] = []
    remaining_lines = line_cap
    for ref in refs:
        path = str(ref.get("path") or "")
        l_start = int(ref.get("l_start") or 0)
        l_end = int(ref.get("l_end") or 0)
        ref_entry = {"path": path, "l_start": l_start, "l_end": l_end}
        label = ref.get("label")
        if label:
            ref_entry["label"] = label
        if remaining_lines <= 0:
            ref_entry["warning"] = "ref truncated by aggregate line cap"
            resolved.append(ref_entry)
            continue
        resolved_path, resolve_warning = resolve_ref_path(path, source_path)
        if resolve_warning:
            ref_entry["warning"] = resolve_warning
            warnings.append(resolve_warning)
            resolved.append(ref_entry)
            continue
        assert resolved_path is not None
        try:
            stat_result = resolved_path.stat()
            if not stat.S_ISREG(stat_result.st_mode):
                warning = f"ref unreadable: {path}:{l_start}-{l_end} (not a regular file)"
                ref_entry["warning"] = warning
                warnings.append(warning)
                resolved.append(ref_entry)
                continue
            if stat_result.st_size > _REF_READ_BYTE_CAP:
                warning = f"ref unreadable: {path}:{l_start}-{l_end} (file exceeds byte cap {_REF_READ_BYTE_CAP})"
                ref_entry["warning"] = warning
                warnings.append(warning)
                resolved.append(ref_entry)
                continue
            slice_lines: List[str] = []
            with resolved_path.open("r", encoding="utf-8") as handle:
                for line_no, line in enumerate(itertools.islice(handle, l_end), start=1):
                    if line_no >= l_start:
                        slice_lines.append(line.rstrip("\n"))
        except Exception as exc:
            warning = f"ref unreadable: {path}:{l_start}-{l_end} ({exc.__class__.__name__})"
            ref_entry["warning"] = warning
            warnings.append(warning)
            resolved.append(ref_entry)
            continue
        if not slice_lines and l_start > 0:
            warning = f"ref unreadable: {path}:{l_start}-{l_end} (start beyond file)"
            ref_entry["warning"] = warning
            warnings.append(warning)
            resolved.append(ref_entry)
            continue
        if not slice_lines:
            warning = f"ref unreadable: {path}:{l_start}-{l_end} (empty slice)"
            ref_entry["warning"] = warning
            warnings.append(warning)
            resolved.append(ref_entry)
            continue
        allowed = min(len(slice_lines), remaining_lines)
        ref_entry["content"] = "\n".join(slice_lines[:allowed])
        ref_entry["truncated"] = allowed < len(slice_lines)
        if ref_entry["truncated"]:
            ref_entry["warning"] = "ref truncated by aggregate line cap"
        remaining_lines -= allowed
        resolved.append(ref_entry)
    return {"refs": resolved, "warnings": warnings, "line_cap": line_cap}


def _condition_lookup(conditions: List[Dict[str, Any]], condition_id: str,
                      version: Optional[int] = None) -> Optional[Dict[str, Any]]:
    matches = [cond for cond in conditions if cond.get("id") == condition_id]
    if version is not None:
        matches = [cond for cond in matches if int(cond.get("version", 0)) == int(version)]
    if not matches:
        return None
    matches.sort(key=lambda item: int(item.get("version", 0)), reverse=True)
    return matches[0]


def _project_stop_reason_state(project: Dict[str, Any]) -> Dict[str, Any]:
    conditions = list(project.get("user_stop_conditions") or [])
    current = project.get("stop_reason_current")
    active_conditions = _active_conditions(conditions)
    deprecated_only = bool(conditions) and not active_conditions
    if not current:
        return {
            "valid": False,
            "orphaned": False,
            "deprecated_only": deprecated_only,
            "condition": None,
        }

    matched = _condition_lookup(
        active_conditions,
        str(current.get("condition_id") or ""),
        int(current.get("condition_version", 0) or 0),
    )
    if matched:
        return {
            "valid": True,
            "orphaned": False,
            "deprecated_only": deprecated_only,
            "condition": matched,
        }
    return {
        "valid": False,
        "orphaned": True,
        "deprecated_only": deprecated_only,
        "condition": None,
    }


def _append_history(existing: List[Dict[str, Any]], entry: Dict[str, Any]) -> str:
    payload = list(existing)
    payload.append(entry)
    return _json_encode(payload)


def _next_project_priority(supervisor: str, config: Optional[OrchConfig] = None) -> int:
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        record = session.run("""
            MATCH (p:OrchProject)
            WHERE coalesce(p.supervisor, '') = $supervisor
            RETURN max(coalesce(p.priority, 0)) AS max_priority
        """, supervisor=supervisor).single()
    current = record["max_priority"] if record and record["max_priority"] is not None else 0
    return int(current) + 1


def _project_record(project_id: str, config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        record = session.run("""
            MATCH (p:OrchProject {id: $project_id})
            RETURN p
        """, project_id=project_id).single()
    if not record:
        raise ProjectNotFoundError(f"Project {project_id} not found")
    return _decode_project_node(dict(record["p"]))


_ZERO_DEP_READY_CYPHER = """
MATCH (t:OrchTask {id: $task_id})
WHERE coalesce(t.owner, '') <> ''
  AND NOT EXISTS {
      MATCH (t)-[:DEPENDS_ON]->(:OrchTask)
  }
RETURN t.id AS task_id,
       t.owner AS owner,
       t.description AS description
"""


def _fleet_redis_connect():
    ensure_notify_importable()
    from identity import redis_connect  # type: ignore
    return redis_connect()


def _state_key(node_id: str, suffix: str) -> str:
    prefix = os.environ.get("NOTIFY_KEY_PREFIX", "taey")
    return f"{prefix}:{node_id}:{suffix}"


_STOP_BLOCK_CONVERGENCE_LIMIT = 3
# This TTL deliberately survives short-lived process restarts so a stop cycle
# can still converge after a crash/restart. The tradeoff is a stale marker/count
# may survive until expiry if a process dies mid-cycle.
_STOP_BLOCK_TTL_SECS = 3600
WAKE_ALLOW_STOP = "ALLOW_STOP"
_STOP_INPROGRESS_EXECUTOR = ThreadPoolExecutor(max_workers=1)
_STOP_INPROGRESS_REDIS_TIMEOUT_S = 0.2
_STOP_MARKER_EXECUTOR = ThreadPoolExecutor(max_workers=1)
_STOP_MARKER_REDIS_TIMEOUT_S = 0.2
_LOG = logging.getLogger(__name__)


def _send_wake(owner: str, body: str) -> None:
    cli = OrchConfig().notify_cli_path
    result = subprocess.run(
        [cli, owner, body, "--from", "orch-create", "--type", "wake", "--priority", "normal"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"{cli} failed")


def _wake_owner_for_zero_dep_task(task_id: str, cfg: OrchConfig) -> None:
    from .plan_readiness import _dedup_wake

    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        record = session.run(_ZERO_DEP_READY_CYPHER, task_id=task_id).single()

    if not record:
        return

    owner = record["owner"]
    redis_client = _fleet_redis_connect()
    if not redis_client.get(_state_key(owner, "idle")):
        return
    if redis_client.get(_state_key(owner, "current_task")):
        return
    if not _dedup_wake(redis_client, task_id):
        return

    body = (
        f"WAKE: task={record['task_id']} "
        f"(\"{(record.get('description') or '')[:80]}\") has zero dependencies "
        f"and is ready now. Pick it up with `taey-plan next` or dispatch a worker."
    )
    _send_wake(owner, body)


def _stop_block_marker_key(node_id: str) -> str:
    return _state_key(node_id, "stop_blocked_task")


def _stop_block_count_key(node_id: str) -> str:
    return _state_key(node_id, "stop_block_count")


def _session_pause_active(session_id: str, config: Optional[OrchConfig] = None) -> bool:
    from .config import get_redis_sync

    cfg = config or OrchConfig()
    r = get_redis_sync(cfg)
    return bool(r.exists(_state_key(session_id, "pause")))


def _resolve_supervisor_session(session_id: str, config: Optional[OrchConfig] = None) -> str:
    from .config import get_redis_sync

    cfg = config or OrchConfig()
    r = get_redis_sync(cfg)
    try:
        explicit = r.get(_state_key(session_id, "parent"))
    except Exception:
        explicit = None
    if explicit:
        return str(explicit)
    for suffix in ("-codex", "-gemini", "-grok", "-claude"):
        if session_id.endswith(suffix):
            base = session_id[: -len(suffix)]
            if base:
                return base
    return session_id


def _observed_stop_task_id(session_id: str, config: Optional[OrchConfig] = None) -> Optional[str]:
    from .config import get_redis_sync

    cfg = config or OrchConfig()
    r = get_redis_sync(cfg)
    raw = r.get(_state_key(session_id, "current_task"))
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    task_id = payload.get("task_id")
    return str(task_id) if task_id else None


def _task_blocked_on(task_id: Optional[str], config: Optional[OrchConfig] = None) -> Optional[str]:
    if not task_id:
        return None
    task = get_task(task_id, config=config)
    if not task:
        return None
    blocked_on = task.get("blocked_on")
    if blocked_on in (None, "", "null"):
        return None
    return str(blocked_on)


def _close_stale_ad_hoc_in_progress_tasks(session_id: str,
                                          config: Optional[OrchConfig] = None) -> List[str]:
    cfg = config or OrchConfig()
    live_task_id = _observed_stop_task_id(session_id, config=cfg)
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        result = session.run(
            """
            MATCH (:OrchProject {id: 'default'})-[:HAS_PHASE]->(:OrchPhase)-[:HAS_TASK]->(t:OrchTask)
            WHERE t.owner = $session_id
              AND t.status = 'in_progress'
              AND ($live_task_id IS NULL OR t.id <> $live_task_id)
            SET t.status = 'interrupted',
                t.blocked_on = NULL,
                t.result = 'stale ad-hoc in_progress reconciled: no live current_task',
                t.completed_by = NULL,
                t.completed_at = NULL,
                t.updated_at = datetime()
            RETURN t.id AS task_id
            """,
            session_id=session_id,
            live_task_id=live_task_id,
        )
        return [str(record["task_id"]) for record in result]


def _queue_block_reason(task_id: Optional[str], description: Optional[str]) -> str:
    task_id_value = task_id or "unknown-task"
    task_title = (description or "untitled task")[:80]
    return (
        "You have ready work and must continue, not stop. "
        f"Next task: {task_id_value} — {task_title}. "
        "Pick it up via taey-queue next / taey-plan next and do it. "
        "Do NOT stop until all supervised projects are completed or have a valid "
        "stop_reason matching a user_stop_condition. Setting blocked-on / "
        "waiting-on-worker is NOT a stop reason if parallel ready work exists."
    )


def _in_progress_block_reason(task_id: Optional[str], description: Optional[str]) -> str:
    task_id_value = task_id or "unknown-task"
    task_title = (description or "untitled task")[:80]
    return f"Finish in-progress task {task_id_value}: {task_title}."


def _stop_inprogress_enabled(session_id: str, config: Optional[OrchConfig] = None) -> bool:
    from .config import get_redis_sync

    cfg = config or OrchConfig()
    if str(os.environ.get("CF_STOP_INPROGRESS") or "").strip().lower() in {"1", "true", "yes", "on"}:
        allowed = {
            item.strip()
            for item in str(os.environ.get("CF_STOP_INPROGRESS_SESSIONS") or "").replace(";", ",").split(",")
            if item.strip()
        }
        if session_id in allowed:
            return True
    try:
        prefix = os.environ.get("NOTIFY_KEY_PREFIX", "taey")
        redis_client = get_redis_sync(cfg)
        future = _STOP_INPROGRESS_EXECUTOR.submit(
            redis_client.sismember,
            f"{prefix}:stop_inprogress_enabled",
            session_id,
        )
        return bool(future.result(timeout=_STOP_INPROGRESS_REDIS_TIMEOUT_S))
    except FuturesTimeoutError:
        return False
    except Exception:
        return False


def _redis_marker_call(fn, *args):
    future = _STOP_MARKER_EXECUTOR.submit(fn, *args)
    return future.result(timeout=_STOP_MARKER_REDIS_TIMEOUT_S)


def _record_stop_convergence_audit(session_id: str,
                                   decision: Dict[str, Any],
                                   marker_value: str,
                                   block_count: int,
                                   config: Optional[OrchConfig] = None) -> str:
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    audit_id = f"stopconv-{uuid.uuid4().hex[:12]}"
    current_work = get_session_current_work(session_id, config=cfg) or {}
    task_id = str(
        decision.get("task_id")
        or current_work.get("top_task_id")
        or ""
    ) or None
    task = get_task(task_id, config=cfg) if task_id else None
    blocked_on = _task_blocked_on(task_id, config=cfg)
    task_state_left_behind = {
        "session_id": session_id,
        "task_id": task_id,
        "task_status": task.get("status") if task else None,
        "task_owner": task.get("owner") if task else None,
        "blocked_on": blocked_on,
        "project_id": decision.get("project_id") or current_work.get("project_id"),
        "project_name": current_work.get("project_name"),
        "phase_id": decision.get("phase_id") or current_work.get("phase_id"),
        "phase_name": current_work.get("phase_name"),
        "task_priority": decision.get("task_priority") if "task_priority" in decision else (task.get("priority") if task else None),
        "task_title_short": decision.get("task_title_short") or current_work.get("top_task_desc") or (task.get("description") if task else None),
        "observed_stop_task_id": _observed_stop_task_id(session_id, config=cfg),
    }
    with driver.session(database=cfg.neo4j_db) as session:
        session.run(
            """
            OPTIONAL MATCH (t:OrchTask {id: $task_id})
            OPTIONAL MATCH (p:OrchProject {id: $project_id})
            CREATE (a:OrchStopConvergenceAudit {
                id: $audit_id,
                event_type: 'stop_converged_allow',
                session_id: $session_id,
                marker_value: $marker_value,
                convergence_count: $convergence_count,
                wake_type_before: $wake_type_before,
                reason_before: $reason_before,
                created_at: datetime($created_at),
                decision_before_json: $decision_before_json,
                task_state_left_behind_json: $task_state_left_behind_json,
                task_id: $task_id,
                project_id: $project_id,
                phase_id: $phase_id,
                task_status: $task_status,
                task_owner: $task_owner,
                blocked_on: $blocked_on,
                task_title_short: $task_title_short
            })
            FOREACH (_ IN CASE WHEN t IS NULL THEN [] ELSE [1] END |
                CREATE (a)-[:LEFT_TASK_STATE]->(t)
            )
            FOREACH (_ IN CASE WHEN p IS NULL THEN [] ELSE [1] END |
                CREATE (a)-[:UNDER_PROJECT]->(p)
            )
            """,
            audit_id=audit_id,
            session_id=session_id,
            marker_value=marker_value,
            convergence_count=int(block_count),
            wake_type_before=decision.get("wake_type"),
            reason_before=decision.get("reason"),
            created_at=_utc_now_iso(),
            decision_before_json=_json_encode(_normalize_value(dict(decision))),
            task_state_left_behind_json=_json_encode(task_state_left_behind),
            task_id=task_state_left_behind["task_id"],
            project_id=task_state_left_behind["project_id"],
            phase_id=task_state_left_behind["phase_id"],
            task_status=task_state_left_behind["task_status"],
            task_owner=task_state_left_behind["task_owner"],
            blocked_on=task_state_left_behind["blocked_on"],
            task_title_short=task_state_left_behind["task_title_short"],
        )
    return audit_id


def _reason_required_block_reason(active_conditions: list[dict[str, Any]]) -> str:
    labels = [str(cond.get("label")) for cond in active_conditions if cond.get("label")]
    labels_text = ", ".join(labels) if labels else "no active user_stop_conditions"
    return (
        "You are trying to stop with no ready work and no valid stop_reason. "
        "Either there IS work (re-check taey-plan next) or you must set a stop_reason "
        f"matching one of: {labels_text}. You cannot stop otherwise."
    )


def _raw_stop_decision(session_id: str,
                       config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    cfg = config or OrchConfig()
    supervisor = _resolve_supervisor_session(session_id, config=cfg)
    ready_owner = session_id
    if _session_pause_active(supervisor, config=cfg):
        return {"block": False, "reason": None, "wake_type": WAKE_ALLOW_STOP, "task_id": None}

    projects = sorted(
        get_session_supervised_projects(supervisor, config=cfg),
        key=lambda project: project.get("priority") if project.get("priority") is not None else 999999999,
    )

    for project in projects:
        status = str(project.get("status") or "active")
        if status == "completed":
            continue
        next_ready = get_session_next_ready(ready_owner, project_id=str(project.get("id")), config=cfg)
        if next_ready:
            task_id = next_ready.get("task_id") or next_ready.get("id")
            return {
                "block": True,
                "reason": _queue_block_reason(task_id, next_ready.get("description")),
                "wake_type": "WAKE_WITH_QUEUE",
                "task_id": task_id,
                "project_id": project.get("id"),
                "phase_id": next_ready.get("phase_id"),
                "task_priority": next_ready.get("priority"),
                "task_title_short": (str(next_ready.get("description") or "")[:80] or None),
            }

    if _stop_inprogress_enabled(session_id, config=cfg):
        current_work = get_session_current_work(session_id, config=cfg)
        current_task_id = current_work.get("top_task_id") if current_work else None
        blocked_on = _task_blocked_on(current_task_id, config=cfg)
        if current_task_id and not blocked_on:
            # This block is intentionally bounded by the wrapper-level
            # convergence release valve: after the same stop block is observed
            # three times in stop-hook context, get_session_stop_decision()
            # force-allows so sessions cannot wedge permanently.
            return {
                "block": True,
                "reason": _in_progress_block_reason(current_task_id, current_work.get("top_task_desc") if current_work else None),
                "wake_type": "WAKE_WITH_QUEUE",
                "task_id": current_task_id,
                "project_id": current_work.get("project_id") if current_work else None,
                "phase_id": current_work.get("phase_id") if current_work else None,
                "task_title_short": (str(current_work.get("top_task_desc") or "")[:80] or None) if current_work else None,
            }
        if current_task_id and blocked_on:
            return {
                "block": False,
                "reason": None,
                "wake_type": WAKE_ALLOW_STOP,
                "task_id": None,
                "blocked_on": blocked_on,
            }

    blocked_on = _task_blocked_on(_observed_stop_task_id(session_id, config=cfg), config=cfg)
    if blocked_on:
        return {
            "block": False,
            "reason": None,
            "wake_type": WAKE_ALLOW_STOP,
            "task_id": None,
            "blocked_on": blocked_on,
        }

    reason_required: Optional[Dict[str, Any]] = None
    for project in projects:
        status = str(project.get("status") or "active")
        if status == "completed":
            continue
        active_conditions = _active_conditions(list(project.get("user_stop_conditions") or []))
        stop_state = _project_stop_reason_state(project)
        if not active_conditions and project.get("user_stop_conditions"):
            continue
        if status == "stopped" and stop_state["valid"]:
            continue
        if stop_state["valid"]:
            continue
        if not active_conditions:
            continue
        if reason_required is None:
            reason_required = {
                "block": True,
                "reason": _reason_required_block_reason(active_conditions),
                "wake_type": "WAKE_REASON_REQUIRED",
                "task_id": None,
                "project_id": project.get("id"),
                "available_conditions": [
                    {
                        "condition_id": cond.get("id"),
                        "version": cond.get("version"),
                        "label": cond.get("label"),
                    }
                    for cond in active_conditions
                ],
            }

    if reason_required is not None:
        return reason_required
    return {"block": False, "reason": None, "wake_type": WAKE_ALLOW_STOP, "task_id": None}


def get_session_stop_decision(session_id: str,
                              stop_hook_active: bool = False,
                              config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    from .config import get_redis_sync

    cfg = config or OrchConfig()
    try:
        enforce_handoff = flags_for_session(session_id)["enforce"]
    except Exception:
        enforce_handoff = False

    try:
        decision = _raw_stop_decision(session_id, config=cfg)
    except Exception as exc:
        decision = {
            "block": False,
            "reason": "keystone stop decision unavailable; fail-open allow used.",
            "wake_type": WAKE_ALLOW_STOP,
            "task_id": None,
            "keystone_fail_open": {
                "session": session_id,
                "operation": "_raw_stop_decision",
                "exception_class": exc.__class__.__name__,
            },
        }

    if enforce_handoff and not decision.get("block"):
        observed_task_id = _observed_stop_task_id(session_id, config=cfg)
        try:
            validate_timeout_s = float(os.environ.get("CF_HANDOFF_VALIDATE_TIMEOUT_S", "0.2") or 0.2)
        except Exception:
            validate_timeout_s = 0.2
        try:
            hv_result = validate_stop_handoff(
                get_redis_sync(cfg),
                session_id,
                observed_task_id,
                prefix=os.environ.get("NOTIFY_KEY_PREFIX", "taey"),
                timeout_s=validate_timeout_s,
            )
        except Exception as exc:
            decision = dict(decision)
            decision["hv_fail_open"] = {
                "session": session_id,
                "operation": "validate_stop_handoff",
                "exception_class": exc.__class__.__name__,
                "handoff_id": observed_task_id,
            }
            _LOG.warning(
                "handoff validation fail-open for %s (%s): %s",
                session_id,
                observed_task_id,
                exc.__class__.__name__,
            )
        else:
            hv_state = hv_result.get("state")
            # Handoff-specific blocks bypass the convergence valve below. They
            # rely on the daemon/handoff retry machinery for bounded release.
            if hv_state in {"pending_unacked", "delivery_failed", "redispatch_requested"}:
                record = hv_result.get("record") or {}
                return {
                    "block": True,
                    "reason": "Explicit handoff has not produced a scoped receipt yet; stop is blocked until receipt_acked or bounded wake gives up.",
                    "wake_type": "WAKE_REASON_REQUIRED",
                    "task_id": observed_task_id,
                    "handoff_state": hv_state,
                    "target_session_id": record.get("target_session_id"),
                    "dispatcher_task_id": record.get("dispatcher_task_id"),
                    "delivery_failure_reason": record.get("delivery_failure_reason"),
                    "last_delivery_signal": record.get("last_delivery_signal"),
                    "delivery_signal_source": record.get("delivery_signal_source"),
                }
            if hv_state == "dead":
                return {
                    "block": False,
                    "reason": "handoff delivery failed after bounded retries; manual handling required.",
                    "wake_type": WAKE_ALLOW_STOP,
                    "task_id": None,
                    "handoff_state": "dead",
                }

    if not stop_hook_active:
        return decision

    marker_key = _stop_block_marker_key(session_id)
    count_key = _stop_block_count_key(session_id)
    try:
        r = get_redis_sync(cfg)
        if not decision.get("block"):
            _redis_marker_call(r.delete, marker_key, count_key)
            return decision

        marker_value = str(
            decision.get("task_id")
            or decision.get("project_id")
            or decision.get("wake_type")
            or "unknown"
        )
        previous_marker = _redis_marker_call(r.get, marker_key)
        if previous_marker is not None:
            previous_marker = str(previous_marker)
        if previous_marker == marker_value:
            try:
                block_count = int(_redis_marker_call(r.get, count_key) or 0) + 1
            except (TypeError, ValueError):
                block_count = 1
        else:
            block_count = 1

        _redis_marker_call(r.set, marker_key, marker_value, _STOP_BLOCK_TTL_SECS)
        _redis_marker_call(r.set, count_key, str(block_count), _STOP_BLOCK_TTL_SECS)
        decision["convergence_count"] = block_count
        if block_count >= _STOP_BLOCK_CONVERGENCE_LIMIT:
            audit_id = _record_stop_convergence_audit(
                session_id,
                dict(decision),
                marker_value,
                block_count,
                config=cfg,
            )
            decision["block"] = False
            decision["reason"] = None
            decision["wake_type"] = WAKE_ALLOW_STOP
            decision["task_id"] = None
            decision["converged_allow"] = True
            decision["convergence_audit_id"] = audit_id
            _redis_marker_call(r.delete, marker_key, count_key)
    except Exception as exc:
        decision = dict(decision)
        decision["convergence_marker_fail_open"] = {
            "session": session_id,
            "exception_class": exc.__class__.__name__,
        }
    return decision


def init_schema(config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    """Create orchestration schema (constraints + indexes). Idempotent."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    results = {"constraints": [], "indexes": [], "errors": []}

    try:
        with driver.session(database=cfg.neo4j_db) as session:
            for stmt in SCHEMA_CONSTRAINTS:
                try:
                    session.run(stmt)
                    results["constraints"].append(stmt.split("FOR")[0].strip())
                except Exception as e:
                    results["errors"].append(f"{stmt[:60]}: {e}")

            for stmt in SCHEMA_INDEXES:
                try:
                    session.run(stmt)
                    results["indexes"].append(stmt.split("FOR")[0].strip())
                except Exception as e:
                    results["errors"].append(f"{stmt[:60]}: {e}")
    finally:
        pass  # Driver is singleton; do not close

    return results


def create_project(project_id: str, name: str, description: str = "",
                   source_path: Optional[str] = None,
                   source_sha256: Optional[str] = None,
                   source_kind: Optional[str] = None,
                   ingested_at: Optional[str] = None,
                   ingested_by: Optional[str] = None,
                   refs: Optional[List[Dict[str, Any]]] = None,
                   user_stop_conditions: Optional[List[Any]] = None,
                   supervisor: Optional[str] = None,
                   priority: Optional[int] = None,
                   migration_exempt: bool = False,
                   config: Optional[OrchConfig] = None) -> str:
    """Create an OrchProject node."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    created_by = ingested_by or "unknown"
    supervisor_value = supervisor or _normalize_owner_session(created_by) or "unassigned"
    if (not migration_exempt) and supervisor_value == "unassigned":
        raise ValueError("supervisor must be non-empty and not 'unassigned' unless migration_exempt=true")
    priority_value = int(priority if priority is not None else _next_project_priority(supervisor_value, cfg))
    conditions_value = _normalize_user_stop_conditions(user_stop_conditions, created_by)
    priority_history = [{
        "priority_before": None,
        "priority_after": priority_value,
        "set_by": created_by,
        "set_at": _utc_now_iso(),
        "source_surface": "api" if source_kind == "api" else "system",
        "reason": "project created",
    }]
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            result = session.run("""
                MERGE (p:OrchProject {id: $id})
                ON CREATE SET p.created_at = datetime(), p.status = 'active'
                SET p.name = $name,
                    p.description = $description,
                    p.supervisor = $supervisor,
                    p.priority = $priority,
                    p.user_stop_conditions = CASE
                        WHEN $user_stop_conditions IS NULL THEN coalesce(p.user_stop_conditions, '[]')
                        ELSE $user_stop_conditions
                    END,
                    p.refs = CASE
                        WHEN $refs IS NULL THEN coalesce(p.refs, '[]')
                        ELSE $refs
                    END,
                    p.stop_reason_current = coalesce(p.stop_reason_current, ''),
                    p.stop_reason_history = coalesce(p.stop_reason_history, '[]'),
                    p.priority_history = CASE
                        WHEN p.priority_history IS NULL OR p.priority_history = '' THEN $priority_history
                        ELSE p.priority_history
                    END,
                    p.migration_exempt = coalesce(p.migration_exempt, $migration_exempt),
                    p.in_progress_heartbeat_at = coalesce(p.in_progress_heartbeat_at, ''),
                    p.source_path = CASE
                        WHEN $source_path IS NULL OR $source_path = '' THEN p.source_path
                        ELSE $source_path
                    END,
                    p.source_sha256 = CASE
                        WHEN $source_sha256 IS NULL OR $source_sha256 = '' THEN p.source_sha256
                        ELSE $source_sha256
                    END,
                    p.source_kind = CASE
                        WHEN $source_kind IS NULL OR $source_kind = '' THEN p.source_kind
                        ELSE $source_kind
                    END,
                    p.ingested_at = CASE
                        WHEN $ingested_at IS NULL OR $ingested_at = '' THEN p.ingested_at
                        ELSE datetime($ingested_at)
                    END,
                    p.ingested_by = CASE
                        WHEN $ingested_by IS NULL OR $ingested_by = '' THEN p.ingested_by
                        ELSE $ingested_by
                    END
                RETURN p.id AS id
            """,
                id=project_id,
                name=name,
                description=description,
                supervisor=supervisor_value,
                priority=priority_value,
                source_path=source_path,
                source_sha256=source_sha256,
                source_kind=source_kind,
                ingested_at=ingested_at,
                ingested_by=ingested_by,
                refs=_encode_refs_or_none(refs),
                user_stop_conditions=_json_encode(conditions_value),
                priority_history=_json_encode(priority_history),
                migration_exempt=bool(migration_exempt),
            )
            return result.single()["id"]
    finally:
        pass  # Driver is singleton; do not close


def create_phase(project_id: str, phase_id: str, name: str,
                 order: int = 0,
                 refs: Optional[List[Dict[str, Any]]] = None,
                 source_path: Optional[str] = None,
                 config: Optional[OrchConfig] = None) -> str:
    """Create an OrchPhase linked to a project."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            result = session.run("""
                MATCH (p:OrchProject {id: $project_id})
                MERGE (ph:OrchPhase {id: $phase_id})
                ON CREATE SET ph.created_at = datetime(), ph.status = 'pending'
                SET ph.name = $name,
                    ph.order = $order,
                    ph.refs = CASE
                        WHEN $refs IS NULL THEN coalesce(ph.refs, '[]')
                        ELSE $refs
                    END,
                    ph.source_path = CASE
                        WHEN $source_path IS NULL OR $source_path = '' THEN ph.source_path
                        ELSE $source_path
                    END
                MERGE (p)-[:HAS_PHASE]->(ph)
                RETURN ph.id AS id
            """, project_id=project_id, phase_id=phase_id, name=name, order=order,
                 refs=_encode_refs_or_none(refs), source_path=source_path)
            return result.single()["id"]
    finally:
        pass  # Driver is singleton; do not close


def create_task(
    phase_id: str,
    task_id: str,
    description: str,
    priority: int = 50,
    owner: str = "",
    created_by: str = "",
    task_type: str = "standard",
    refs: Optional[List[Dict[str, Any]]] = None,
    source_path: Optional[str] = None,
    capability_tags: Optional[List[str]] = None,
    file_blast_radius: Optional[List[str]] = None,
    estimated_tokens: int = 50_000,
    heartbeat_exempt_secs: Optional[int] = None,
    wake_owner_if_ready: bool = True,
    config: Optional[OrchConfig] = None,
) -> str:
    """Create an OrchTask linked to a phase."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            result = session.run("""
                MATCH (ph:OrchPhase {id: $phase_id})
                MERGE (t:OrchTask {id: $task_id})
                ON CREATE SET t.created_at = datetime(),
                              t.status = 'pending',
                              t.owner = $owner,
                              t.forced_continuation_count = 0
                SET t.description = $description,
                    t.priority = $priority,
                    t.owner = $owner,
                    t.created_by = CASE
                        WHEN $created_by = '' THEN t.created_by
                        ELSE $created_by
                    END,
                    t.task_type = CASE
                        WHEN $task_type = '' THEN t.task_type
                        ELSE $task_type
                    END,
                    t.refs = CASE
                        WHEN $refs IS NULL THEN coalesce(t.refs, '[]')
                        ELSE $refs
                    END,
                    t.source_path = CASE
                        WHEN $source_path IS NULL OR $source_path = '' THEN t.source_path
                        ELSE $source_path
                    END,
                    t.capability_tags = $capability_tags,
                    t.file_blast_radius = $file_blast_radius,
                    t.estimated_tokens = $estimated_tokens,
                    t.heartbeat_exempt_secs = $heartbeat_exempt_secs
                MERGE (ph)-[:HAS_TASK]->(t)
                RETURN t.id AS id
            """,
                task_id=task_id,
                phase_id=phase_id,
                description=description,
                priority=priority,
                owner=owner,
                created_by=created_by,
                task_type=task_type,
                refs=_encode_refs_or_none(refs),
                source_path=source_path,
                capability_tags=capability_tags or [],
                file_blast_radius=file_blast_radius or [],
                estimated_tokens=estimated_tokens,
                heartbeat_exempt_secs=heartbeat_exempt_secs,
            )
            created_id = result.single()["id"]
        if wake_owner_if_ready:
            _wake_owner_for_zero_dep_task(created_id, cfg)
        return created_id
    finally:
        pass  # Driver is singleton; do not close


def add_dependency(task_id: str, depends_on_id: str,
                   config: Optional[OrchConfig] = None) -> bool:
    """Create DEPENDS_ON relationship between tasks."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            session.run("""
                MATCH (t:OrchTask {id: $task_id})
                MATCH (dep:OrchTask {id: $depends_on_id})
                MERGE (t)-[:DEPENDS_ON]->(dep)
            """, task_id=task_id, depends_on_id=depends_on_id)
            return True
    finally:
        pass  # Driver is singleton; do not close


def get_ready_tasks(config: Optional[OrchConfig] = None) -> List[Dict[str, Any]]:
    """Get tasks that are pending with all dependencies satisfied."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            result = session.run("""
                MATCH (t:OrchTask {status: 'pending'})
                WHERE NOT EXISTS {
                    MATCH (t)-[:DEPENDS_ON]->(dep:OrchTask)
                    WHERE dep.status <> 'completed'
                }
                RETURN t.id AS id, t.description AS description,
                       t.priority AS priority, t.owner AS owner,
                       t.capability_tags AS capability_tags,
                       t.file_blast_radius AS file_blast_radius,
                       t.estimated_tokens AS estimated_tokens
                ORDER BY coalesce(t.priority, 999999999) ASC
            """)
            return [dict(r) for r in result]
    finally:
        pass  # Driver is singleton; do not close


def update_task_status(task_id: str, status: str, owner: str = "",
                       result: Optional[str] = None,
                       blocked_on: Optional[str] = None,
                       completion_evidence: Optional[Dict[str, Any]] = None,
                       completed_by: Optional[str] = None,
                       config: Optional[OrchConfig] = None) -> bool:
    """Update task status, owner, and optional result."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    blocked_on_value = "__KEEP__" if blocked_on is None else blocked_on
    if status == "completed" and completion_evidence is None:
        raise CompletionEvidenceError(
            "completed status requires evidence with at least one of: commit_sha, gate_run_id, production_observation"
        )
    completion_evidence_value = _normalize_completion_evidence(completion_evidence) if status == "completed" else None
    if status != "completed" and completion_evidence is not None:
        raise CompletionEvidenceError("completion evidence is only valid on a completed transition")
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            if result is None:
                rec = session.run("""
                    MATCH (t:OrchTask {id: $task_id})
                    SET t.status = $status, t.owner = $owner,
                        t.blocked_on = CASE
                            WHEN $status <> 'in_progress' THEN NULL
                            WHEN $blocked_on = '__KEEP__' THEN t.blocked_on
                            WHEN $blocked_on = '' THEN NULL
                            ELSE $blocked_on
                        END,
                        t.forced_continuation_count = CASE
                            WHEN t.status <> $status THEN 0
                            WHEN coalesce(t.blocked_on, '') <> coalesce(
                                CASE
                                    WHEN $status <> 'in_progress' THEN NULL
                                    WHEN $blocked_on = '__KEEP__' THEN t.blocked_on
                                    WHEN $blocked_on = '' THEN NULL
                                    ELSE $blocked_on
                                END,
                                ''
                            ) THEN 0
                            ELSE coalesce(t.forced_continuation_count, 0)
                        END,
                        t.completion_evidence = CASE
                            WHEN $status = 'completed' THEN $completion_evidence
                            ELSE NULL
                        END,
                        t.completed_by = CASE
                            WHEN $status = 'completed' THEN $completed_by
                            ELSE NULL
                        END,
                        t.completed_at = CASE
                            WHEN $status = 'completed' THEN datetime()
                            ELSE NULL
                        END,
                        t.updated_at = datetime()
                    RETURN t.id AS id
                """, task_id=task_id, status=status, owner=owner, blocked_on=blocked_on_value,
                     completion_evidence=_json_encode(completion_evidence_value) if completion_evidence_value else None,
                     completed_by=completed_by or owner or "")
            else:
                rec = session.run("""
                    MATCH (t:OrchTask {id: $task_id})
                    SET t.status = $status, t.owner = $owner,
                        t.result = $result,
                        t.blocked_on = CASE
                            WHEN $status <> 'in_progress' THEN NULL
                            WHEN $blocked_on = '__KEEP__' THEN t.blocked_on
                            WHEN $blocked_on = '' THEN NULL
                            ELSE $blocked_on
                        END,
                        t.forced_continuation_count = CASE
                            WHEN t.status <> $status THEN 0
                            WHEN coalesce(t.blocked_on, '') <> coalesce(
                                CASE
                                    WHEN $status <> 'in_progress' THEN NULL
                                    WHEN $blocked_on = '__KEEP__' THEN t.blocked_on
                                    WHEN $blocked_on = '' THEN NULL
                                    ELSE $blocked_on
                                END,
                                ''
                            ) THEN 0
                            ELSE coalesce(t.forced_continuation_count, 0)
                        END,
                        t.completion_evidence = CASE
                            WHEN $status = 'completed' THEN $completion_evidence
                            ELSE NULL
                        END,
                        t.completed_by = CASE
                            WHEN $status = 'completed' THEN $completed_by
                            ELSE NULL
                        END,
                        t.completed_at = CASE
                            WHEN $status = 'completed' THEN datetime()
                            ELSE NULL
                        END,
                        t.updated_at = datetime()
                    RETURN t.id AS id
                """, task_id=task_id, status=status, owner=owner, result=result,
                     blocked_on=blocked_on_value,
                     completion_evidence=_json_encode(completion_evidence_value) if completion_evidence_value else None,
                     completed_by=completed_by or owner or "")
            if rec.single() is None:
                return False
            session.run("""
                MATCH (p:OrchProject)-[:HAS_PHASE]->(:OrchPhase)-[:HAS_TASK]->(t:OrchTask {id: $task_id})
                SET p.in_progress_heartbeat_at = CASE
                        WHEN $status = 'in_progress' THEN datetime()
                        WHEN $status IN ['completed', 'failed', 'interrupted'] THEN ''
                        ELSE p.in_progress_heartbeat_at
                    END,
                    p.status = CASE
                        WHEN $status = 'in_progress' THEN 'in_progress'
                        WHEN $status IN ['completed', 'failed', 'interrupted'] AND p.status = 'in_progress' THEN 'active'
                        ELSE p.status
                    END,
                    p.updated_at = datetime()
            """, task_id=task_id, status=status)
            return True
    finally:
        pass  # Driver is singleton; do not close


def get_task(task_id: str,
             config: Optional[OrchConfig] = None) -> Optional[Dict[str, Any]]:
    """Return one OrchTask node as a plain dict."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            result = session.run("""
                MATCH (t:OrchTask {id: $task_id})
                RETURN t
            """, task_id=task_id).single()
            if not result:
                return None
            task = _normalize_map(dict(result["t"]))
            task["forced_continuation_count"] = int(task.get("forced_continuation_count", 0) or 0)
            task["completion_evidence"] = _decode_json_field(task.get("completion_evidence"), None)
            return _attach_ref_runtime(task, source_path=task.get("source_path"))
    finally:
        pass  # Driver is singleton; do not close


def check_phase_complete(phase_id: str,
                         config: Optional[OrchConfig] = None) -> bool:
    """Check if all tasks in a phase are completed. If so, mark phase completed."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            result = session.run("""
                MATCH (ph:OrchPhase {id: $phase_id})-[:HAS_TASK]->(t:OrchTask)
                WITH ph,
                     count(t) AS total,
                     sum(CASE WHEN t.status = 'completed' THEN 1 ELSE 0 END) AS done
                WHERE total > 0 AND total = done AND ph.status <> 'completed'
                SET ph.status = 'completed', ph.completed_at = datetime()
                RETURN ph.id AS id
            """, phase_id=phase_id)
            rec = result.single()
            return rec is not None
    finally:
        pass  # Driver is singleton; do not close


def get_task_phase(task_id: str,
                   config: Optional[OrchConfig] = None) -> Optional[str]:
    """Get the phase ID for a given task."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            result = session.run("""
                MATCH (ph:OrchPhase)-[:HAS_TASK]->(t:OrchTask {id: $task_id})
                RETURN ph.id AS phase_id
            """, task_id=task_id)
            rec = result.single()
            return rec["phase_id"] if rec else None
    finally:
        pass  # Driver is singleton; do not close


def assign_task_to_phase(task_id: str, phase_id: str,
                         config: Optional[OrchConfig] = None) -> bool:
    """Ensure a task belongs to exactly one phase, re-parenting when needed."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            result = session.run("""
                MATCH (t:OrchTask {id: $task_id})
                MATCH (ph:OrchPhase {id: $phase_id})
                OPTIONAL MATCH (:OrchPhase)-[rel:HAS_TASK]->(t)
                DELETE rel
                MERGE (ph)-[:HAS_TASK]->(t)
                RETURN t.id AS task_id
            """, task_id=task_id, phase_id=phase_id)
            return result.single() is not None
    finally:
        pass  # Driver is singleton; do not close


def _normalize_value(value: Any) -> Any:
    iso = getattr(value, "iso_format", None)
    if callable(iso):
        return iso()
    if isinstance(value, list):
        return [_normalize_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize_value(item) for key, item in value.items()}
    return value


def _normalize_map(values: Dict[str, Any]) -> Dict[str, Any]:
    return {key: _normalize_value(value) for key, value in values.items()}


def _attach_ref_runtime(record: Dict[str, Any], *, source_path: Optional[str]) -> Dict[str, Any]:
    refs = _normalize_refs(record.get("refs"))
    record["refs"] = refs
    if not refs:
        record["ref_context"] = {"refs": [], "warnings": [], "line_cap": 200}
        return record
    record["ref_context"] = _read_ref_context(refs, source_path=source_path, line_cap=200)
    return record


def get_project_summary(project_id: str,
                        config: Optional[OrchConfig] = None) -> Optional[Dict[str, Any]]:
    """Return a project with its phases, tasks, and per-phase task status counts."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            result = session.run("""
                MATCH (p:OrchProject {id: $project_id})
                OPTIONAL MATCH (p)-[:HAS_PHASE]->(ph:OrchPhase)
                OPTIONAL MATCH (ph)-[:HAS_TASK]->(t:OrchTask)
                WITH p, ph, t
                ORDER BY coalesce(t.priority, 999999999) ASC, t.created_at ASC
                WITH p, ph,
                     count(t) AS total_tasks,
                     sum(CASE WHEN t.status = 'pending' THEN 1 ELSE 0 END) AS pending,
                     sum(CASE WHEN t.status = 'in_progress' THEN 1 ELSE 0 END) AS in_progress,
                     sum(CASE WHEN t.status = 'completed' THEN 1 ELSE 0 END) AS completed,
                     sum(CASE WHEN t.status = 'failed' THEN 1 ELSE 0 END) AS failed,
                     collect(
                         CASE
                             WHEN t IS NULL THEN NULL
                             ELSE {
                                 id: t.id,
                                 description: t.description,
                                 status: t.status,
                                 owner: t.owner,
                                 priority: t.priority,
                                 blocked_on: t.blocked_on,
                                 refs: t.refs,
                                 source_path: t.source_path
                             }
                         END
                     ) AS tasks
                ORDER BY ph.order ASC, ph.name ASC
                RETURN p, collect(
                    CASE
                        WHEN ph IS NULL THEN NULL
                        ELSE {
                            phase: ph,
                            task_counts: {
                                total: total_tasks,
                                pending: pending,
                                in_progress: in_progress,
                                completed: completed,
                                failed: failed
                            },
                            tasks: tasks
                        }
                    END
                ) AS phases
            """, project_id=project_id)
            record = result.single()
            if not record:
                return None

            project = _decode_project_node(dict(record["p"]))
            phases = []
            for item in record["phases"]:
                if item is None:
                    continue
                phase = _normalize_map(dict(item["phase"]))
                phase = _attach_ref_runtime(phase, source_path=phase.get("source_path") or project.get("source_path"))
                tasks = []
                for task in item["tasks"]:
                    if task is None:
                        continue
                    task_row = _normalize_map(dict(task))
                    tasks.append(_attach_ref_runtime(
                        task_row,
                        source_path=task_row.get("source_path") or phase.get("source_path") or project.get("source_path"),
                    ))
                phases.append({
                    "phase": phase,
                    "task_counts": dict(item["task_counts"]),
                    "tasks": tasks,
                })

            project = _attach_ref_runtime(project, source_path=project.get("source_path"))
            return {
                "project": project,
                "phases": phases,
            }
    finally:
        pass  # Driver is singleton; do not close


def get_session_current_work(session_id: str,
                             config: Optional[OrchConfig] = None) -> Optional[Dict[str, Any]]:
    """Return the highest-priority in-progress task for a session with project context."""
    cfg = config or OrchConfig()
    _close_stale_ad_hoc_in_progress_tasks(session_id, config=cfg)
    driver = get_neo4j_driver(cfg)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            result = session.run("""
                MATCH (p:OrchProject)-[:HAS_PHASE]->(ph:OrchPhase)-[:HAS_TASK]->(t:OrchTask)
                WHERE t.owner = $session_id AND t.status = 'in_progress'
                RETURN p.id AS project_id,
                       p.name AS project_name,
                       p.source_path AS project_source_path,
                       p.refs AS project_refs,
                       ph.id AS phase_id,
                       ph.name AS phase_name,
                       ph.source_path AS phase_source_path,
                       ph.refs AS phase_refs,
                       t.id AS top_task_id,
                       t.description AS top_task_desc,
                       t.source_path AS task_source_path,
                       t.refs AS task_refs
                ORDER BY coalesce(p.priority, 999999999) ASC, coalesce(t.priority, 999999999) ASC, ph.order ASC, t.created_at ASC
                LIMIT 1
            """, session_id=session_id)
            record = result.single()
            if not record:
                return None
            result = dict(record)
            result["project_ref_context"] = _read_ref_context(
                _normalize_refs(result.get("project_refs")),
                source_path=result.get("project_source_path"),
                line_cap=200,
            )
            result["phase_ref_context"] = _read_ref_context(
                _normalize_refs(result.get("phase_refs")),
                source_path=result.get("phase_source_path") or result.get("project_source_path"),
                line_cap=200,
            )
            result["task_ref_context"] = _read_ref_context(
                _normalize_refs(result.get("task_refs")),
                source_path=result.get("task_source_path") or result.get("phase_source_path") or result.get("project_source_path"),
                line_cap=200,
            )
            result["project_refs"] = _normalize_refs(result.get("project_refs"))
            result["phase_refs"] = _normalize_refs(result.get("phase_refs"))
            result["task_refs"] = _normalize_refs(result.get("task_refs"))
            return result
    finally:
        pass  # Driver is singleton; do not close


def get_session_next_ready(session_id: str, exclude_task_id: Optional[str] = None,
                           project_id: Optional[str] = None,
                           config: Optional[OrchConfig] = None) -> Optional[Dict[str, Any]]:
    """Return the top ready task for a session, excluding a specific task if requested."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            result = session.run("""
                MATCH (proj:OrchProject)-[:HAS_PHASE]->(ph:OrchPhase)-[:HAS_TASK]->(t:OrchTask)
                WHERE t.status = 'pending'
                  AND coalesce(t.owner, '') = $sess
                  AND coalesce(t.blocked_on, '') = ''
                  AND ($exclude_task_id IS NULL OR t.id <> $exclude_task_id)
                  AND ($project_id IS NULL OR proj.id = $project_id)
                  AND NOT EXISTS {
                      MATCH (t)-[:DEPENDS_ON]->(dep:OrchTask)
                      WHERE dep.status <> 'completed'
                  }
                  AND coalesce(proj.status, 'active') <> 'stopped'
                  AND coalesce(proj.status, 'active') <> 'completed'
                RETURN t.id AS task_id, t.description AS description,
                       t.priority AS priority, t.owner AS owner,
                       t.blocked_on AS blocked_on,
                       t.refs AS task_refs,
                       t.source_path AS task_source_path,
                       ph.id AS phase_id, ph.name AS phase_name,
                       ph.refs AS phase_refs, ph.source_path AS phase_source_path,
                       proj.id AS project_id, proj.name AS project_name,
                       proj.refs AS project_refs, proj.source_path AS project_source_path
                ORDER BY toInteger(coalesce(proj.priority, 999999999)) ASC,
                         toInteger(coalesce(t.priority, 999999999)) ASC,
                         t.created_at ASC
                LIMIT 1
            """, sess=session_id, exclude_task_id=exclude_task_id, project_id=project_id).single()
            if not result:
                return None
            row = dict(result)
            row["project_ref_context"] = _read_ref_context(
                _normalize_refs(row.get("project_refs")),
                source_path=row.get("project_source_path"),
                line_cap=200,
            )
            row["phase_ref_context"] = _read_ref_context(
                _normalize_refs(row.get("phase_refs")),
                source_path=row.get("phase_source_path") or row.get("project_source_path"),
                line_cap=200,
            )
            row["task_ref_context"] = _read_ref_context(
                _normalize_refs(row.get("task_refs")),
                source_path=row.get("task_source_path") or row.get("phase_source_path") or row.get("project_source_path"),
                line_cap=200,
            )
            row["project_refs"] = _normalize_refs(row.get("project_refs"))
            row["phase_refs"] = _normalize_refs(row.get("phase_refs"))
            row["task_refs"] = _normalize_refs(row.get("task_refs"))
            return row
    finally:
        pass  # Driver is singleton; do not close


def get_project_user_stop_conditions(project_id: str,
                                     config: Optional[OrchConfig] = None) -> Optional[List[Dict[str, Any]]]:
    """Return the project's configured versioned user-stop conditions."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            record = session.run("""
                MATCH (p:OrchProject {id: $project_id})
                RETURN p.user_stop_conditions AS user_stop_conditions
            """, project_id=project_id).single()
            if not record:
                return None
            return _normalize_user_stop_conditions(
                _decode_json_field(record["user_stop_conditions"], []),
                created_by="decoded",
            )
    finally:
        pass  # Driver is singleton; do not close


def set_project_user_stop_conditions(project_id: str, user_stop_conditions: List[Any],
                                     config: Optional[OrchConfig] = None,
                                     created_by: str = "unknown") -> List[Dict[str, Any]]:
    """Persist the project's configured versioned user-stop conditions."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    normalized = _normalize_user_stop_conditions(user_stop_conditions, created_by)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            record = session.run("""
                MATCH (p:OrchProject {id: $project_id})
                SET p.user_stop_conditions = $user_stop_conditions,
                    p.updated_at = datetime()
                RETURN p.user_stop_conditions AS user_stop_conditions
            """, project_id=project_id, user_stop_conditions=_json_encode(normalized)).single()
            if not record:
                raise ValueError(f"Project {project_id} not found")
            return _normalize_user_stop_conditions(
                _decode_json_field(record["user_stop_conditions"], []),
                created_by="decoded",
            )
    finally:
        pass  # Driver is singleton; do not close


def get_task_project(task_id: str,
                     config: Optional[OrchConfig] = None) -> Optional[Dict[str, Any]]:
    """Return the project context for a task."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            record = session.run("""
                MATCH (proj:OrchProject)-[:HAS_PHASE]->(:OrchPhase)-[:HAS_TASK]->(t:OrchTask {id: $task_id})
                RETURN proj.id AS project_id, proj.name AS project_name,
                       proj.user_stop_conditions AS user_stop_conditions
            """, task_id=task_id).single()
            if not record:
                return None
            result = dict(record)
            conditions = _normalize_user_stop_conditions(
                _decode_json_field(result.get("user_stop_conditions"), []),
                created_by="decoded",
            )
            result["user_stop_conditions"] = [cond["label"] for cond in _active_conditions(conditions)]
            return result
    finally:
        pass  # Driver is singleton; do not close


def ensure_default_project(config: Optional[OrchConfig] = None) -> str:
    """Ensure a default project and phase exist for ad-hoc tasks. Returns phase_id."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    default_supervisor = "system"
    default_priority = _next_project_priority(default_supervisor, cfg)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            session.run("""
                MERGE (p:OrchProject {id: 'default'})
                ON CREATE SET p.name = 'Default Project',
                              p.description = 'Auto-created for ad-hoc tasks',
                              p.created_at = datetime(), p.status = 'active'
                SET p.supervisor = coalesce(p.supervisor, $default_supervisor),
                    p.priority = coalesce(p.priority, $priority),
                    p.migration_exempt = coalesce(p.migration_exempt, false),
                    p.user_stop_conditions = coalesce(p.user_stop_conditions, '[]'),
                    p.stop_reason_current = coalesce(p.stop_reason_current, ''),
                    p.stop_reason_history = coalesce(p.stop_reason_history, '[]'),
                    p.priority_history = coalesce(
                        p.priority_history,
                        $priority_history
                    )
                MERGE (ph:OrchPhase {id: 'default-main'})
                ON CREATE SET ph.name = 'Main', ph.order = 0, ph.status = 'active'
                MERGE (p)-[:HAS_PHASE]->(ph)
            """,
                default_supervisor=default_supervisor,
                priority=default_priority,
                priority_history=_json_encode([{
                    "priority_before": None,
                    "priority_after": default_priority,
                    "set_by": "system",
                    "set_at": _utc_now_iso(),
                    "source_surface": "system",
                    "reason": "default project bootstrap",
                }]),
            )
        return "default-main"
    finally:
        pass  # Driver is singleton; do not close


def get_session_supervised_projects(session_id: str,
                                    config: Optional[OrchConfig] = None) -> List[Dict[str, Any]]:
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            result = session.run("""
                MATCH (p:OrchProject)
                WHERE coalesce(p.supervisor, '') = $session_id
                  AND coalesce(p.migration_exempt, false) = false
                RETURN p
                ORDER BY coalesce(p.priority, 999999999) ASC, p.created_at ASC
            """, session_id=session_id)
            return [_decode_project_node(dict(record["p"])) for record in result]
    finally:
        pass


def get_project_ready_tasks(project_id: str, owner: Optional[str] = None,
                            config: Optional[OrchConfig] = None) -> List[Dict[str, Any]]:
    cfg = config or OrchConfig()
    project = _project_record(project_id, cfg)
    stop_state = _project_stop_reason_state(project)
    if project.get("status") == "completed":
        return []
    if project.get("status") == "stopped" and stop_state["valid"]:
        return []
    if stop_state["valid"]:
        return []
    owner_value = owner if owner is not None else str(project.get("supervisor") or "")
    if not owner_value:
        return []
    driver = get_neo4j_driver(cfg)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            result = session.run("""
                MATCH (p:OrchProject {id: $project_id})-[:HAS_PHASE]->(ph:OrchPhase)-[:HAS_TASK]->(t:OrchTask)
                WHERE t.status = 'pending'
                  AND coalesce(t.owner, '') = $owner
                  AND coalesce(t.blocked_on, '') = ''
                  AND NOT EXISTS {
                      MATCH (t)-[:DEPENDS_ON]->(dep:OrchTask)
                      WHERE dep.status <> 'completed'
                  }
                RETURN t.id AS id,
                       t.description AS description,
                       t.priority AS priority,
                       t.owner AS owner,
                       t.forced_continuation_count AS forced_continuation_count,
                       t.task_type AS task_type,
                       t.required_credentials AS required_credentials,
                       t.credentials_available AS credentials_available,
                       t.permissions_available AS permissions_available,
                       ph.id AS phase_id,
                       ph.name AS phase_name
                ORDER BY coalesce(t.priority, 999999999) ASC, t.created_at ASC
            """, project_id=project_id, owner=owner_value)
            ready = []
            for record in result:
                task = _normalize_map(dict(record))
                task["forced_continuation_count"] = int(task.get("forced_continuation_count", 0) or 0)
                required = task.get("required_credentials")
                if required:
                    if not bool(task.get("credentials_available")) and not bool(task.get("permissions_available")):
                        continue
                ready.append(task)
            return ready
    finally:
        pass


def ready_work(project_id: str, session_id: Optional[str] = None,
               config: Optional[OrchConfig] = None) -> List[Dict[str, Any]]:
    return get_project_ready_tasks(project_id, owner=session_id, config=config)


def set_project_stop_reason(project_id: str, condition_id: str, condition_version: int,
                            detail: str, set_by: str,
                            config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    cfg = config or OrchConfig()
    project = _project_record(project_id, cfg)
    if ready_work(project_id, session_id=str(project.get("supervisor") or ""), config=cfg):
        raise ReadyWorkConflictError(f"ready_work exists for project {project_id}")
    conditions = list(project.get("user_stop_conditions") or [])
    condition = _condition_lookup(conditions, condition_id, condition_version)
    if not condition:
        raise ConditionValidationError("condition_id/version not found")
    if condition.get("deprecated_at"):
        raise ConditionValidationError("condition_id is deprecated")
    entry = {
        "condition_id": condition["id"],
        "condition_version": int(condition["version"]),
        "label_snapshot": condition["label"],
        "detail": detail or "",
        "set_at": _utc_now_iso(),
        "set_by": set_by,
    }
    history_entry = {
        "action": "set",
        "condition_id": condition["id"],
        "condition_version": int(condition["version"]),
        "label_snapshot": condition["label"],
        "detail": detail or "",
        "set_by": set_by,
        "set_at": entry["set_at"],
    }
    driver = get_neo4j_driver(cfg)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            record = session.run("""
                MATCH (p:OrchProject {id: $project_id})
                SET p.stop_reason_current = $stop_reason_current,
                    p.stop_reason_history = $stop_reason_history,
                    p.status = 'stopped',
                    p.updated_at = datetime()
                RETURN p.stop_reason_current AS stop_reason_current
            """,
                project_id=project_id,
                stop_reason_current=_json_encode(entry),
                stop_reason_history=_append_history(list(project.get("stop_reason_history") or []), history_entry),
            ).single()
            return _decode_json_field(record["stop_reason_current"], {})
    finally:
        pass


def clear_project_stop_reason(project_id: str, cleared_by: str,
                              config: Optional[OrchConfig] = None) -> bool:
    cfg = config or OrchConfig()
    project = _project_record(project_id, cfg)
    current = project.get("stop_reason_current") or {}
    history_entry = {
        "action": "clear",
        "condition_id": current.get("condition_id"),
        "condition_version": current.get("condition_version"),
        "label_snapshot": current.get("label_snapshot"),
        "detail": current.get("detail", ""),
        "cleared_by": cleared_by,
        "cleared_at": _utc_now_iso(),
    }
    driver = get_neo4j_driver(cfg)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            record = session.run("""
                MATCH (p:OrchProject {id: $project_id})
                SET p.stop_reason_current = '',
                    p.stop_reason_history = $stop_reason_history,
                    p.status = CASE
                        WHEN p.status = 'stopped' THEN 'active'
                        ELSE p.status
                    END,
                    p.updated_at = datetime()
                RETURN p.id AS id
            """,
                project_id=project_id,
                stop_reason_history=_append_history(list(project.get("stop_reason_history") or []), history_entry),
            ).single()
            return record is not None
    finally:
        pass


def complete_project(project_id: str, *, force: bool = False,
                     completed_by: str = "unknown",
                     config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            record = session.run("""
                MATCH (p:OrchProject {id: $project_id})
                WHERE $force OR NOT EXISTS {
                    MATCH (p)-[:HAS_PHASE]->(:OrchPhase)-[:HAS_TASK]->(t:OrchTask)
                    WHERE coalesce(t.status, 'pending') <> 'completed'
                }
                SET p.status = 'completed',
                    p.completed_at = datetime(),
                    p.updated_at = datetime()
                RETURN p.id AS id
            """, project_id=project_id, force=bool(force)).single()
            if not record:
                _project_record(project_id, cfg)
                raise ReadyWorkConflictError(f"project {project_id} has incomplete tasks")
        return {"ok": True, "project_id": project_id, "status": "completed", "force": bool(force), "completed_by": completed_by}
    finally:
        pass


def reset_project(project_id: str, *, reset_by: str = "unknown",
                  config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    cfg = config or OrchConfig()
    project = _project_record(project_id, cfg)
    driver = get_neo4j_driver(cfg)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            session.run("""
                MATCH (p:OrchProject {id: $project_id})
                SET p.status = 'active',
                    p.completed_at = NULL,
                    p.in_progress_heartbeat_at = '',
                    p.stop_reason_current = '',
                    p.stop_reason_history = '[]',
                    p.updated_at = datetime()
            """, project_id=project_id)
            session.run("""
                MATCH (p:OrchProject {id: $project_id})
                OPTIONAL MATCH (p)-[:HAS_PHASE]->(ph:OrchPhase)
                SET ph.status = 'pending',
                    ph.completed_at = NULL,
                    ph.updated_at = datetime()
            """, project_id=project_id)
            session.run("""
                MATCH (p:OrchProject {id: $project_id})
                OPTIONAL MATCH (p)-[:HAS_PHASE]->(:OrchPhase)-[:HAS_TASK]->(t:OrchTask)
                SET t.status = 'pending',
                    t.blocked_on = NULL,
                    t.result = NULL,
                    t.forced_continuation_count = 0,
                    t.updated_at = datetime()
            """, project_id=project_id)
        return {
            "ok": True,
            "project_id": project_id,
            "status": "active",
            "reset_by": reset_by,
            "cleared_sessions": [],
            "previous_stop_reason": project.get("stop_reason_current"),
        }
    finally:
        pass


def update_project_priority(project_id: str, new_priority: int, set_by: str,
                            source_surface: str, reason: str,
                            config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    if not set_by or not source_surface or not reason.strip():
        raise PriorityAuditError("set_by, source_surface, and reason are required")
    cfg = config or OrchConfig()
    project = _project_record(project_id, cfg)
    old_priority = project.get("priority")
    history_entry = {
        "priority_before": old_priority,
        "priority_after": int(new_priority),
        "set_by": set_by,
        "set_at": _utc_now_iso(),
        "source_surface": source_surface,
        "reason": reason,
    }
    driver = get_neo4j_driver(cfg)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            record = session.run("""
                MATCH (p:OrchProject {id: $project_id})
                SET p.priority = $new_priority,
                    p.priority_history = $priority_history,
                    p.updated_at = datetime()
                RETURN p.priority AS priority, p.priority_history AS priority_history
            """,
                project_id=project_id,
                new_priority=int(new_priority),
                priority_history=_append_history(list(project.get("priority_history") or []), history_entry),
            ).single()
            return {
                "priority": int(record["priority"]),
                "priority_history": _decode_json_field(record["priority_history"], []),
            }
    finally:
        pass


def add_project_condition(project_id: str, label: str, created_by: str,
                          config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    cfg = config or OrchConfig()
    project = _project_record(project_id, cfg)
    conditions = list(project.get("user_stop_conditions") or [])
    condition = _build_condition(label.strip(), created_by)
    conditions.append(condition)
    set_project_user_stop_conditions(project_id, conditions, config=cfg, created_by=created_by)
    return condition


def edit_project_condition(project_id: str, condition_id: str, label: str, edited_by: str,
                           config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    cfg = config or OrchConfig()
    project = _project_record(project_id, cfg)
    conditions = list(project.get("user_stop_conditions") or [])
    active = _condition_lookup(_active_conditions(conditions), condition_id)
    if not active:
        raise ConditionValidationError("active condition not found")
    updated_conditions: List[Dict[str, Any]] = []
    now_iso = _utc_now_iso()
    for condition in conditions:
        if condition["id"] == condition_id and int(condition["version"]) == int(active["version"]):
            mutated = dict(condition)
            mutated["deprecated_at"] = now_iso
            updated_conditions.append(mutated)
        else:
            updated_conditions.append(condition)
    new_condition = _build_condition(
        label.strip(),
        edited_by,
        condition_id=condition_id,
        version=int(active["version"]) + 1,
        replaces_id=condition_id,
    )
    updated_conditions.append(new_condition)
    set_project_user_stop_conditions(project_id, updated_conditions, config=cfg, created_by=edited_by)
    return new_condition


def preflight_supervisor_orphan_check(config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            result = session.run("""
                MATCH (p:OrchProject)
                WHERE coalesce(p.status, 'active') = 'active'
                  AND coalesce(p.migration_exempt, false) = false
                  AND (p.supervisor IS NULL OR p.supervisor = '' OR p.supervisor = 'unassigned' OR p.supervisor = 'unknown')
                RETURN p.id AS project_id, p.name AS name, p.status AS status, p.supervisor AS supervisor
                ORDER BY p.id
            """)
            rows = [dict(record) for record in result]
            return {"ok": len(rows) == 0, "count": len(rows), "rows": rows}
    finally:
        pass


def get_session_stop_status(session_id: str,
                            config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    cfg = config or OrchConfig()
    projects = get_session_supervised_projects(session_id, config=cfg)
    project_statuses: List[Dict[str, Any]] = []
    decision: Dict[str, Any] = {"can_stop": True}
    for project in projects:
        stop_state = _project_stop_reason_state(project)
        ready_tasks = ready_work(project["id"], session_id=session_id, config=cfg)
        available_conditions = [_condition_view(cond) for cond in _active_conditions(project.get("user_stop_conditions", []))]
        stop_reason = project.get("stop_reason_current")
        project_statuses.append({
            "project_id": project["id"],
            "priority": project.get("priority"),
            "status": project.get("status"),
            "stop_reason": stop_reason,
            "stop_reason_orphaned": stop_state["orphaned"],
            "available_conditions": available_conditions,
            "ready_task_count": len(ready_tasks),
        })
        if ready_tasks:
            decision = {
                "can_stop": False,
                "wake_type": "WAKE_WITH_QUEUE",
                "wake_reason": f"ready_work:{project['id']}",
            }
            break
        if project.get("status") == "completed":
            continue
        if stop_state["valid"]:
            continue
        if stop_state["deprecated_only"]:
            continue
        if project.get("status") == "in_progress":
            continue
        decision = {
            "can_stop": False,
            "wake_type": "WAKE_REASON_REQUIRED",
            "wake_reason": f"stop_reason_required:{project['id']}",
        }
        break
    return {"projects": project_statuses, "decision": decision}


def set_session_pause(session_id: str, pause_source: str, pause_reason: str,
                      pause_expires_at: Optional[str] = None,
                      paused_by: Optional[str] = None,
                      config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    if pause_source not in _PAUSE_SOURCES:
        raise PauseValidationError("pause_source invalid")
    cfg = config or OrchConfig()
    from .config import get_redis_sync
    r = get_redis_sync(cfg)
    meta = {
        "paused_by": paused_by or session_id,
        "paused_at": _utc_now_iso(),
        "pause_source": pause_source,
        "pause_reason": pause_reason or "",
        "pause_expires_at": pause_expires_at,
    }
    prefix = os.environ.get("NOTIFY_KEY_PREFIX", "taey")
    r.set(f"{prefix}:{session_id}:pause", "1")
    r.set(f"{prefix}:{session_id}:pause_meta", _json_encode(meta))
    return meta


def clear_session_pause(session_id: str, cleared_by: str,
                        config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    cfg = config or OrchConfig()
    from .config import get_redis_sync
    r = get_redis_sync(cfg)
    prefix = os.environ.get("NOTIFY_KEY_PREFIX", "taey")
    meta_raw = r.get(f"{prefix}:{session_id}:pause_meta")
    meta = _decode_json_field(meta_raw, {})
    meta["cleared_by"] = cleared_by
    meta["cleared_at"] = _utc_now_iso()
    r.delete(f"{prefix}:{session_id}:pause")
    r.delete(f"{prefix}:{session_id}:pause_meta")
    return meta


def get_agent_tasks(agent_id: str, config: Optional[OrchConfig] = None) -> List[Dict[str, Any]]:
    """Get tasks owned by an agent."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            result = session.run("""
                MATCH (t:OrchTask)
                WHERE t.owner = $agent_id
                  AND t.status IN ['pending', 'in_progress']
                RETURN t.id AS id, t.description AS description,
                       t.status AS status, t.priority AS priority
                ORDER BY coalesce(t.priority, 999999999) ASC
                LIMIT 10
            """, agent_id=agent_id)
            return [dict(r) for r in result]
    finally:
        pass  # Driver is singleton; do not close


def create_question(question_id: str, text: str, context: str = "",
                    task_id: str = "", asked_by: str = "",
                    config: Optional[OrchConfig] = None) -> str:
    """Create an OrchQuestion node linked to a task."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            # Link to task if provided
            task_clause = ""
            if task_id:
                task_clause = """
                WITH q
                MATCH (t:OrchTask {id: $task_id})
                MERGE (q)-[:CONCERNS_TASK]->(t)
                """
            
            result = session.run(f"""
                MERGE (q:OrchQuestion {{id: $id}})
                SET q.text = $text,
                    q.context = $context,
                    q.task_id = $task_id,
                    q.asked_by = $asked_by,
                    q.status = 'open',
                    q.created_at = datetime()
                {task_clause}
                RETURN q.id AS id
            """, id=question_id, text=text, context=context, 
                task_id=task_id, asked_by=asked_by)
            return result.single()["id"]
    finally:
        pass


def answer_question(question_id: str, answer: str, answered_by: str,
                    config: Optional[OrchConfig] = None) -> bool:
    """Provide an answer to an open question."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            result = session.run("""
                MATCH (q:OrchQuestion {id: $id})
                SET q.answer = $answer,
                    q.answered_by = $answered_by,
                    q.status = 'answered',
                    q.answered_at = datetime()
                RETURN q.id AS id
            """, id=question_id, answer=answer, answered_by=answered_by)
            return result.single() is not None
    finally:
        pass


def get_open_questions(config: Optional[OrchConfig] = None) -> List[Dict[str, Any]]:
    """Get all questions with status 'open'."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            result = session.run("""
                MATCH (q:OrchQuestion {status: 'open'})
                RETURN q.id AS id, q.text AS text, q.context AS context,
                       q.task_id AS task_id, q.asked_by AS asked_by,
                       q.created_at AS created_at
                ORDER BY q.created_at ASC
            """)
            return [dict(r) for r in result]
    finally:
        pass
