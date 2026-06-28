"""
Neo4j Orchestration Schema

Creates task DAG schema in the neo4j database with Orch-prefixed labels
to isolate from memory infrastructure (ISMA, HMM, Weaviate).

Label convention: OrchProject, OrchPhase, OrchTask, OrchFileOwnership
(memory labels: ISMAExchange, HMMTile, HMMMotif, Message, ChatSession)
"""

import copy
import datetime as dt
import hashlib
import itertools
import json
import logging
import math
import os
import stat
import subprocess
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional

from .config import OrchConfig, get_neo4j_driver
from .evidence_verification import UNVERIFIED, VERIFIED, verify_completion_evidence
from .inflight import PEER_HEARTBEAT_STALE_SEC as _DEFAULT_PEER_HEARTBEAT_STALE_SEC
from .inflight import task_actively_in_flight
from .notify_state import redis_connect as _notify_redis_connect
from .notify_state import state_key as _notify_state_key


SCHEMA_CONSTRAINTS = [
    "CREATE CONSTRAINT orch_task_id IF NOT EXISTS FOR (t:OrchTask) REQUIRE t.id IS UNIQUE",
    "CREATE CONSTRAINT orch_project_id IF NOT EXISTS FOR (p:OrchProject) REQUIRE p.id IS UNIQUE",
    "CREATE CONSTRAINT orch_phase_id IF NOT EXISTS FOR (ph:OrchPhase) REQUIRE ph.id IS UNIQUE",
    "CREATE CONSTRAINT orch_question_id IF NOT EXISTS FOR (q:OrchQuestion) REQUIRE q.id IS UNIQUE",
    "CREATE CONSTRAINT orch_global_context_key IF NOT EXISTS FOR (g:OrchGlobalContext) REQUIRE g.key IS UNIQUE",
    "CREATE CONSTRAINT orch_supervisor_session IF NOT EXISTS FOR (s:OrchSupervisor) REQUIRE s.session IS UNIQUE",
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


class TaskIdCollisionError(ValueError):
    """create_task was asked to create/MERGE a task id that is ALREADY owned by a DIFFERENT project.
    Allowing it would adopt + clobber the other project's node and fuse the two plans. Plan ingest
    auto-scopes declared ids to <project>::<id> (fleet_orchestrator/plan_loader) so legit ingest never trips this;
    this guard is the lower-choke backstop for every other path (R2 audit: do not delete it)."""
    pass


class TaskParentNotFoundError(TaskIdCollisionError):
    pass


_PAUSE_SOURCES = {"ui", "cli", "api", "user_command_explicit"}
_REF_READ_BYTE_CAP = 1024 * 1024
_COMPLETION_EVIDENCE_KEYS = ("commit_sha", "gate_run_id", "production_observation")
_COMPLETION_EVIDENCE_CONTEXT_KEYS = ("repo",)
_NON_SUCCESS_TERMINAL_EVIDENCE_KEYS = ("reason", "error", "production_observation")
# Closed set of legal task statuses. Validated BEFORE any completed-specific logic so a
# non-canonical spelling can never slip past the evidence gate.
_VALID_TASK_STATUSES = frozenset({"pending", "in_progress", "completed", "failed", "interrupted"})
_TERMINAL_TASK_STATUSES = frozenset({"completed", "failed", "interrupted"})
HUMAN_REVIEW_TASK_TYPE = "human-review"
HUMAN_REVIEW_QUESTION_TYPE = "human_review_gate"
COMPLETED_EVIDENCE_NEXT_STEP = (
    'Completed writes require shape-valid evidence and record completion_evidence_verification. VERIFIED means GitHub confirms the commit exists in a repo present in ORCH_COMPLETION_ALLOWED_REPOS and the required independent gate contexts passed for that exact commit_sha from trusted GitHub actors/apps. If evidence.repo is omitted, the configured/inferred runtime repo is used but must still be in ORCH_COMPLETION_ALLOWED_REPOS; UNVERIFIED means a shape-valid self-report only. '
    'Use `taey-task update <task-id> completed --evidence '
    '\'{"commit_sha":"<sha>","repo":"OWNER/REPO","production_observation":"<what you verified>"}\'` '
    'or PATCH /api/task/<task-id> with body '
    '{"status":"completed","evidence":{"commit_sha":"<sha>","repo":"OWNER/REPO",'
    '"production_observation":"<what you verified>"}}.'
)
NON_SUCCESS_EVIDENCE_NEXT_STEP = (
    'Use `taey-task update <task-id> %STATUS% --evidence '
    '\'{"reason":"<why>"}\'` or PATCH /api/task/<task-id> with body '
    '{"status":"%STATUS%","evidence":{"reason":"<why>"}}.'
)
PAUSE_NEXT_STEP = (
    'Use body {"pause_source":"api","pause_reason":"<why>",'
    '"pause_expires_at":"2026-06-19T20:00:00Z"}; '
    'pause_source must be one of: api, cli, ui, user_command_explicit.'
)
CHAT_NEXT_STEP = (
    'Use lineage matching [A-Za-z0-9._-]+ with no "..", for example session-1-codex. '
    'For chat, POST /api/chat/<lineage> with body '
    '{"text":"<message>","sender":"<session-id>","role":"user"}; '
    'role must be user or assistant.'
)


def _non_success_evidence_next_step(status: str) -> str:
    return NON_SUCCESS_EVIDENCE_NEXT_STEP.replace("%STATUS%", status)


def _human_review_answer_next_step(question_id: str) -> str:
    return (
        f"Complete human-review gates in `/ui/` or POST /api/ui/questions/{question_id}/answer "
        'with body {"answer":"<verdict>","answered_by":"<reviewer>"}; '
        f"ordinary POST /api/questions/{question_id}/answer records an unverified answer only."
    )


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _parse_pause_expires_at(value: Optional[str]) -> Optional[dt.datetime]:
    if value in (None, ""):
        return None
    if not isinstance(value, str):
        raise PauseValidationError(f"pause_expires_at must be an ISO-8601 string. {PAUSE_NEXT_STEP}")
    text = value.strip()
    if not text:
        return None
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        expires_at = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise PauseValidationError(f"pause_expires_at must be an ISO-8601 timestamp. {PAUSE_NEXT_STEP}") from exc
    if expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=dt.timezone.utc)
    return expires_at.astimezone(dt.timezone.utc)


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


def _completion_verification_status(verification: Dict[str, Any]) -> str:
    status = str(verification.get("status") or "").strip().upper()
    if status in {VERIFIED, UNVERIFIED}:
        return status
    return VERIFIED if verification.get("verified") is True else UNVERIFIED


def _legacy_unverified_completion_verification(evidence: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "status": UNVERIFIED,
        "verified": False,
        "source": "legacy-or-direct-db-write",
        "repo": "",
        "commit_sha": str(evidence.get("commit_sha") or "").strip(),
        "required_checks": [],
        "producer": "",
        "verifier": "none",
        "reason": "completed task has evidence but no independent verification record",
        "checks": [],
    }


def _attach_completion_evidence_verification(record: Dict[str, Any]) -> Dict[str, Any]:
    evidence = _decode_json_field(record.get("completion_evidence"), None)
    record["completion_evidence"] = evidence
    verification = _decode_json_field(record.get("completion_evidence_verification"), None)
    if not isinstance(verification, dict) and record.get("status") == "completed" and isinstance(evidence, dict) and evidence:
        verification = _legacy_unverified_completion_verification(evidence)
    if isinstance(verification, dict):
        status = _completion_verification_status(verification)
        verification["status"] = status
        verification["verified"] = status == VERIFIED
        record["completion_evidence_verification"] = verification
        record["completion_evidence_verification_status"] = status
        record["completion_evidence_verified"] = status == VERIFIED
    return record


def _evidence_value_well_formed(key: str, text: str) -> bool:
    """Cheap shape check per evidence key — rejects trivial junk, never claims to verify provenance."""
    if key == "commit_sha":
        # 4 (git --short min) to 64 (SHA-256) hex — future-proofs the sha256 transition (ChatGPT ws0 audit).
        return 4 <= len(text) <= 64 and all(c in "0123456789abcdefABCDEF" for c in text)
    if key == "repo":
        parts = text.split("/")
        allowed = set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._-")
        return (
            len(parts) == 2
            and all(1 <= len(part) <= 100 for part in parts)
            and all(all(c in allowed for c in part) for part in parts)
            and ".." not in text
        )
    if key == "gate_run_id":
        return len(text) >= 3 and all(c.isalnum() or c in "._:-/" for c in text)
    if key == "production_observation":
        return len(text) >= 8
    return False


def _normalize_completion_evidence(evidence: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    if evidence is None:
        return None
    if not isinstance(evidence, dict):
        raise CompletionEvidenceError(f"The completion-evidence check is a shape/plausibility filter (self-reported claim, not verified provenance). completion evidence must be a JSON object. {COMPLETED_EVIDENCE_NEXT_STEP}")
    normalized: Dict[str, str] = {}
    for key in _COMPLETION_EVIDENCE_KEYS + _COMPLETION_EVIDENCE_CONTEXT_KEYS:
        value = evidence.get(key)
        if value is None:
            continue
        # Must be a real non-empty string — not 0/False/[] coerced via str().
        if not isinstance(value, str):
            raise CompletionEvidenceError(
                "The completion-evidence check is a shape/plausibility filter (self-reported, not verified). "
                f"completion evidence {key!r} must be a string, got {type(value).__name__}. "
                f"{COMPLETED_EVIDENCE_NEXT_STEP}"
            )
        text = value.strip()
        if not text:
            continue
        # Per-key shape so trivial junk ("x"/"0") cannot pass as evidence.
        # This only rejects values that cannot plausibly BE the thing they claim to be;
        # the independent verifier writes VERIFIED/UNVERIFIED beside the accepted evidence.
        if not _evidence_value_well_formed(key, text):
            raise CompletionEvidenceError(
                "The completion-evidence check is a shape/plausibility filter; provenance is recorded separately as VERIFIED/UNVERIFIED. "
                f"completion evidence {key!r}={text!r} is not well-formed "
                f"(commit_sha=4-64 hex, repo=OWNER/REPO, gate_run_id>=3 id-chars, production_observation>=8 chars). "
                f"{COMPLETED_EVIDENCE_NEXT_STEP}"
            )
        normalized[key] = text
    if not any(key in normalized for key in _COMPLETION_EVIDENCE_KEYS):
        raise CompletionEvidenceError(
            "The completion-evidence check is a shape/plausibility filter; provenance is recorded separately as VERIFIED/UNVERIFIED. completed status requires evidence with at least one of: "
            f"commit_sha, gate_run_id, production_observation. Optional repo=OWNER/REPO selects the GitHub repository for commit verification. {COMPLETED_EVIDENCE_NEXT_STEP}"
        )
    return normalized


def has_valid_completion_evidence(evidence: Optional[Dict[str, Any]]) -> bool:
    try:
        return bool(_normalize_completion_evidence(evidence))
    except CompletionEvidenceError:
        return False


def _normalize_non_success_terminal_evidence(
    status: str,
    evidence: Optional[Dict[str, Any]],
) -> Optional[Dict[str, str]]:
    if evidence is None:
        raise CompletionEvidenceError(
            f"{status} status requires evidence with at least one of: "
            f"reason, error, production_observation. {_non_success_evidence_next_step(status)}"
        )
    if not isinstance(evidence, dict):
        raise CompletionEvidenceError(
            f"terminal evidence must be a JSON object. {_non_success_evidence_next_step(status)}"
        )
    normalized: Dict[str, str] = {}
    for key in _NON_SUCCESS_TERMINAL_EVIDENCE_KEYS:
        value = evidence.get(key)
        if value is None:
            continue
        if not isinstance(value, str):
            raise CompletionEvidenceError(
                f"terminal evidence {key!r} must be a string, got {type(value).__name__}. "
                f"{_non_success_evidence_next_step(status)}"
            )
        text = value.strip()
        if not text:
            continue
        if key == "production_observation" and not _evidence_value_well_formed(key, text):
            raise CompletionEvidenceError(
                f"terminal evidence {key!r}={text!r} is not well-formed "
                f"(production_observation>=8 chars). {_non_success_evidence_next_step(status)}"
            )
        normalized[key] = text
    if not normalized:
        raise CompletionEvidenceError(
            f"{status} status requires evidence with at least one of: "
            f"reason, error, production_observation. {_non_success_evidence_next_step(status)}"
        )
    return normalized


def _validate_terminal_status_write(status: str, evidence: Optional[Dict[str, Any]]) -> Optional[Dict[str, str]]:
    if status not in _VALID_TASK_STATUSES:
        raise CompletionEvidenceError(
            f"invalid status {status!r}; must be one of {sorted(_VALID_TASK_STATUSES)}"
        )
    if status == "completed" and evidence is None:
        raise CompletionEvidenceError(
            "completed status requires evidence with at least one of: "
            f"commit_sha, gate_run_id, production_observation. {COMPLETED_EVIDENCE_NEXT_STEP}"
        )
    if status == "completed":
        return _normalize_completion_evidence(evidence)
    if status in ("failed", "interrupted"):
        return _normalize_non_success_terminal_evidence(status, evidence)
    if evidence is not None:
        raise CompletionEvidenceError(
            "terminal evidence is only valid on a terminal transition. "
            "Remove evidence or set status to completed/failed/interrupted with the matching evidence body."
        )
    return None


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
        raw_sections = item.get("sections")
        sections: List[Dict[str, int]] = []
        section_items = raw_sections if isinstance(raw_sections, list) else [item]
        for section in section_items:
            if not isinstance(section, dict):
                continue
            try:
                l_start = int(section.get("l_start"))
                l_end = int(section.get("l_end"))
            except Exception:
                continue
            if l_start > 0 and l_end >= l_start:
                sections.append({"l_start": l_start, "l_end": l_end})
        if not path or not sections:
            continue
        first_section = sections[0]
        entry = {
            "path": path,
            "sections": sections,
            "l_start": first_section["l_start"],
            "l_end": first_section["l_end"],
        }
        label = str(item.get("label") or "").strip()
        if label:
            entry["label"] = label
        level = str(item.get("level") or "").strip()
        if level in {"overall", "supervisor", "project", "phase", "task"}:
            entry["level"] = level
        normalized.append(entry)
    return normalized


def _encode_refs_or_none(refs: Optional[List[Dict[str, Any]]]) -> Optional[str]:
    if refs is None:
        return None
    return _json_encode(_normalize_refs(refs))


def _has_control_chars(value: str) -> bool:
    return any(ord(ch) < 32 for ch in value)


def _session_root_map() -> Dict[str, str]:
    """Session -> repo-root values from ORCH_SESSION_ROOTS."""
    raw = os.environ.get("ORCH_SESSION_ROOTS", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return {
                str(k).strip(): str(v).strip()
                for k, v in parsed.items()
                if str(k).strip() and str(v).strip()
            }
    except (ValueError, TypeError):
        pass
    roots: Dict[str, str] = {}
    for pair in raw.split(","):
        if "=" in pair:
            key, _, value = pair.partition("=")
            if key.strip() and value.strip():
                roots[key.strip()] = value.strip()
    return roots


def _session_root_dirs() -> List[str]:
    """Directory values from ORCH_SESSION_ROOTS (JSON dict OR comma key=value).

    Each supervisor's own repo root is implicitly an allowed ref root for its
    own plans — so adding a supervisor (ORCH_SESSION_ROOTS) auto-grants ingestion
    without separately maintaining ORCH_REF_ALLOWED_ROOT. (Local parser, mirroring
    context_assembler, to avoid an import cycle.)
    """
    return list(_session_root_map().values())


def supervisor_access_resolution(
    session_id: Optional[str],
    *,
    config: Optional[OrchConfig] = None,
    registered_sessions: Optional[List[str]] = None,
    session_roots: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    session = (session_id or "").strip()
    registered = set(registered_sessions if registered_sessions is not None else (config or OrchConfig()).session_ids)
    roots = session_roots if session_roots is not None else _session_root_map()
    root = roots.get(session) if session in registered else None
    return {
        "session": session,
        "registered": bool(session and session in registered),
        "plan_ref_root": root or "",
    }


def session_ref_root(
    session_id: Optional[str],
    *,
    config: Optional[OrchConfig] = None,
    registered_sessions: Optional[List[str]] = None,
    session_roots: Optional[Dict[str, str]] = None,
) -> Optional[str]:
    access = supervisor_access_resolution(
        session_id,
        config=config,
        registered_sessions=registered_sessions,
        session_roots=session_roots,
    )
    return access["plan_ref_root"] or None


def _format_registered_sessions(sessions: List[str]) -> str:
    if not sessions:
        return "<none>"
    return ", ".join(sorted(sessions))


def session_registration_error_detail(session_id: str, config: Optional[OrchConfig] = None) -> str:
    cfg = config or OrchConfig()
    registered = list(cfg.session_ids or [])
    return (
        f"tmux session {session_id} is not a registered supervisor; "
        f"registered: {_format_registered_sessions(registered)}; "
        "supervisors are identified by tmux session name. "
        "Rename this tmux session to a registered name, or update ORCH_SESSION_IDS "
        "and ORCH_SESSION_ROOTS so the names match. No separate access request is "
        "needed when the session name matches local registration."
    )


def source_path_ref_root_error(
    raw_path: str,
    session_id: Optional[str],
    config: Optional[OrchConfig] = None,
) -> str:
    session = (session_id or "").strip() or "<unknown>"
    access = supervisor_access_resolution(session, config=config)
    if access["plan_ref_root"]:
        return (
            f"You are session {session}; your plan/ref root is {access['plan_ref_root']}; "
            f"put the file under it or pass a path there. You already have access - "
            f"you do not request it. Rejected source_path outside allowed ref roots: {raw_path}"
        )
    if not access["registered"]:
        return (
            f"{session_registration_error_detail(session, config)} "
            f"Rejected source_path outside allowed ref roots: {raw_path}"
        )
    roots = _session_root_map()
    configured = _format_registered_sessions(list(roots.keys()))
    return (
        f"You are session {session}, but no ORCH_SESSION_ROOTS path is registered for that name. "
        f"Registered session roots: {configured}. Supervisors are identified by tmux session name; "
        f"fix the name or registration, then put the plan under that root. "
        f"Rejected source_path outside allowed ref roots: {raw_path}"
    )


def _allowed_ref_roots() -> List[Path]:
    candidates: List[str] = []
    raw = str(os.environ.get("ORCH_REF_ALLOWED_ROOT") or "").strip()
    if raw:
        if raw.startswith("["):
            try:
                parsed = json.loads(raw)
            except Exception:
                parsed = []
            candidates = [str(item).strip() for item in parsed if str(item).strip()]
        else:
            normalized = raw.replace(os.pathsep, ",")
            candidates = [item.strip() for item in normalized.split(",") if item.strip()]
    # Auto-derive: every supervisor's session root is an allowed ref root, so the
    # explicit ORCH_REF_ALLOWED_ROOT list never has to be hand-synced per supervisor
    # (the drift that blocked two supervisors sharing one checkout from ingesting their own plans).
    candidates.extend(_session_root_dirs())
    # de-dup by resolved path, preserve order
    seen: set = set()
    roots: List[Path] = []
    for item in candidates:
        p = Path(item).expanduser().resolve(strict=False)
        if p not in seen:
            seen.add(p)
            roots.append(p)
    return roots


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


def validate_source_path_for_refs(
    source_path: Optional[str],
    refs_present: bool,
    session_id: Optional[str] = None,
    config: Optional[OrchConfig] = None,
) -> tuple[Optional[str], Optional[str]]:
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
        return None, "refs require ORCH_REF_ALLOWED_ROOT or ORCH_SESSION_ROOTS"
    try:
        resolved_source = Path(raw_path).resolve(strict=False)
    except Exception as exc:
        return None, f"invalid source_path ({exc.__class__.__name__})"
    if not _path_within_any_root(resolved_source, allowed_roots):
        return None, source_path_ref_root_error(raw_path, session_id, config=config)
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
    if any(part == ".." for part in candidate.parts):
        return None, f"ref outside allowed root: {raw_path}"
    allowed_roots = _allowed_ref_roots()
    if not allowed_roots:
        return None, "ref disabled: ORCH_REF_ALLOWED_ROOT and ORCH_SESSION_ROOTS are unset"
    # Resolution bases, in order of author intent:
    #   1) the source plan's own directory — a bare filename means "the file
    #      next to this plan" (source_path is validated within an allowed root
    #      at ingest, so this base is itself inside the sandbox);
    #   2) each allowed root — for repo-root-relative refs like "plans/x.md".
    # SECURITY is enforced below regardless of base: the resolved real path
    # MUST live within an allowed root (with ".." / absolute / "~" already
    # rejected above), so source-relative resolution cannot escape the sandbox.
    bases: List[Path] = []
    if source_path:
        try:
            bases.append(Path(source_path).resolve(strict=False).parent)
        except Exception:
            pass
    bases.extend(allowed_roots)
    first_resolved: Optional[Path] = None
    try:
        for base in bases:
            resolved = (base / candidate).resolve(strict=False)
            if not _path_within_any_root(resolved, allowed_roots):
                continue
            if first_resolved is None:
                first_resolved = resolved
            if resolved.exists():
                return resolved, None
        if first_resolved is not None:
            return first_resolved, None
        return None, f"ref outside allowed root: {raw_path}"
    except Exception as exc:
        return None, f"ref unreadable: {raw_path} ({exc.__class__.__name__})"


def _git_head_for_path(path: Path) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(path.parent), "rev-parse", "HEAD"],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=2,
        )
    except Exception:
        return "nogit"
    head = result.stdout.strip()
    return head if result.returncode == 0 and head else "nogit"


def _ref_section_pointer(section: Dict[str, int]) -> str:
    return f"{int(section.get('l_start') or 0)}-{int(section.get('l_end') or 0)}"


def _read_ref_context(refs: List[Dict[str, Any]], source_path: Optional[str],
                      line_cap: int = 200) -> Dict[str, Any]:
    resolved: List[Dict[str, Any]] = []
    warnings: List[str] = []
    remaining_lines = line_cap
    for ref in _normalize_refs(refs):
        path = str(ref.get("path") or "")
        sections = list(ref.get("sections") or [])
        first_section = sections[0] if sections else {"l_start": 0, "l_end": 0}
        ref_entry = {
            "path": path,
            "sections": [],
            "l_start": int(first_section.get("l_start") or 0),
            "l_end": int(first_section.get("l_end") or 0),
        }
        label = ref.get("label")
        if label:
            ref_entry["label"] = label
        level = ref.get("level")
        if level:
            ref_entry["level"] = level
        if remaining_lines <= 0:
            for section in sections:
                ref_entry["sections"].append({
                    "l_start": section["l_start"],
                    "l_end": section["l_end"],
                    "warning": "ref truncated by aggregate line cap",
                })
            ref_entry["warning"] = "ref truncated by aggregate line cap"
            resolved.append(ref_entry)
            continue
        resolved_path, resolve_warning = resolve_ref_path(path, source_path)
        if resolve_warning:
            for section in sections:
                ref_entry["sections"].append({
                    "l_start": section["l_start"],
                    "l_end": section["l_end"],
                    "warning": resolve_warning,
                })
            ref_entry["warning"] = resolve_warning
            warnings.append(resolve_warning)
            resolved.append(ref_entry)
            continue
        assert resolved_path is not None
        try:
            stat_result = resolved_path.stat()
            if not stat.S_ISREG(stat_result.st_mode):
                for section in sections:
                    section_warning = f"ref unreadable: {path}:{_ref_section_pointer(section)} (not a regular file)"
                    ref_entry["sections"].append({
                        "l_start": section["l_start"],
                        "l_end": section["l_end"],
                        "warning": section_warning,
                    })
                    warnings.append(section_warning)
                    ref_entry.setdefault("warning", section_warning)
                resolved.append(ref_entry)
                continue
            if stat_result.st_size > _REF_READ_BYTE_CAP:
                for section in sections:
                    section_warning = f"ref unreadable: {path}:{_ref_section_pointer(section)} (file exceeds byte cap {_REF_READ_BYTE_CAP})"
                    ref_entry["sections"].append({
                        "l_start": section["l_start"],
                        "l_end": section["l_end"],
                        "warning": section_warning,
                    })
                    warnings.append(section_warning)
                    ref_entry.setdefault("warning", section_warning)
                resolved.append(ref_entry)
                continue
            file_bytes = resolved_path.read_bytes()
            file_sha = hashlib.sha256(file_bytes).hexdigest()
            provenance_material = f"{_git_head_for_path(resolved_path)}:{file_sha}"
            ref_entry["provenance_hash"] = hashlib.sha256(provenance_material.encode("utf-8")).hexdigest()
            file_lines = file_bytes.decode("utf-8").splitlines()
        except Exception as exc:
            for section in sections:
                section_warning = f"ref unreadable: {path}:{_ref_section_pointer(section)} ({exc.__class__.__name__})"
                ref_entry["sections"].append({
                    "l_start": section["l_start"],
                    "l_end": section["l_end"],
                    "warning": section_warning,
                })
                warnings.append(section_warning)
                ref_entry.setdefault("warning", section_warning)
            resolved.append(ref_entry)
            continue
        for section in sections:
            l_start = int(section["l_start"])
            l_end = int(section["l_end"])
            section_entry: Dict[str, Any] = {"l_start": l_start, "l_end": l_end}
            slice_lines = file_lines[l_start - 1:l_end]
            if not slice_lines and l_start > 0:
                warning = f"ref unreadable: {path}:{l_start}-{l_end} (start beyond file)"
                section_entry["warning"] = warning
                ref_entry.setdefault("warning", warning)
                warnings.append(warning)
                ref_entry["sections"].append(section_entry)
                continue
            if not slice_lines:
                warning = f"ref unreadable: {path}:{l_start}-{l_end} (empty slice)"
                section_entry["warning"] = warning
                ref_entry.setdefault("warning", warning)
                warnings.append(warning)
                ref_entry["sections"].append(section_entry)
                continue
            allowed = min(len(slice_lines), remaining_lines)
            section_entry["content"] = "\n".join(slice_lines[:allowed])
            section_entry["truncated"] = allowed < len(slice_lines)
            if section_entry["truncated"]:
                section_entry["warning"] = "ref truncated by aggregate line cap"
                ref_entry["warning"] = "ref truncated by aggregate line cap"
            remaining_lines -= allowed
            if "content" not in ref_entry:
                ref_entry["content"] = section_entry["content"]
                ref_entry["truncated"] = section_entry["truncated"]
            ref_entry["sections"].append(section_entry)
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


_READY_DEPENDENCIES_SATISFIED_CYPHER = """
NOT EXISTS {
    MATCH (t)-[:DEPENDS_ON]->(dep:OrchTask)
    WHERE dep.status <> 'completed'
}
"""

_READY_TASK_ORDER_CYPHER = (
    "toInteger(coalesce(t.priority, 999999999)) ASC, "
    "t.created_at ASC, "
    "t.id ASC"
)

_ZERO_DEP_READY_CYPHER = f"""
MATCH (t:OrchTask {{id: $task_id}})
WHERE coalesce(t.owner, '') <> ''
  AND {_READY_DEPENDENCIES_SATISFIED_CYPHER}
  AND NOT EXISTS {{
      MATCH (t)-[:DEPENDS_ON]->(:OrchTask)
  }}
RETURN t.id AS task_id,
       t.owner AS owner,
       t.description AS description
"""


def _fleet_redis_connect():
    return _notify_redis_connect()


def _fleet_state_redis():
    return _notify_redis_connect()


def _state_key(node_id: str, suffix: str) -> str:
    return _notify_state_key(node_id, suffix)


# A peer counts as "actively working" (so the supervisor may ALLOW_STOP and wait
# for its RESPONSE_READY) only if its liveness heartbeat is fresher than this. The
# tool hooks stamp `<peer>:last_tool_activity` on pre/post-tool only. Prompt
# activity is intentionally excluded: daemon-injected prompts stamp
# `<peer>:last_activity`, and using that key created a false-working window for
# peers that were merely woken. 300s tolerates normal think/tool gaps; the
# failure direction past it is a brief busy-loop (bounded by the convergence
# valve), never a strand.
_PEER_HEARTBEAT_STALE_SEC = _DEFAULT_PEER_HEARTBEAT_STALE_SEC
_STOP_BLOCK_CONVERGENCE_LIMIT = 3
# This TTL deliberately survives short-lived process restarts so a stop cycle
# can still converge after a crash/restart. The tradeoff is a stale marker/count
# may survive until expiry if a process dies mid-cycle.
_STOP_BLOCK_TTL_SECS = 3600
WAKE_ALLOW_STOP = "ALLOW_STOP"
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
    r = _fleet_state_redis()
    pause_key = _state_key(session_id, "pause")
    meta_key = _state_key(session_id, "pause_meta")
    if not r.exists(pause_key):
        return False
    meta = _decode_json_field(r.get(meta_key), {})
    expires_at = None
    if isinstance(meta, dict):
        try:
            expires_at = _parse_pause_expires_at(meta.get("pause_expires_at"))
        except PauseValidationError:
            expires_at = dt.datetime.now(dt.timezone.utc)
    if expires_at and expires_at <= dt.datetime.now(dt.timezone.utc):
        r.delete(pause_key)
        r.delete(meta_key)
        return False
    return True


def _is_configured_dashboard_supervisor_session(session_id: str, cfg: OrchConfig) -> bool:
    session = str(session_id or "").strip().lower()
    if not session:
        return False
    return session in {
        _dashboard_supervisor_session(str(raw_session or ""))
        for raw_session in cfg.session_ids or []
    }


def _resolve_supervisor_session(session_id: str, config: Optional[OrchConfig] = None) -> str:
    cfg = config or OrchConfig()
    session = str(session_id or "").strip()
    if _is_configured_dashboard_supervisor_session(session, cfg):
        return _normalize_owner_session(session)
    r = _fleet_state_redis()
    try:
        explicit = r.get(_state_key(session, "parent"))
    except Exception:
        explicit = None
    if explicit and str(explicit).strip() != session:
        return str(explicit)
    for suffix in ("-codex", "-gemini", "-grok", "-claude"):
        if session.endswith(suffix):
            base = session[: -len(suffix)]
            if base:
                return base
    return session


def _session_is_supervisor_context(session_id: str, supervisor: str) -> bool:
    return str(session_id or "").strip() == str(supervisor or "").strip()


def _dashboard_supervisor_session(value: Any) -> str:
    session = str(value or "").strip()
    lowered = session.lower()
    for suffix in ("-codex", "-gemini", "-grok"):
        if lowered.endswith(suffix):
            session = session[: -len(suffix)]
            break
    return _normalize_owner_session(session).lower()


def _configured_dashboard_supervisor_session(session_id: str, cfg: OrchConfig) -> str:
    value = str(session_id or "").strip()
    if not value:
        return ""
    lowered = value.lower()
    if any(lowered.endswith(suffix) for suffix in ("-codex", "-gemini", "-grok")):
        value = _resolve_supervisor_session(value, config=cfg)
    return _dashboard_supervisor_session(value)


def _configured_dashboard_supervisors(config: Optional[OrchConfig] = None) -> set[str]:
    cfg = config or OrchConfig()
    supervisors: set[str] = set()
    for raw_session in cfg.session_ids or []:
        supervisor = _configured_dashboard_supervisor_session(str(raw_session or ""), cfg)
        if not supervisor:
            continue
        if supervisor and supervisor not in {"unassigned", "unknown", "none", "null"}:
            supervisors.add(supervisor)
    return supervisors


def list_dashboard_sessions(config: Optional[OrchConfig] = None) -> list:
    """Canonical supervisor sessions to surface as dashboard cards.

    The internal UI is fail-closed: data may prove a configured supervisor is active,
    but data alone may not mint visible sessions. ``ORCH_SESSION_IDS`` is the configured
    universe; peer names in it are resolved to their parent supervisor via Redis when
    available, then by suffix normalization.
    """
    cfg = config or OrchConfig()
    allowlist = _configured_dashboard_supervisors(cfg)
    if not allowlist:
        return []
    found: set[str] = set(allowlist)
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        for record in session.run(
            "MATCH (p:OrchProject) WHERE coalesce(p.supervisor, '') <> '' "
            "RETURN DISTINCT p.supervisor AS s"
        ):
            supervisor = _dashboard_supervisor_session(record["s"])
            if supervisor in allowlist:
                found.add(supervisor)
        for record in session.run(
            "MATCH (s:OrchSupervisor) WHERE coalesce(s.session, '') <> '' RETURN s.session AS s"
        ):
            supervisor = _dashboard_supervisor_session(record["s"])
            if supervisor in allowlist:
                found.add(supervisor)
    return sorted(found)


def list_sessions(config: Optional[OrchConfig] = None) -> list:
    """Sessions to surface (dashboard cards, etc.), derived from DATA — never hardcoded.

    Union of: distinct ``OrchProject.supervisor``, ``OrchSupervisor.session``, the
    ``ORCH_SESSION_IDS`` notify-allowlist, and an optional ``ORCH_DASHBOARD_SESSIONS`` pin
    (comma/semicolon list, for sessions that have no project yet). Sorted + de-duplicated.
    A fresh install with no data and no env pin returns ``[]`` — no operator-specific session
    names are baked into the product.
    """
    cfg = config or OrchConfig()
    found: set = set()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        for record in session.run(
            "MATCH (p:OrchProject) WHERE coalesce(p.supervisor, '') <> '' "
            "RETURN DISTINCT p.supervisor AS s"
        ):
            found.add(str(record["s"]))
        for record in session.run(
            "MATCH (s:OrchSupervisor) WHERE coalesce(s.session, '') <> '' RETURN s.session AS s"
        ):
            found.add(str(record["s"]))
    found.update(cfg.session_ids or [])
    raw = os.environ.get("ORCH_DASHBOARD_SESSIONS", "")
    if raw:
        found.update(item.strip() for item in raw.replace(";", ",").split(",") if item.strip())
    return sorted(found)


def _observed_stop_task_id(session_id: str, config: Optional[OrchConfig] = None) -> Optional[str]:
    r = _fleet_state_redis()
    raw = r.get(_state_key(session_id, "current_task"))
    if not raw:
        return None
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    task_id = payload.get("task_id")
    return str(task_id) if task_id else None


def _done_outcome_for_current_task(session_id: str, task_id: str) -> Optional[Dict[str, Any]]:
    from redis import RedisError

    try:
        r = _fleet_state_redis()
        current_raw = r.get(_state_key(session_id, "current_task"))
        outcome_raw = r.get(_state_key(session_id, "last_outcome"))
    except RedisError:
        return None
    if not current_raw or not outcome_raw:
        return None
    try:
        current = json.loads(current_raw)
        outcome = json.loads(outcome_raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(current, dict) or not isinstance(outcome, dict):
        return None
    if str(current.get("task_id") or "").strip() != str(task_id or "").strip():
        return None
    dispatcher = str(current.get("dispatcher") or current.get("supervisor") or "").strip()
    if not dispatcher or dispatcher == str(session_id or "").strip():
        return None
    if str(outcome.get("outcome") or "").strip().lower() != "done":
        return None
    if str(outcome.get("task_id") or "").strip() != str(task_id or "").strip():
        return None
    return outcome


def _safe_observed_stop_task_id(session_id: str, config: Optional[OrchConfig] = None) -> Optional[str]:
    """Best-effort observed-stop-task lookup that never raises -- for use inside the keystone
    fail-CLOSED exception handler, where the original decision already errored and we only
    want to keep the session on its current task if we can still read it."""
    try:
        return _observed_stop_task_id(session_id, config=config)
    except Exception:
        return None


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


def is_human_review_task(task: Optional[Dict[str, Any]]) -> bool:
    if not task:
        return False
    return str(task.get("task_type") or "").strip().lower() == HUMAN_REVIEW_TASK_TYPE


def is_awaiting_human_review_gate(task: Optional[Dict[str, Any]],
                                  config: Optional[OrchConfig] = None) -> bool:
    if not is_human_review_task(task):
        return False
    status = str(task.get("status") or "pending").strip().lower()
    if status in _TERMINAL_TASK_STATUSES:
        return False
    task_id = str(task.get("id") or task.get("task_id") or "").strip()
    if not task_id:
        return False
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        record = session.run("""
            MATCH (q:OrchQuestion)-[:CONCERNS_TASK]->(t:OrchTask {id: $task_id})
            WHERE q.question_type = $question_type
              AND q.status = 'open'
              AND q.gate_task_id = t.id
              AND t.task_type = $task_type
              AND NOT (coalesce(t.status, 'pending') IN $terminal_statuses)
            RETURN q.id AS question_id
            LIMIT 1
        """,
            task_id=task_id,
            question_type=HUMAN_REVIEW_QUESTION_TYPE,
            task_type=HUMAN_REVIEW_TASK_TYPE,
            terminal_statuses=list(_TERMINAL_TASK_STATUSES),
        ).single()
        return record is not None


def get_project_awaiting_human_review_gates(project_id: str,
                                            config: Optional[OrchConfig] = None) -> List[Dict[str, Any]]:
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        result = session.run("""
            MATCH (p:OrchProject {id: $project_id})-[:HAS_PHASE]->(ph:OrchPhase)-[:HAS_TASK]->(t:OrchTask)
            MATCH (q:OrchQuestion)-[:CONCERNS_TASK]->(t)
            WHERE t.task_type = $task_type
              AND NOT (coalesce(t.status, 'pending') IN $terminal_statuses)
              AND q.question_type = $question_type
              AND q.status = 'open'
              AND q.gate_task_id = t.id
            RETURN t.id AS task_id,
                   t.description AS description,
                   t.priority AS priority,
                   ph.id AS phase_id,
                   p.id AS project_id,
                   q.id AS question_id,
                   q.reviewer AS reviewer
            ORDER BY toInteger(coalesce(t.priority, 999999999)) ASC, t.created_at ASC
        """,
            project_id=project_id,
            task_type=HUMAN_REVIEW_TASK_TYPE,
            question_type=HUMAN_REVIEW_QUESTION_TYPE,
            terminal_statuses=list(_TERMINAL_TASK_STATUSES),
        )
        return [dict(row) for row in result]


def get_project_non_surfaced_human_review_tasks(project_id: str,
                                                config: Optional[OrchConfig] = None) -> List[Dict[str, Any]]:
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        result = session.run("""
            MATCH (p:OrchProject {id: $project_id})-[:HAS_PHASE]->(ph:OrchPhase)-[:HAS_TASK]->(t:OrchTask)
            WHERE t.task_type = $task_type
              AND NOT (coalesce(t.status, 'pending') IN $terminal_statuses)
              AND NOT EXISTS {
                  MATCH (q:OrchQuestion)-[:CONCERNS_TASK]->(t)
                  WHERE q.question_type = $question_type
                    AND q.status = 'open'
                    AND q.gate_task_id = t.id
              }
            RETURN t.id AS task_id,
                   t.description AS description,
                   t.status AS status,
                   t.priority AS priority,
                   ph.id AS phase_id,
                   p.id AS project_id
            ORDER BY toInteger(coalesce(t.priority, 999999999)) ASC, t.created_at ASC
        """,
            project_id=project_id,
            task_type=HUMAN_REVIEW_TASK_TYPE,
            question_type=HUMAN_REVIEW_QUESTION_TYPE,
            terminal_statuses=list(_TERMINAL_TASK_STATUSES),
        )
        return [dict(row) for row in result]


def project_has_non_human_nonterminal_work(project_id: str,
                                           config: Optional[OrchConfig] = None) -> bool:
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        record = session.run("""
            MATCH (p:OrchProject {id: $project_id})-[:HAS_PHASE]->(:OrchPhase)-[:HAS_TASK]->(t:OrchTask)
            WHERE NOT (coalesce(t.status, 'pending') IN $terminal_statuses)
              AND NOT (
                  coalesce(t.task_type, '') = $task_type
                  AND EXISTS {
                      MATCH (q:OrchQuestion)-[:CONCERNS_TASK]->(t)
                      WHERE q.question_type = $question_type
                        AND q.status = 'open'
                        AND q.gate_task_id = t.id
                  }
              )
            RETURN t.id AS task_id
            LIMIT 1
        """,
            project_id=project_id,
            task_type=HUMAN_REVIEW_TASK_TYPE,
            question_type=HUMAN_REVIEW_QUESTION_TYPE,
            terminal_statuses=list(_TERMINAL_TASK_STATUSES),
        ).single()
        return record is not None


# A blocked_on releases a session to STOP only if it names a REAL, non-terminal task (a live
# resolver) that is not the waiter itself and not part of a blocked_on cycle. This is the
# narrow, catastrophe-preventing core: a free-text human gate ("waiting on a person") is not a
# task -> not a resolver -> keep working. We deliberately do NOT inspect the DEPENDS_ON
# execution graph for runnability here: external audit showed that can't be made correct
# without a re-wake/terminalizer mechanism the orchestrator doesn't yet have, and attempting
# it is how this fix thrashed. Runnability-RECOVERY (a parked waiter on a resolver that never
# completes) is orch-watch's job, tracked as its own design effort (project phase p-systemic),
# NOT this engine's. Terminal statuses (completed/failed/interrupted) are excluded.
#
# A resolver must be ACTIVELY being resolved to license a stop -- 'in_progress'/'dispatched', NOT
# merely 'pending'/'ready'. A pending task that nobody is working is not a live wait: that was the
# hole that let a stale, self-created tracking task (made to satisfy blocked_on, then left pending)
# license a false stop -- the recurring "why did you stop" failure. If the only thing you are
# "waiting on" is pending, you are not waiting, you are stopping: keep going. You may validly wait
# only once a resolver is actually being worked (in_progress), which is what wakes you on completion.
_LIVE_RESOLVER_STATUSES = {"in_progress", "dispatched"}
# Max hops when walking the blocked_on chain (cycle/depth guard).
_MAX_RESOLVER_DEPTH = 8
_AWAIT_SIGNAL_PREFIX = "AWAIT"
_AWAIT_SIGNAL_KINDS = frozenset({"human-review", "family-consent", "external-signal"})


def _await_signal_gates_enabled() -> bool:
    raw = os.environ.get("ORCH_AWAIT_SIGNAL_GATES", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def _declared_await_signal(blocked_on: Optional[str]) -> Optional[Dict[str, str]]:
    """Parse the explicit terminal-wait marker accepted by the stop engine.

    Shape: ``AWAIT:<kind>:<detail>`` where kind is one of
    human-review/family-consent/external-signal. This is deliberately exact
    and does not scan arbitrary prose, so "waiting on a person" remains rejected.
    """
    if not _await_signal_gates_enabled() or not blocked_on:
        return None
    raw = str(blocked_on).strip()
    if not raw:
        return None
    parts = raw.split(":", 2)
    if len(parts) != 3:
        return None
    prefix, kind, detail = (part.strip() for part in parts)
    kind = kind.lower()
    if prefix.upper() != _AWAIT_SIGNAL_PREFIX or kind not in _AWAIT_SIGNAL_KINDS or not detail:
        return None
    return {"kind": kind, "detail": detail, "marker": raw}


def is_declared_await_signal(blocked_on: Optional[str]) -> bool:
    return _declared_await_signal(blocked_on) is not None


def _declared_await_signal_stop_reason(signal: Dict[str, str]) -> str:
    return (
        "ALLOW_STOP: task is explicitly awaiting "
        f"{signal['kind']} ({signal['detail']}). The session should wake when "
        "the signal resolves and blocked_on is cleared."
    )


def _blocked_on_has_live_resolver(blocked_on: Optional[str],
                                  current_task_id: Optional[str] = None,
                                  config: Optional[OrchConfig] = None) -> bool:
    """True only if `blocked_on` is the id of a task that will actually make progress and
    wake the session. The disciplined convention is that `blocked_on` IS a task id (an
    exact value), not prose -- so a free-text human gate ("waiting on a person's pull-forward
    decision") names no resolver and returns False, keeping the session on its work.

    We do NOT scan prose for embedded ids: "see unrelated-live-task" must not silently
    license a stop on a task that has no obligation to wake this one.
    The value is matched EXACTLY against the DB (ids may be slugs OR task-<hex>).

    Guards (all fail toward CONTINUING, never toward a silent permanent stop):
      - self-wait: a task waiting on itself is not an autonomous resolver (H1);
      - cycle/depth: follow the resolver's own blocked_on; if it loops back to the waiter
        or to an already-seen task, or runs deeper than _MAX_RESOLVER_DEPTH, it cannot
        guarantee a wake (H7/N1). A valid resolver chain must terminate in a live task
        that is NOT itself waiting -- something actually progressing. TRANSITIVE consequence
        Transitive consequence: if B is in_progress but is itself blocked_on a PENDING C,
        the chain does NOT bottom out in an actively-progressing node, so the waiter keeps
        going. A chain that ends in a not-yet-worked task offers no guaranteed wake;
      - stale/missing/terminal-status ref -> not live -> False;
      - STATUS FILTER (the real filter): only {in_progress, dispatched} are live -- a pending/ready
        task is not actively being resolved and cannot guarantee a wake, so it is NOT live. We do
        NOT inspect the DEPENDS_ON runnability graph here (external audit: it can't be made
        correct without a terminalizer; orch-watch / p-systemic owns parked-resolver recovery).
        Re-adding pending/ready to _LIVE_RESOLVER_STATUSES reopens the stale-tracking-task
        false-stop this fix closed -- do not;
      - DB errors are NOT swallowed here: they bubble to get_session_stop_decision's keystone
        fail-CLOSED handler (blocks + labels keystone_fail_closed honestly, not as a gate)."""
    if not blocked_on:
        return False
    node = str(blocked_on).strip()
    if not node:
        return False
    seen: set[str] = set()
    if current_task_id:
        seen.add(str(current_task_id).strip())
    # The walk is deliberately NOT wrapped in try/except. A get_task / dep-query error must
    # BUBBLE to get_session_stop_decision's keystone fail-CLOSED handler, which blocks AND
    # labels it keystone_fail_closed. Swallowing it here (-> False) would still block, but
    # would mislabel an infra error as a human gate -- a cannot-lie violation that hides DB
    # failures from telemetry. Bubbling = same fail-closed safety,
    # honest label. (Both resolver call-sites are inside _raw_stop_decision, inside that try.)
    for _ in range(_MAX_RESOLVER_DEPTH):
        if node in seen:
            return False  # self-wait or cycle -- no guaranteed wake
        seen.add(node)
        task = get_task(node, config=config)
        if not task:
            return False
        if str(task.get("status") or "").strip().lower() not in _LIVE_RESOLVER_STATUSES:
            return False
        # NOTE: we deliberately do NOT inspect the node's DEPENDS_ON execution graph here.
        # External audit proved that a stop-time "is this resolver runnable" check cannot
        # be made correct without a TERMINALIZER: nothing transitions a crashed/frozen
        # in_progress task to failed/interrupted, so a frozen/NULL-status dep is never "dead",
        # the completion-wake never fires, and a strict check either fail-OPENS the frozen
        # case or deadlocks live single-worker pipelines.
        # Runnability-recovery is the system's job via orch-watch + a reaper, NOT this engine's
        # (tracked separately). Here we validate ONLY the blocked_on delegation chain: a real,
        # live, non-self, non-cyclic task id. A human gate (free text) is not a task -> not a
        # resolver -> keep working (the actual catastrophe), which is what this engine owns.
        nxt = task.get("blocked_on")
        nxt = str(nxt).strip() if nxt not in (None, "", "null") else ""
        if not nxt:
            return True  # live, runnable, not-waiting -> real resolver
        node = nxt
    return False  # chain too deep -> cannot prove a wake -> keep working


def _human_gate_block_reason(task_id: Optional[str], blocked_on: Optional[str]) -> str:
    task_id_value = task_id or "your active task"
    marker = (str(blocked_on or "")[:120]) or "(empty)"
    return (
        "You cannot stop here. Your blocked_on names no live autonomous resolver "
        f"(marker: {marker!r}) and is not a declared await-signal marker "
        "(AWAIT:<human-review|family-consent|external-signal>:<detail>). "
        f"Continue {task_id_value}. If you are genuinely waiting on autonomous work, "
        "set blocked_on to reference the task id you await (e.g. task-abcd1234) so the "
        "system can wake you when it resolves; if you are genuinely awaiting a human, "
        "human review, or external signal, declare it with the structured AWAIT marker."
    )


def _queue_block_reason(task_id: Optional[str], description: Optional[str]) -> str:
    task_id_value = task_id or "unknown-task"
    task_title = (description or "untitled task")[:80]
    return (
        "You have ready work and must continue, not stop. "
        f"Next task: {task_id_value} — {task_title}. "
        "Pick it up via `taey-plan next` and do it. "
        "Do NOT stop until all supervised projects are completed or have a valid "
        "stop_reason matching a user_stop_condition. Setting blocked-on / "
        "waiting-on-worker is NOT a stop reason if parallel ready work exists."
    )


def _in_progress_block_reason(task_id: Optional[str], description: Optional[str]) -> str:
    task_id_value = task_id or "unknown-task"
    task_title = (description or "untitled task")[:80]
    return (
        f"Finish in-progress task {task_id_value}: {task_title}. "
        f"If this is dispatched peer work, call record_outcome('<session>', 'done', '<summary>'); "
        f"if you own the tracker task directly, run `taey-task update {task_id_value} completed "
        "--evidence '{\"commit_sha\":\"<sha>\",\"repo\":\"OWNER/REPO\",\"production_observation\":\"<what you verified>\"}'`."
    )


def _redis_marker_call(fn, *args):
    future = _STOP_MARKER_EXECUTOR.submit(fn, *args)
    return future.result(timeout=_STOP_MARKER_REDIS_TIMEOUT_S)


def _reason_required_block_reason(active_conditions: list[dict[str, Any]]) -> str:
    labels = [str(cond.get("label")) for cond in active_conditions if cond.get("label")]
    labels_text = ", ".join(labels) if labels else "no active user_stop_conditions"
    return (
        "You are trying to stop with no ready work and no valid stop_reason. "
        "Either there IS work (re-check taey-plan next) or you must set a stop_reason "
        f"matching one of: {labels_text}. You cannot stop otherwise."
    )


# NOTE: supervisor keep-going is DEFAULT-ON with NO flag (removed 2026-06-11). It was
# briefly flag-gated (_supervisor_dispatch_block_enabled, CF_SUPERVISOR_DISPATCH); that
# default-OFF flag was itself an override against the engine's whole purpose -- it let a
# supervisor stop while peer work was pending or in-flight. The keep-going invariant has
# no off-switch; see _raw_stop_decision.

_AUTONOMOUS_PEER_SUFFIXES = ("-codex", "-gemini", "-grok", "-claude")


def get_supervisor_dispatchable_peer_task(supervisor: str, project_id: str,
                                          config: Optional[OrchConfig] = None) -> Optional[Dict[str, Any]]:
    """Top task in ``project_id`` that is dispatchable to an AUTONOMOUS PEER of
    ``supervisor`` (``{supervisor}-codex`` / ``-gemini`` / ``-grok`` / ``-claude``)
    RIGHT NOW: status='pending' (NOT in_progress/dispatched -- a peer already on it
    must not block the supervisor), blocked_on empty, all DEPENDS_ON satisfied,
    project live. Returns enough to build a dispatch reason, or None."""
    cfg = config or OrchConfig()
    peer_owners = [f"{supervisor}{suf}" for suf in _AUTONOMOUS_PEER_SUFFIXES]
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        record = session.run(
            """
            MATCH (proj:OrchProject {id: $project_id})-[:HAS_PHASE]->(ph:OrchPhase)-[:HAS_TASK]->(t:OrchTask)
            WHERE t.status = 'pending'
              AND t.owner IN $peer_owners
              AND NOT (
                  coalesce(t.task_type, '') = $human_review_task_type
                  AND EXISTS {
                      MATCH (q:OrchQuestion)-[:CONCERNS_TASK]->(t)
                      WHERE q.question_type = $human_review_question_type
                        AND q.status = 'open'
                        AND q.gate_task_id = t.id
                  }
              )
              AND coalesce(t.blocked_on, '') = ''
              AND NOT EXISTS {
                  MATCH (t)-[:DEPENDS_ON]->(dep:OrchTask)
                  WHERE dep.status <> 'completed'
              }
              AND coalesce(toLower(trim(proj.status)), '') IN ['active', 'in_progress']
            RETURN t.id AS task_id, t.description AS description, t.owner AS owner,
                   t.priority AS priority, ph.id AS phase_id, proj.id AS project_id
            ORDER BY toInteger(coalesce(t.priority, 999999999)) ASC, t.created_at ASC
            LIMIT 1
            """,
            project_id=project_id, peer_owners=peer_owners,
            human_review_task_type=HUMAN_REVIEW_TASK_TYPE,
            human_review_question_type=HUMAN_REVIEW_QUESTION_TYPE,
        ).single()
        return dict(record) if record else None


def _supervisor_dispatch_block_reason(task_id: Optional[str], owner: Optional[str],
                                      description: Optional[str]) -> str:
    task_id_value = task_id or "unknown-task"
    owner_value = owner or "a peer"
    task_title = (description or "untitled task")[:80]
    return (
        "You supervise ready work that is NOT being worked: "
        f"{task_id_value} — {task_title} is pending and owned by your peer "
        f"{owner_value} but undispatched. DISPATCH it to that peer now; do not stop. "
        "A supervisor with dispatchable peer-owned work has not finished."
    )


# A dispatched peer task whose worker shows no 'working' signal is presumed live
# (its RESPONSE_READY will re-wake the supervisor) only until this dispatch age.
# Past it, a non-binding peer (codex/grok) that never set last_outcome is treated
# as a DROPPED/dead dispatch -> the supervisor must re-check (not silently strand).
# Generous so a normal long codex/grok build never trips it; the failure direction
# past it is a re-check (bounded), not a strand.
_PEER_DISPATCH_STALE_SEC = 1800


def _dispatch_age_seconds(task_id: str, config: Optional[OrchConfig] = None) -> Optional[float]:
    """Seconds since ``task_id`` last changed state (its in_progress dispatch age,
    via OrchTask.updated_at -- a peer working a task does not touch the node).
    Returns None if unknown/unparseable; the caller treats None as 'cannot confirm
    fresh' and fails toward BLOCK (re-check), never toward a silent strand."""
    try:
        task = get_task(task_id, config=config)
    except Exception:
        return None
    if not task:
        return None
    ua = task.get("updated_at")
    if ua is None:
        return None
    try:
        if hasattr(ua, "to_native"):
            native = ua.to_native()
        else:
            s = str(ua)
            if "." in s:
                head, _, tail = s.partition(".")
                i = 0
                while i < len(tail) and tail[i].isdigit():
                    i += 1
                s = head + "." + tail[:i][:6] + tail[i:]
            native = dt.datetime.fromisoformat(s)
        return time.time() - native.timestamp()
    except Exception:
        return None


def _peer_actively_working_task(workers: List[str], task_id: Optional[str],
                                config: Optional[OrchConfig] = None) -> bool:
    """True iff some worker in ``workers`` can be presumed ALIVE and working
    ``task_id`` now, so the supervisor may ALLOW_STOP and let the peer's
    RESPONSE_READY re-wake it. Two peer classes:

    BINDING peers (claude-clones via dispatch.bind_current_task) expose a live
    ``current_task`` + idle + last_tool_activity heartbeat. The signal is precise:
    current_task == task AND idle CLEAR AND heartbeat FRESH. current_task persists
    across NON-done terminations (record_outcome clears it only on done) and a
    hard kill clears nothing -- so requiring idle-clear + fresh heartbeat is what
    stops the PR#39 strand (a stopped/dead peer must NOT look working).

    NON-BINDING peers (codex/grok CLIs) bind current_task inconsistently and
    their idle oscillates per step, so the binding check can never be the only
    working signal. Their reliable queryable signals are last_outcome (set on
    done/error) + the tool-only heartbeat while work is actively using tools.

    All reads fail-closed toward BLOCK (not-working) so an outage / parse failure
    keeps the supervisor up rather than stranding work."""
    if not task_id:
        return False
    return task_actively_in_flight(
        task_id,
        workers=workers,
        config=config,
        heartbeat_ttl_secs=_PEER_HEARTBEAT_STALE_SEC,
        heartbeat_mode="peer",
    )


def get_session_liveness(session_id: str, config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "state": "idle",
        "active": False,
        "last_tool_activity": None,
        "age_seconds": None,
        "threshold_seconds": _PEER_HEARTBEAT_STALE_SEC,
    }
    try:
        raw = _fleet_state_redis().get(_state_key(session_id, "last_tool_activity"))
    except Exception:
        return payload
    if raw is None:
        return payload
    try:
        last_tool_activity = float(raw)
    except (TypeError, ValueError):
        return payload
    age_seconds = max(0.0, time.time() - last_tool_activity)
    active = age_seconds < _PEER_HEARTBEAT_STALE_SEC
    payload.update({
        "state": "active" if active else "idle",
        "active": active,
        "last_tool_activity": last_tool_activity,
        "age_seconds": age_seconds,
    })
    return payload


def get_supervisor_inflight_peer_task(supervisor: str, project_id: str,
                                      config: Optional[OrchConfig] = None) -> Optional[Dict[str, Any]]:
    """Top in-flight peer task in ``project_id`` that NEEDS THE SUPERVISOR TO
    ACT -- owned/dispatched to an autonomous peer, status in_progress/dispatched,
    project live, AND the peer is NOT actively working it right now.

    A peer that is actively mid-flight (its live current_task is bound to the
    task) does NOT need the supervisor awake: the peer's RESPONSE_READY
    notification re-wakes the supervisor the instant the work is done, to gate
    it. Blocking the supervisor's stop during that window is a busy-loop with
    nothing to do -- the repeated-Stop-hook symptom. So actively-worked tasks
    are EXCLUDED here (they ALLOW_STOP). What remains DOES need the supervisor:
    a peer that reported done (current_task cleared, awaiting gate) or stalled
    (dispatched but never bound / died). That is the real anti-strand case (the
    7-hour-stop hole) and it still BLOCKs. Returns the gate/investigate task,
    or None."""
    cfg = config or OrchConfig()
    peer_owners = [f"{supervisor}{suf}" for suf in _AUTONOMOUS_PEER_SUFFIXES]
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        rows = [dict(record) for record in session.run(
            """
            MATCH (proj:OrchProject {id: $project_id})-[:HAS_PHASE]->(ph:OrchPhase)-[:HAS_TASK]->(t:OrchTask)
            WHERE t.status IN ['in_progress', 'dispatched']
              AND (t.owner IN $peer_owners OR t.dispatched_to IN $peer_owners)
              AND NOT (
                  coalesce(t.task_type, '') = $human_review_task_type
                  AND EXISTS {
                      MATCH (q:OrchQuestion)-[:CONCERNS_TASK]->(t)
                      WHERE q.question_type = $human_review_question_type
                        AND q.status = 'open'
                        AND q.gate_task_id = t.id
                  }
              )
              AND coalesce(toLower(trim(proj.status)), '') IN ['active', 'in_progress']
            RETURN t.id AS task_id, t.description AS description, t.owner AS owner,
                   t.dispatched_to AS dispatched_to,
                   t.blocked_on AS blocked_on,
                   t.priority AS priority, ph.id AS phase_id, proj.id AS project_id
            ORDER BY toInteger(coalesce(t.priority, 999999999)) ASC, t.created_at ASC
            LIMIT 25
            """,
            project_id=project_id, peer_owners=peer_owners,
            human_review_task_type=HUMAN_REVIEW_TASK_TYPE,
            human_review_question_type=HUMAN_REVIEW_QUESTION_TYPE,
        )]
    for row in rows:
        if is_declared_await_signal(row.get("blocked_on")):
            continue
        workers = [w for w in (row.get("owner"), row.get("dispatched_to"))
                   if w in peer_owners]
        if _peer_actively_working_task(workers, row.get("task_id"), config=cfg):
            # Peer is mid-flight -> ALLOW_STOP; its RESPONSE_READY re-wakes us.
            continue
        return row
    return None


def has_active_inflight_peer_task(supervisor: str, project_id: str,
                                  config: Optional[OrchConfig] = None) -> bool:
    """True when a live supervised project has peer-owned in-flight work that
    the peer is actively working now.

    This is the positive counterpart to ``get_supervisor_inflight_peer_task``:
    that helper returns only work requiring supervisor action; this one returns
    the legitimate wait state where the supervisor has nothing to do until the
    peer's RESPONSE_READY/peer_idle re-wakes it.
    """
    cfg = config or OrchConfig()
    peer_owners = [f"{supervisor}{suf}" for suf in _AUTONOMOUS_PEER_SUFFIXES]
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        rows = [dict(record) for record in session.run(
            """
            MATCH (proj:OrchProject {id: $project_id})-[:HAS_PHASE]->(ph:OrchPhase)-[:HAS_TASK]->(t:OrchTask)
            WHERE t.status IN ['in_progress', 'dispatched']
              AND (t.owner IN $peer_owners OR t.dispatched_to IN $peer_owners)
              AND NOT (
                  coalesce(t.task_type, '') = $human_review_task_type
                  AND EXISTS {
                      MATCH (q:OrchQuestion)-[:CONCERNS_TASK]->(t)
                      WHERE q.question_type = $human_review_question_type
                        AND q.status = 'open'
                        AND q.gate_task_id = t.id
                  }
              )
              AND coalesce(toLower(trim(proj.status)), '') IN ['active', 'in_progress']
            RETURN t.id AS task_id, t.owner AS owner, t.dispatched_to AS dispatched_to
            ORDER BY toInteger(coalesce(t.priority, 999999999)) ASC, t.created_at ASC
            LIMIT 25
            """,
            project_id=project_id, peer_owners=peer_owners,
            human_review_task_type=HUMAN_REVIEW_TASK_TYPE,
            human_review_question_type=HUMAN_REVIEW_QUESTION_TYPE,
        )]
    for row in rows:
        workers = [w for w in (row.get("owner"), row.get("dispatched_to"))
                   if w in peer_owners]
        if _peer_actively_working_task(workers, row.get("task_id"), config=cfg):
            return True
    return False


def _supervisor_gate_block_reason(task_id: Optional[str], owner: Optional[str],
                                  description: Optional[str]) -> str:
    task_id_value = task_id or "unknown-task"
    owner_value = owner or "a peer"
    task_title = (description or "untitled task")[:80]
    return (
        "Your peer finished or stalled on supervised work and it needs YOU to act: "
        f"{task_id_value} — {task_title} (peer {owner_value}) is in_progress/dispatched "
        "but the peer is NOT actively working it (its current_task is clear). Either it "
        "reported done and awaits your gate (verify -> audit -> merge -> close with "
        "evidence), or it stalled (re-dispatch / investigate). Do NOT stop with un-gated "
        "peer work. (A peer that is actively mid-flight does NOT block your stop -- its "
        "RESPONSE_READY re-wakes you the instant it is done.)"
    )


def _awaiting_human_review_stop_reason(gates: List[Dict[str, Any]]) -> str:
    if not gates:
        return (
            "Awaiting human review surfaced on the dashboard. Open `/ui/` or inspect "
            "GET /api/projects to find the question, then complete it with "
            'POST /api/ui/questions/{question_id}/answer body '
            '{"answer":"<verdict>","answered_by":"<reviewer>"}.'
        )
    first = gates[0]
    task_id = first.get("task_id") or "unknown-task"
    question_id = first.get("question_id") or "unknown-question"
    reviewer = first.get("reviewer") or "the human reviewer"
    return (
        "Awaiting human review surfaced on the dashboard: "
        f"{task_id} / {question_id} for {reviewer}. No autonomous session work remains. "
        f"Open `/ui/` or POST /api/ui/questions/{question_id}/answer with body "
        '{"answer":"<verdict>","answered_by":"<reviewer>"} to complete the gate.'
    )


def _non_surfaced_human_review_block_reason(task: Dict[str, Any]) -> str:
    task_id = task.get("task_id") or "unknown-task"
    task_title = (str(task.get("description") or "human-review task")[:80] or "human-review task")
    return (
        "Human-review task is not surfaced as an open dashboard human_review_gate: "
        f"{task_id} — {task_title}. Treat it as normal unresolved work; do not stop "
        "until it is either surfaced for human review or completed through the proper gate."
    )


def _raw_stop_decision(session_id: str,
                       config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    cfg = config or OrchConfig()
    supervisor = _resolve_supervisor_session(session_id, config=cfg)
    ready_owner = session_id
    if _session_pause_active(supervisor, config=cfg):
        return {"block": False, "reason": None, "wake_type": WAKE_ALLOW_STOP, "task_id": None}

    supervised_projects = sorted(
        get_session_supervised_projects(session_id, config=cfg),
        key=lambda project: project.get("priority") if project.get("priority") is not None else 999999999,
    )
    projects = list(supervised_projects)
    if not _session_is_supervisor_context(session_id, supervisor):
        seen_project_ids = {str(project.get("id")) for project in projects}
        parent_projects = sorted(
            get_session_supervised_projects(supervisor, config=cfg),
            key=lambda project: project.get("priority") if project.get("priority") is not None else 999999999,
        )
        for project in parent_projects:
            project_id = str(project.get("id"))
            if project_id not in seen_project_ids:
                projects.append(project)
                seen_project_ids.add(project_id)

    for project in projects:
        status = str(project.get("status") or "").strip().lower()
        # Readiness/wake surfaces work ONLY from live projects. Status is normalized
        # (strip+lower) so 'Active'/'ACTIVE ' still ADMIT (case/whitespace must
        # not starve live work); NULL/missing/unknown -> '' -> EXCLUDED fail-closed
        # unknown must not silently admit). Concluded statuses (stopped/
        # completed/archived/...) are excluded. This keeps concluded projects from
        # re-surfacing work.
        if status not in ("active", "in_progress"):
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

    observed_done_task_id = _observed_stop_task_id(session_id, config=cfg)
    observed_done_outcome = (
        _done_outcome_for_current_task(session_id, observed_done_task_id)
        if observed_done_task_id
        else None
    )

    # Supervisor keep-going (DEFAULT-ON, no flag). The own-ready loop above only
    # surfaces work the supervisor owns ITSELF. A supervisor is ALSO not finished while
    # a supervised active project has peer-owned work that NEEDS THE SUPERVISOR TO ACT:
    # (a) pending-ready -> the supervisor must DISPATCH it, or (b) in-flight but the peer
    # is NOT actively working it (reported done -> gate it, or stalled -> investigate).
    # The keep-going invariant is "not finished while there is work for ME to act on" --
    # NOT "not finished while a peer is busy." A peer that is actively mid-flight does
    # NOT block the supervisor's stop: its RESPONSE_READY notification re-wakes the
    # supervisor the instant the work is done, so blocking during that window is a
    # busy-loop with nothing to do (the repeated-Stop-hook symptom). Leaving a
    # dispatchable or finished-but-un-gated task to ALLOW_STOP is what stranded a
    # supervisor for HOURS; that case still BLOCKs. There is NO off-switch -- the only
    # release is the wrapper convergence valve (force-allow after N stop-hook attempts),
    # which exists solely to prevent a permanent wedge on genuinely-stuck state.
    if not observed_done_outcome:
        for project in supervised_projects:
            status = str(project.get("status") or "").strip().lower()
            if status not in ("active", "in_progress"):
                continue
            peer_task = get_supervisor_dispatchable_peer_task(
                session_id, str(project.get("id")), config=cfg)
            if peer_task:
                task_id = peer_task.get("task_id")
                return {
                    "block": True,
                    "reason": _supervisor_dispatch_block_reason(
                        task_id, peer_task.get("owner"), peer_task.get("description")),
                    "wake_type": "WAKE_WITH_QUEUE",
                    "task_id": task_id,
                    "project_id": peer_task.get("project_id"),
                    "phase_id": peer_task.get("phase_id"),
                    "task_priority": peer_task.get("priority"),
                    "task_title_short": (str(peer_task.get("description") or "")[:80] or None),
                    "dispatch_to": peer_task.get("owner"),
                }
        for project in supervised_projects:
            status = str(project.get("status") or "").strip().lower()
            if status not in ("active", "in_progress"):
                continue
            inflight = get_supervisor_inflight_peer_task(
                session_id, str(project.get("id")), config=cfg)
            if inflight:
                task_id = inflight.get("task_id")
                return {
                    "block": True,
                    "reason": _supervisor_gate_block_reason(
                        task_id, inflight.get("owner"), inflight.get("description")),
                    "wake_type": "WAKE_WITH_QUEUE",
                    "task_id": task_id,
                    "project_id": inflight.get("project_id"),
                    "phase_id": inflight.get("phase_id"),
                    "task_priority": inflight.get("priority"),
                    "task_title_short": (str(inflight.get("description") or "")[:80] or None),
                    "gate_for": inflight.get("owner"),
                }

    current_work = get_session_current_work(session_id, config=cfg)
    current_task_id = current_work.get("top_task_id") if current_work else None
    blocked_on = _task_blocked_on(current_task_id, config=cfg)
    if current_task_id and not blocked_on:
        done_outcome = (
            observed_done_outcome
            if current_task_id == observed_done_task_id
            else _done_outcome_for_current_task(session_id, current_task_id)
        )
        if done_outcome:
            decision = {
                "block": False,
                "reason": (
                    "ALLOW_STOP: dispatched peer reported record_outcome(done); "
                    "CONTROL owns r5/merge/close."
                ),
                "wake_type": WAKE_ALLOW_STOP,
                "task_id": None,
                "completed_current_outcome": done_outcome,
            }
            return decision
        if task_actively_in_flight(
            current_task_id,
            workers=[session_id, supervisor],
            config=cfg,
            heartbeat_mode="none",
        ):
            return {
                "block": False,
                "reason": None,
                "wake_type": WAKE_ALLOW_STOP,
                "task_id": None,
                "out_of_band_inflight": True,
            }
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
        await_signal = _declared_await_signal(blocked_on)
        if await_signal:
            return {
                "block": False,
                "reason": _declared_await_signal_stop_reason(await_signal),
                "wake_type": WAKE_ALLOW_STOP,
                "task_id": None,
                "blocked_on": blocked_on,
                "awaiting_signal": await_signal,
            }
        if _blocked_on_has_live_resolver(blocked_on, current_task_id=current_task_id, config=cfg):
            return {
                "block": False,
                "reason": None,
                "wake_type": WAKE_ALLOW_STOP,
                "task_id": None,
                "blocked_on": blocked_on,
            }
        # blocked_on names no live autonomous resolver -> it is a human gate (or a
        # stale/self/cyclic reference). A human gate must NOT park a session in an
        # active plan: keep it on its in-progress task. This block is NON_CONVERGABLE
        # -- a human gate is permanently insoluble, so the wrapper convergence valve
        # must NOT force-allow it after N attempts (that would just delay the same
        # indefinite-park bug). See get_session_stop_decision.
        return {
            "block": True,
            "reason": _human_gate_block_reason(current_task_id, blocked_on),
            "wake_type": "WAKE_WITH_QUEUE",
            "task_id": current_task_id,
            "project_id": current_work.get("project_id") if current_work else None,
            "phase_id": current_work.get("phase_id") if current_work else None,
            "task_title_short": (str(current_work.get("top_task_desc") or "")[:80] or None) if current_work else None,
            "blocked_on_rejected": blocked_on,
            "non_convergable": True,
        }

    observed_task_id = _observed_stop_task_id(session_id, config=cfg)
    if observed_task_id:
        observed_task = get_task(observed_task_id, config=cfg)
        if is_awaiting_human_review_gate(observed_task, config=cfg):
            return {
                "block": False,
                "reason": _awaiting_human_review_stop_reason([{
                    "task_id": observed_task_id,
                    "question_id": "",
                    "reviewer": "",
                }]),
                "wake_type": WAKE_ALLOW_STOP,
                "task_id": None,
                "awaiting_human_review": True,
            }
    blocked_on = _task_blocked_on(observed_task_id, config=cfg)
    if blocked_on:
        await_signal = _declared_await_signal(blocked_on)
        if await_signal:
            return {
                "block": False,
                "reason": _declared_await_signal_stop_reason(await_signal),
                "wake_type": WAKE_ALLOW_STOP,
                "task_id": None,
                "blocked_on": blocked_on,
                "awaiting_signal": await_signal,
            }
        if _blocked_on_has_live_resolver(blocked_on, current_task_id=observed_task_id, config=cfg):
            return {
                "block": False,
                "reason": None,
                "wake_type": WAKE_ALLOW_STOP,
                "task_id": None,
                "blocked_on": blocked_on,
            }
        # A blocked_on with no live autonomous resolver (a human gate / stale / self /
        # cyclic ref) must NOT release the stop here either. Hard-block exactly like the
        # in-progress path -- do NOT fall through to the stop-reason logic, which would
        # ALLOW_STOP for a project with no active user_stop_conditions. Non_convergable
        # for the same reason as the first path.
        return {
            "block": True,
            "reason": _human_gate_block_reason(observed_task_id, blocked_on),
            "wake_type": "WAKE_WITH_QUEUE",
            "task_id": observed_task_id,
            "blocked_on_rejected": blocked_on,
            "non_convergable": True,
        }

    for project in supervised_projects:
        status = str(project.get("status") or "").strip().lower()
        if status not in ("active", "in_progress"):
            continue
        if has_active_inflight_peer_task(session_id, str(project.get("id")), config=cfg):
            return {"block": False, "reason": None, "wake_type": WAKE_ALLOW_STOP, "task_id": None}

    for project in supervised_projects:
        status = str(project.get("status") or "").strip().lower()
        if status not in ("active", "in_progress"):
            continue
        non_surfaced = get_project_non_surfaced_human_review_tasks(str(project.get("id")), config=cfg)
        if non_surfaced:
            task = non_surfaced[0]
            return {
                "block": True,
                "reason": _non_surfaced_human_review_block_reason(task),
                "wake_type": "WAKE_WITH_QUEUE",
                "task_id": task.get("task_id"),
                "project_id": task.get("project_id"),
                "phase_id": task.get("phase_id"),
                "task_priority": task.get("priority"),
                "task_title_short": (str(task.get("description") or "")[:80] or None),
            }

    awaiting_human_gates: List[Dict[str, Any]] = []
    for project in supervised_projects:
        status = str(project.get("status") or "").strip().lower()
        if status not in ("active", "in_progress"):
            continue
        project_id = str(project.get("id"))
        gates = get_project_awaiting_human_review_gates(project_id, config=cfg)
        if gates and not project_has_non_human_nonterminal_work(project_id, config=cfg):
            awaiting_human_gates.extend(gates)
    if awaiting_human_gates:
        return {
            "block": False,
            "reason": _awaiting_human_review_stop_reason(awaiting_human_gates),
            "wake_type": WAKE_ALLOW_STOP,
            "task_id": None,
            "awaiting_human_review": True,
            "question_id": awaiting_human_gates[0].get("question_id"),
            "project_id": awaiting_human_gates[0].get("project_id"),
        }

    reason_required: Optional[Dict[str, Any]] = None
    for project in supervised_projects:
        status = str(project.get("status") or "").strip().lower()
        # Readiness/wake surfaces work ONLY from live projects. Status is normalized
        # (strip+lower) so 'Active'/'ACTIVE ' still ADMIT (case/whitespace must
        # not starve live work); NULL/missing/unknown -> '' -> EXCLUDED fail-closed
        # unknown must not silently admit). Concluded statuses (stopped/
        # completed/archived/...) are excluded so concluded projects do not
        # re-surface work.
        if status not in ("active", "in_progress"):
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
    cfg = config or OrchConfig()
    try:
        decision = _raw_stop_decision(session_id, config=cfg)
    except Exception as exc:
        # FAIL CLOSED. A stop-DISCIPLINE engine must never let an error license a stop:
        # the human-gate decision path runs live Neo4j/Redis reads (_task_blocked_on ->
        # get_task, _observed_stop_task_id, get_session_current_work, ...), so a transient
        # blip -- not just a full DB outage -- would otherwise bubble here and ALLOW_STOP an
        # unresolved human gate. Keep working instead; mark
        # non_convergable so the valve below can't later release it. Anti-wedge is satisfied
        # by "keep working", not by "allow stop". This is loud (busy-loop visible in logs)
        # if the engine is persistently broken -- the correct failure mode for a keystone,
        # vs. a silent fleet-wide stop. (no-fallbacks: works or fails loud.)
        decision = {
            "block": True,
            "reason": "Stop-engine decision errored; failing CLOSED (keep working) so an "
                      "error cannot license an unverified stop. Stay on your current task; if "
                      "this persists the orchestrator DB is degraded -- fix that, do not stop.",
            "wake_type": "WAKE_WITH_QUEUE",
            # best-effort: keep the session on its CURRENT task rather than detaching it to
            # fetch a new one. If even this read fails, None.
            "task_id": _safe_observed_stop_task_id(session_id, cfg),
            "non_convergable": True,
            "keystone_fail_closed": {
                "session": session_id,
                "operation": "_raw_stop_decision",
                "exception_class": exc.__class__.__name__,
            },
        }

    if not stop_hook_active:
        return decision

    # NON_CONVERGABLE blocks bypass the convergence release valve below. The valve exists
    # so a session cannot wedge permanently on a repeated block -- but a human-gate /
    # stale / self / cyclic blocked_on is permanently insoluble BY DESIGN, and the whole
    # point of rejecting it is that it must NEVER release a stop. Letting the valve
    # force-allow it after N attempts just delays the indefinite-park bug by N cycles
    # Clear any stale convergence counter so a
    # later genuine block starts fresh, then return the block unmodified.
    if decision.get("non_convergable") and decision.get("block"):
        try:
            r = _fleet_state_redis()
            _redis_marker_call(r.delete, _stop_block_marker_key(session_id), _stop_block_count_key(session_id))
        except Exception:
            pass
        return decision

    marker_key = _stop_block_marker_key(session_id)
    count_key = _stop_block_count_key(session_id)
    try:
        r = _fleet_state_redis()
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
            original_reason = decision.get("reason")
            original_wake_type = decision.get("wake_type")
            original_task_id = decision.get("task_id")
            decision["block"] = False
            decision["reason"] = None
            decision["wake_type"] = WAKE_ALLOW_STOP
            decision["task_id"] = None
            decision["converged_allow"] = True
            decision["converged_reason"] = (
                "Stop-hook convergence released a repeated identical block after "
                f"{block_count} observations so the session cannot wedge permanently. "
                f"Original block reason: {original_reason or '(none)'}. "
                "Inspect the original task/project state, then resume with `taey-plan current` "
                "or `taey-plan next` if work remains."
            )
            decision["converged_original"] = {
                "wake_type": original_wake_type,
                "task_id": original_task_id,
                "reason": original_reason,
            }
            _redis_marker_call(r.delete, marker_key, count_key)
    except Exception as exc:
        decision = dict(decision)
        decision["convergence_marker_fail_open"] = {
            "session": session_id,
            "exception_class": exc.__class__.__name__,
            "reason": (
                "Redis convergence marker write/read failed; the stop decision is still returned, "
                "but repeated Stop hook bodies may repeat until notify Redis/fleet state Redis is healthy."
            ),
            "next_step": (
                f"Inspect notify Redis/fleet state Redis connectivity for session {session_id}; "
                f"retry GET /api/sessions/{session_id}/stop-decision?stop_hook_active=true after Redis recovers."
            ),
        }
    return decision


def init_schema(config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    """Create orchestration schema (constraints + indexes). Idempotent."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    results = {"constraints": [], "indexes": [], "errors": []}

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
        raise ValueError(
            "supervisor must be non-empty and not 'unassigned' unless migration_exempt=true. "
            'Use POST /api/projects body {"id":"<project-id>","name":"<name>",'
            '"supervisor":"<tmux-session>"} or run '
            "`taey-plan ingest <plan.md> --supervisor <tmux-session>`."
        )
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


def create_phase(project_id: str, phase_id: str, name: str,
                 order: int = 0,
                 refs: Optional[List[Dict[str, Any]]] = None,
                 source_path: Optional[str] = None,
                 config: Optional[OrchConfig] = None) -> str:
    """Create an OrchPhase linked to a project."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        # Same ownership guard as create_task (R3 audit CRITICAL: the phase path was unhardened —
        # /api/projects/{id}/phases passes a caller phase_id straight to this bare MERGE). Refuse if
        # $phase_id already exists owned by another project / fused / orphan.
        _guard_creatable(session, label="OrchPhase", node_id=phase_id, target_project_id=project_id)
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


# Owner-traversal per node label, for the shared creation guard.
_OWNER_PATTERN = {
    "OrchTask": "(ex)<-[:HAS_TASK]-(:OrchPhase)<-[:HAS_PHASE]-(op:OrchProject)",
    "OrchPhase": "(ex)<-[:HAS_PHASE]-(op:OrchProject)",
}


def _guard_creatable(session, *, label: str, node_id: str, target_project_id: str) -> None:
    """Refuse to create/MERGE an OrchTask/OrchPhase id that ALREADY exists and is NOT owned SOLELY by
    target_project_id. Covers foreign-owned (adopt+clobber), already-fused (multi-owner), AND owner-less
    ORPHAN nodes (R3 audit: the guard was blind to orphans → poisoned-orphan ship-gate bypass). A pure
    re-create within the same project (owners == [target]) is allowed. This is the ONE guard every
    caller-influenced node-identity write goes through — harden the CLASS, not one instance."""
    rec = session.run(
        f"""
        OPTIONAL MATCH (ex:{label} {{id: $id}})
        OPTIONAL MATCH {_OWNER_PATTERN[label]}
        WITH ex, collect(DISTINCT op.id) AS owners
        WHERE ex IS NOT NULL AND (size(owners) = 0 OR size([o IN owners WHERE o <> $target]) > 0)
        RETURN owners AS owners
        """,
        id=node_id, target=target_project_id,
    ).single()
    if rec is not None:
        raise TaskIdCollisionError(
            f"{label} id '{node_id}' already exists and is not owned solely by project "
            f"'{target_project_id}' (owners={rec['owners'] or 'orphan/none'}); refusing to adopt or "
            f"clobber it. Ids auto-scope to <project>::<id>; an id reaching create unscoped or naming a "
            f"foreign/orphan node is rejected."
        )


def _resolve_phase_project(session, phase_id: str) -> str:
    """Return the SINGLE OrchProject id that owns this phase, else RAISE (fail-closed). A task may only
    be created/assigned under a phase that resolves to exactly one project — never an unparented/missing
    phase (0 owners) or a fused phase (>=2 owners). R4 audit: the old `if _trow and pid` guard FAILED
    OPEN on the resolution-miss branch (skipped the guard, then MERGEd). Collect (not .single()) so a
    fused phase is a clean refusal, not a ResultNotSingleError 500."""
    rec = session.run(
        "MATCH (ph:OrchPhase {id: $pid})<-[:HAS_PHASE]-(p:OrchProject) "
        "RETURN collect(DISTINCT p.id) AS owners",
        pid=phase_id,
    ).single()
    owners = (rec["owners"] if rec else []) or []
    if not owners:
        raise TaskParentNotFoundError(f"phase '{phase_id}' not found; cannot create task under a missing phase")
    if len(owners) != 1:
        raise TaskIdCollisionError(
            f"phase '{phase_id}' does not resolve to exactly one project (owners={owners or 'none'}); "
            f"refusing to create/assign a task under a missing, orphan, or fused phase."
        )
    return owners[0]


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
    initial_status: str = "pending",
    wake_owner_if_ready: bool = True,
    config: Optional[OrchConfig] = None,
) -> str:
    """Create an OrchTask linked to a phase."""
    if initial_status in _VALID_TASK_STATUSES:
        _validate_terminal_status_write(initial_status, None)
    if str(initial_status or "").strip().lower() in _TERMINAL_TASK_STATUSES:
        raise CompletionEvidenceError(
            "terminal initial status is not accepted; create the task pending and complete it through the evidence-gated task API"
        )
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        # Ownership guard via the shared chokepoint (R2/R3/R4 audit — do NOT delete, do NOT make
        # conditional). Resolve THIS phase's project FAIL-CLOSED (raises on missing/orphan/fused
        # phase), then refuse if $task_id already exists owned by anyone other than it
        # (foreign/fused/orphan). Legit ingest never trips this (declared ids are auto-scoped).
        _target = _resolve_phase_project(session, phase_id)
        _guard_creatable(session, label="OrchTask", node_id=task_id, target_project_id=_target)
        result = session.run("""
            MATCH (ph:OrchPhase {id: $phase_id})
            MERGE (t:OrchTask {id: $task_id})
            ON CREATE SET t.created_at = datetime(),
                          t.status = $initial_status,
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
            initial_status=initial_status,
        )
        record = result.single()
        if record is None:
            raise TaskParentNotFoundError(f"phase '{phase_id}' not found; cannot create task '{task_id}'")
        created_id = record["id"]
    if wake_owner_if_ready:
        _wake_owner_for_zero_dep_task(created_id, cfg)
    return created_id


def set_overall_refs(refs: List[Dict[str, Any]], config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        row = session.run("""
            MERGE (g:OrchGlobalContext {key: 'overall'})
            ON CREATE SET g.created_at = datetime()
            SET g.refs = $refs,
                g.updated_at = datetime()
            RETURN g.key AS key, g.refs AS refs
        """, refs=_encode_refs_or_none(refs) or "[]").single()
    result = dict(row) if row else {"key": "overall", "refs": "[]"}
    result["refs"] = _normalize_refs(result.get("refs"))
    return result


def set_supervisor_refs(session_id: str, refs: List[Dict[str, Any]],
                        config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    session_value = _normalize_owner_session(session_id)
    if not session_value:
        raise ValueError("supervisor session must be non-empty")
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        row = session.run("""
            MERGE (s:OrchSupervisor {session: $session})
            ON CREATE SET s.created_at = datetime()
            SET s.refs = $refs,
                s.updated_at = datetime()
            RETURN s.session AS session, s.refs AS refs
        """, session=session_value, refs=_encode_refs_or_none(refs) or "[]").single()
    result = dict(row) if row else {"session": session_value, "refs": "[]"}
    result["refs"] = _normalize_refs(result.get("refs"))
    return result


def _context_record(level: str, refs: Any, *, line_cap: int = 200) -> Dict[str, Any]:
    normalized = []
    for ref in _normalize_refs(refs):
        ref["level"] = level
        normalized.append(ref)
    return {
        "level": level,
        "refs": normalized,
        "ref_context": _read_ref_context(normalized, source_path=None, line_cap=line_cap),
    }


def get_overall_refs(config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        row = session.run("""
            OPTIONAL MATCH (g:OrchGlobalContext {key: 'overall'})
            RETURN g.refs AS refs
        """).single()
    return _context_record("overall", row.get("refs") if row else [])


def get_supervisor_refs(session_id: str, config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    session_value = _normalize_owner_session(session_id)
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        row = session.run("""
            OPTIONAL MATCH (s:OrchSupervisor {session: $session})
            RETURN s.refs AS refs
        """, session=session_value).single()
    return _context_record("supervisor", row.get("refs") if row else [])


def add_dependency(task_id: str, depends_on_id: str,
                   config: Optional[OrchConfig] = None) -> bool:
    """Create DEPENDS_ON relationship between tasks."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        result = session.run("""
            OPTIONAL MATCH (t:OrchTask {id: $task_id})
            OPTIONAL MATCH (dep:OrchTask {id: $depends_on_id})
            FOREACH (_ IN CASE WHEN t IS NOT NULL AND dep IS NOT NULL THEN [1] ELSE [] END |
                MERGE (t)-[:DEPENDS_ON]->(dep)
            )
            RETURN (t IS NOT NULL) AS t_exists,
                   (dep IS NOT NULL) AS dep_exists
        """, task_id=task_id, depends_on_id=depends_on_id)
        record = result.single()
        return bool(record and record["t_exists"] and record["dep_exists"])


def get_ready_tasks(config: Optional[OrchConfig] = None) -> List[Dict[str, Any]]:
    """Get tasks that are pending with all dependencies satisfied."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        result = session.run(f"""
            MATCH (t:OrchTask {{status: 'pending'}})
            WHERE {_READY_DEPENDENCIES_SATISFIED_CYPHER}
            RETURN t.id AS id, t.description AS description,
                   t.priority AS priority, t.owner AS owner,
                   t.capability_tags AS capability_tags,
                   t.file_blast_radius AS file_blast_radius,
                   t.estimated_tokens AS estimated_tokens
            ORDER BY {_READY_TASK_ORDER_CYPHER}
        """)
        return [dict(r) for r in result]


def _clear_matching_current_task(owner: str, task_id: str, config: Optional[OrchConfig] = None) -> bool:
    if not owner or not task_id:
        return False
    from redis import RedisError, WatchError

    try:
        r = _fleet_state_redis()
        key = _state_key(owner, "current_task")
        for attempt in range(5):
            with r.pipeline() as pipe:
                try:
                    pipe.watch(key)
                    raw = pipe.get(key)
                    if not raw:
                        pipe.unwatch()
                        return False
                    try:
                        current = json.loads(raw)
                    except Exception:
                        pipe.unwatch()
                        return False
                    if not isinstance(current, dict) or str(current.get("task_id") or "") != task_id:
                        pipe.unwatch()
                        return False
                    pipe.multi()
                    pipe.delete(key)
                    pipe.execute()
                    return True
                except WatchError:
                    time.sleep(0.01 * (attempt + 1))
                    continue
        return False
    except RedisError as exc:
        _LOG.warning("best-effort current_task clear failed owner=%s task=%s: %s", owner, task_id, exc)
        return False


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
    terminal_status = status in _TERMINAL_TASK_STATUSES
    with driver.session(database=cfg.neo4j_db) as session:
        if terminal_status:
            existing = session.run("""
                MATCH (t:OrchTask {id: $task_id})
                RETURN t.task_type AS task_type
            """, task_id=task_id).single()
            if existing is not None and existing.get("task_type") == "human-review":
                raise CompletionEvidenceError(
                    f"human-review gate tasks must be resolved through the dashboard UI review endpoint, "
                    f"not by setting terminal status {status!r} through the task status API. "
                    + _human_review_answer_next_step("{question_id}")
                )
        completion_evidence_value = _validate_terminal_status_write(status, completion_evidence)
        completed_by_value = completed_by or owner or ""
        completion_verification_value = (
            verify_completion_evidence(completion_evidence_value, producer=completed_by_value)
            if status == "completed"
            else None
        )
        completion_verification_status = (
            _completion_verification_status(completion_verification_value)
            if isinstance(completion_verification_value, dict)
            else None
        )
        if result is None:
            rec = session.run("""
                MATCH (t:OrchTask {id: $task_id})
                WITH t, t.owner AS previous_owner
                SET t.status = $status,
                    t.owner = CASE WHEN coalesce(trim($owner), '') = '' THEN t.owner ELSE $owner END,
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
                        WHEN $terminal_status THEN $completion_evidence
                        ELSE NULL
                    END,
                    t.completion_evidence_verification = CASE
                        WHEN $status = 'completed' THEN $completion_evidence_verification
                        ELSE NULL
                    END,
                    t.completion_evidence_verification_status = CASE
                        WHEN $status = 'completed' THEN $completion_evidence_verification_status
                        ELSE NULL
                    END,
                    t.completion_evidence_verified = CASE
                        WHEN $status = 'completed' THEN $completion_evidence_verified
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
                RETURN t.id AS id, t.owner AS owner, previous_owner AS previous_owner
            """, task_id=task_id, status=status, owner=owner, blocked_on=blocked_on_value,
                 completion_evidence=_json_encode(completion_evidence_value) if completion_evidence_value else None,
                 completion_evidence_verification=_json_encode(completion_verification_value) if completion_verification_value else None,
                 completion_evidence_verification_status=completion_verification_status,
                 completion_evidence_verified=completion_verification_status == VERIFIED,
                 terminal_status=terminal_status,
                 completed_by=completed_by_value)
        else:
            rec = session.run("""
                MATCH (t:OrchTask {id: $task_id})
                WITH t, t.owner AS previous_owner
                SET t.status = $status,
                    t.owner = CASE WHEN coalesce(trim($owner), '') = '' THEN t.owner ELSE $owner END,
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
                        WHEN $terminal_status THEN $completion_evidence
                        ELSE NULL
                    END,
                    t.completion_evidence_verification = CASE
                        WHEN $status = 'completed' THEN $completion_evidence_verification
                        ELSE NULL
                    END,
                    t.completion_evidence_verification_status = CASE
                        WHEN $status = 'completed' THEN $completion_evidence_verification_status
                        ELSE NULL
                    END,
                    t.completion_evidence_verified = CASE
                        WHEN $status = 'completed' THEN $completion_evidence_verified
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
                RETURN t.id AS id, t.owner AS owner, previous_owner AS previous_owner
            """, task_id=task_id, status=status, owner=owner, result=result,
                 blocked_on=blocked_on_value,
                 completion_evidence=_json_encode(completion_evidence_value) if completion_evidence_value else None,
                 completion_evidence_verification=_json_encode(completion_verification_value) if completion_verification_value else None,
                 completion_evidence_verification_status=completion_verification_status,
                 completion_evidence_verified=completion_verification_status == VERIFIED,
                 terminal_status=terminal_status,
                 completed_by=completed_by_value)
        task_row = rec.single()
        if task_row is None:
            return False
        session.run("""
            MATCH (p:OrchProject)-[:HAS_PHASE]->(:OrchPhase)-[:HAS_TASK]->(t:OrchTask {id: $task_id})
            WITH p
            OPTIONAL MATCH (p)-[:HAS_PHASE]->(:OrchPhase)-[:HAS_TASK]->(sibling:OrchTask)
            WHERE sibling.id <> $task_id AND sibling.status = 'in_progress'
            WITH p, count(sibling) AS in_progress_siblings
            SET p.in_progress_heartbeat_at = CASE
                    WHEN $status = 'in_progress' THEN datetime()
                    WHEN $status IN ['completed', 'failed', 'interrupted'] AND in_progress_siblings = 0 THEN ''
                    ELSE p.in_progress_heartbeat_at
                END,
                p.status = CASE
                    WHEN $status = 'in_progress' THEN 'in_progress'
                    WHEN $status IN ['completed', 'failed', 'interrupted']
                         AND p.status = 'in_progress'
                         AND in_progress_siblings = 0 THEN 'active'
                    ELSE p.status
                END,
                p.updated_at = datetime()
        """, task_id=task_id, status=status)
        if terminal_status:
            from .worker_liveness import clear_worker_task_liveness

            clear_worker_task_liveness(task_id, config=cfg)
            for session_id in {str(task_row.get("previous_owner") or ""), str(task_row.get("owner") or "")}:
                _clear_matching_current_task(session_id, task_id, config=cfg)
        return True


def resolve_task_id(task_id: str, config: Optional[OrchConfig] = None) -> str:
    """Resolve a possibly-bare task id to its canonical namespaced node id.

    Post-v1.7.0 tasks are namespaced '<project>::<bare>', so a session that references the BARE id
    (e.g. 'task-abcd1234') would 404 — the cause of the recurring stop-engine "phantom" a session
    could not self-clear. Resolution: an exact match always wins; else, if the id is bare (no '::')
    and EXACTLY ONE OrchTask ends with '::<id>', return that node id; else return the id unchanged
    (an honest 404 downstream). Ambiguous (>1) matches are never guessed."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        if session.run("MATCH (t:OrchTask {id: $id}) RETURN count(t) AS n", id=task_id).single()["n"] > 0:
            return task_id
        if "::" in task_id:
            return task_id
        rows = session.run(
            "MATCH (t:OrchTask) WHERE t.id ENDS WITH $sfx RETURN t.id AS id LIMIT 2",
            sfx="::" + task_id,
        ).data()
    return rows[0]["id"] if len(rows) == 1 else task_id


def get_task(task_id: str,
             config: Optional[OrchConfig] = None) -> Optional[Dict[str, Any]]:
    """Return one OrchTask node as a plain dict."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        result = session.run("""
            MATCH (t:OrchTask {id: $task_id})
            RETURN t
        """, task_id=task_id).single()
        if not result:
            return None
        task = _normalize_map(dict(result["t"]))
        task["forced_continuation_count"] = int(task.get("forced_continuation_count", 0) or 0)
        task = _attach_completion_evidence_verification(task)
        return _attach_ref_runtime(task, source_path=task.get("source_path"))


def check_phase_complete(phase_id: str,
                         config: Optional[OrchConfig] = None) -> bool:
    """Check if all tasks in a phase are completed. If so, mark phase completed."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
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


def get_task_phase(task_id: str,
                   config: Optional[OrchConfig] = None) -> Optional[str]:
    """Get the phase ID for a given task."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        result = session.run("""
            MATCH (ph:OrchPhase)-[:HAS_TASK]->(t:OrchTask {id: $task_id})
            RETURN ph.id AS phase_id
        """, task_id=task_id)
        rec = result.single()
        return rec["phase_id"] if rec else None


def assign_task_to_phase(task_id: str, phase_id: str,
                         config: Optional[OrchConfig] = None) -> bool:
    """Ensure a task belongs to exactly one phase, re-parenting when needed."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        # Guard via the SHARED chokepoint (R4 audit BLOCK-2: the prior bespoke predicate here was
        # orphan-blind — it permitted owners==[] and re-opened the poisoned-orphan ship-gate bypass on
        # the re-parent path). Resolve the phase's project fail-closed, then run the SAME _guard_creatable
        # the create paths use (refuses foreign/fused/orphan; allows the same-project re-parent the
        # loader does, where the task is already owned by target). One helper, no per-path divergence.
        _target = _resolve_phase_project(session, phase_id)
        _guard_creatable(session, label="OrchTask", node_id=task_id, target_project_id=_target)
        result = session.run("""
            MATCH (t:OrchTask {id: $task_id})
            MATCH (ph:OrchPhase {id: $phase_id})
            OPTIONAL MATCH (:OrchPhase)-[rel:HAS_TASK]->(t)
            DELETE rel
            MERGE (ph)-[:HAS_TASK]->(t)
            RETURN t.id AS task_id
        """, task_id=task_id, phase_id=phase_id)
        return result.single() is not None


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
    with driver.session(database=cfg.neo4j_db) as session:
        result = session.run("""
            MATCH (p:OrchProject {id: $project_id})
            OPTIONAL MATCH (g:OrchGlobalContext {key: 'overall'})
            OPTIONAL MATCH (s:OrchSupervisor {session: coalesce(p.supervisor, '')})
            WITH p, g, s
            OPTIONAL MATCH (p)-[:HAS_PHASE]->(ph:OrchPhase)
            OPTIONAL MATCH (ph)-[:HAS_TASK]->(t:OrchTask)
            WITH p, g, s, ph, t
            ORDER BY coalesce(t.priority, 999999999) ASC, t.created_at ASC
            WITH p, g, s, ph,
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
                             completion_evidence: t.completion_evidence,
                             completion_evidence_verification: t.completion_evidence_verification,
                             completion_evidence_verification_status: t.completion_evidence_verification_status,
                             completion_evidence_verified: t.completion_evidence_verified,
                             completed_by: t.completed_by,
                             completed_at: t.completed_at,
                             refs: t.refs,
                             source_path: t.source_path
                         }
                     END
                 ) AS tasks
            ORDER BY ph.order ASC, ph.name ASC
            RETURN p, g, s, collect(
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
        overall_tier = _context_record("overall", dict(record["g"]).get("refs") if record["g"] else [])
        supervisor_tier = _context_record("supervisor", dict(record["s"]).get("refs") if record["s"] else [])
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
                task_row = _attach_completion_evidence_verification(task_row)
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
            "ref_tiers": {
                "overall": overall_tier,
                "supervisor": supervisor_tier,
                "project": {
                    "level": "project",
                    "refs": project.get("refs", []),
                    "ref_context": project.get("ref_context", {"refs": [], "warnings": [], "line_cap": 200}),
                },
                "phases": [
                    {
                        "id": item["phase"].get("id"),
                        "name": item["phase"].get("name"),
                        "level": "phase",
                        "refs": item["phase"].get("refs", []),
                        "ref_context": item["phase"].get("ref_context", {"refs": [], "warnings": [], "line_cap": 200}),
                    }
                    for item in phases
                ],
                "tasks": [
                    {
                        "id": task.get("id"),
                        "description": task.get("description"),
                        "level": "task",
                        "refs": task.get("refs", []),
                        "ref_context": task.get("ref_context", {"refs": [], "warnings": [], "line_cap": 200}),
                    }
                    for item in phases
                    for task in item.get("tasks", [])
                ],
            },
        }


def get_session_current_work(session_id: str,
                             config: Optional[OrchConfig] = None) -> Optional[Dict[str, Any]]:
    """Return the highest-priority in-progress task for a session with project context."""
    cfg = config or OrchConfig()
    observed_task_id = _safe_observed_stop_task_id(session_id, cfg)
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        result = session.run("""
            MATCH (p:OrchProject)-[:HAS_PHASE]->(ph:OrchPhase)-[:HAS_TASK]->(t:OrchTask)
            WHERE (t.owner = $session_id OR t.dispatched_to = $session_id)
              AND t.status = 'in_progress'
              AND NOT (
                  coalesce(t.task_type, '') = $human_review_task_type
                  AND EXISTS {
                      MATCH (q:OrchQuestion)-[:CONCERNS_TASK]->(t)
                      WHERE q.question_type = $human_review_question_type
                        AND q.status = 'open'
                        AND q.gate_task_id = t.id
                  }
              )
              AND (
                  p.id <> 'default'
                  OR ($observed_task_id IS NOT NULL AND t.id = $observed_task_id)
              )
              // Fail-closed, normalized allowlist: an in_progress task in a concluded
              // project (stopped/completed/unknown) is NOT live work and must not
              // force-grind its owner. trim+lower so mixed-case/whitespace status
              // still admits; NULL/'' -> excluded fail-closed.
              AND coalesce(toLower(trim(p.status)), '') IN ['active', 'in_progress']
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
                   t.owner AS owner,
                   t.dispatched_to AS dispatched_to,
                   t.status AS status,
                   t.blocked_on AS blocked_on,
                   t.source_path AS task_source_path,
                   t.refs AS task_refs
            ORDER BY CASE WHEN $observed_task_id IS NOT NULL AND t.id = $observed_task_id THEN 0 ELSE 1 END ASC,
                     coalesce(p.priority, 999999999) ASC, coalesce(t.priority, 999999999) ASC, ph.order ASC, t.created_at ASC
            LIMIT 1
        """,
            session_id=session_id,
            observed_task_id=observed_task_id,
            human_review_task_type=HUMAN_REVIEW_TASK_TYPE,
            human_review_question_type=HUMAN_REVIEW_QUESTION_TYPE,
        )
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


def list_self_owned_in_progress_sessions(*,
                                         task_id_prefix: Optional[str] = None,
                                         config: Optional[OrchConfig] = None,
                                         limit: int = 200) -> List[Dict[str, Any]]:
    """Return sessions with self-owned graph work that may need a wake sweep.

    Redis ``current_task`` sweeps cover dispatched/bound work. This query covers
    the complementary graph-only case: an owner has marked a task in_progress
    itself, there is no distinct dispatched peer, and a live project still
    expects that owner to drive forward.
    """
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        rows = session.run("""
            MATCH (p:OrchProject)-[:HAS_PHASE]->(ph:OrchPhase)-[:HAS_TASK]->(t:OrchTask)
            WHERE t.status = 'in_progress'
              AND coalesce(t.owner, '') <> ''
              AND (coalesce(t.dispatched_to, '') = '' OR t.dispatched_to = t.owner)
              AND ($task_id_prefix IS NULL OR t.id STARTS WITH $task_id_prefix)
              AND NOT (
                  coalesce(t.task_type, '') = $human_review_task_type
                  AND EXISTS {
                      MATCH (q:OrchQuestion)-[:CONCERNS_TASK]->(t)
                      WHERE q.question_type = $human_review_question_type
                        AND q.status = 'open'
                        AND q.gate_task_id = t.id
                  }
              )
              AND coalesce(toLower(trim(p.status)), '') IN ['active', 'in_progress']
            RETURN DISTINCT t.owner AS session_id,
                   t.id AS task_id,
                   t.description AS description,
                   p.id AS project_id,
                   ph.id AS phase_id,
                   coalesce(t.priority, 999999999) AS priority,
                   coalesce(p.priority, 999999999) AS project_priority,
                   t.created_at AS created_at
            ORDER BY project_priority ASC, priority ASC, created_at ASC
            LIMIT $limit
        """,
            task_id_prefix=task_id_prefix,
            human_review_task_type=HUMAN_REVIEW_TASK_TYPE,
            human_review_question_type=HUMAN_REVIEW_QUESTION_TYPE,
            limit=int(limit),
        )
        return [dict(row) for row in rows]


def get_session_next_ready(session_id: str, exclude_task_id: Optional[str] = None,
                           project_id: Optional[str] = None,
                           config: Optional[OrchConfig] = None) -> Optional[Dict[str, Any]]:
    """Return the top ready task for a session, excluding a specific task if requested."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        result = session.run(f"""
            MATCH (proj:OrchProject)-[:HAS_PHASE]->(ph:OrchPhase)-[:HAS_TASK]->(t:OrchTask)
            WHERE t.status = 'pending'
              AND coalesce(t.owner, '') = $sess
              AND NOT (
                  coalesce(t.task_type, '') = $human_review_task_type
                  AND EXISTS {{
                      MATCH (q:OrchQuestion)-[:CONCERNS_TASK]->(t)
                      WHERE q.question_type = $human_review_question_type
                        AND q.status = 'open'
                        AND q.gate_task_id = t.id
                  }}
              )
              AND coalesce(t.blocked_on, '') = ''
              AND ($exclude_task_id IS NULL OR t.id <> $exclude_task_id)
              AND ($project_id IS NULL OR proj.id = $project_id)
              AND {_READY_DEPENDENCIES_SATISFIED_CYPHER}
              // Fail-closed, normalized allowlist (was a stopped/completed denylist;
              // denylist is fail-open for any new concluded status). trim+lower so
              // mixed-case status admits; NULL/'' excluded fail-closed.
              AND coalesce(toLower(trim(proj.status)), '') IN ['active', 'in_progress']
            RETURN t.id AS task_id, t.description AS description,
                   t.priority AS priority, t.owner AS owner,
                   t.blocked_on AS blocked_on,
                   t.refs AS task_refs,
                   t.source_path AS task_source_path,
                   ph.id AS phase_id, ph.name AS phase_name,
                   ph.refs AS phase_refs, ph.source_path AS phase_source_path,
                   proj.id AS project_id, proj.name AS project_name,
                   proj.refs AS project_refs, proj.source_path AS project_source_path
            ORDER BY {_READY_TASK_ORDER_CYPHER}
            LIMIT 1
        """,
            sess=session_id,
            exclude_task_id=exclude_task_id,
            project_id=project_id,
            human_review_task_type=HUMAN_REVIEW_TASK_TYPE,
            human_review_question_type=HUMAN_REVIEW_QUESTION_TYPE,
        ).single()
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


def get_project_user_stop_conditions(project_id: str,
                                     config: Optional[OrchConfig] = None) -> Optional[List[Dict[str, Any]]]:
    """Return the project's configured versioned user-stop conditions."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
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


def set_project_user_stop_conditions(project_id: str, user_stop_conditions: List[Any],
                                     config: Optional[OrchConfig] = None,
                                     created_by: str = "unknown") -> List[Dict[str, Any]]:
    """Persist the project's configured versioned user-stop conditions."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    normalized = _normalize_user_stop_conditions(user_stop_conditions, created_by)
    with driver.session(database=cfg.neo4j_db) as session:
        record = session.run("""
            MATCH (p:OrchProject {id: $project_id})
            SET p.user_stop_conditions = $user_stop_conditions,
                p.updated_at = datetime()
            RETURN p.user_stop_conditions AS user_stop_conditions
        """, project_id=project_id, user_stop_conditions=_json_encode(normalized)).single()
        if not record:
            raise ProjectNotFoundError(
                f"Project {project_id} not found. List valid projects with `taey-plan list` "
                "or GET /api/projects before writing stop conditions."
            )
        return _normalize_user_stop_conditions(
            _decode_json_field(record["user_stop_conditions"], []),
            created_by="decoded",
        )


def get_task_project(task_id: str,
                     config: Optional[OrchConfig] = None) -> Optional[Dict[str, Any]]:
    """Return the project context for a task."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
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


def ensure_default_project(config: Optional[OrchConfig] = None) -> str:
    """Ensure a default project and phase exist for ad-hoc tasks. Returns phase_id."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    default_supervisor = "system"
    default_priority = _next_project_priority(default_supervisor, cfg)
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


def get_session_supervised_projects(session_id: str,
                                    config: Optional[OrchConfig] = None) -> List[Dict[str, Any]]:
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        result = session.run("""
            MATCH (p:OrchProject)
            WHERE coalesce(p.supervisor, '') = $session_id
              AND coalesce(p.migration_exempt, false) = false
            RETURN p
            ORDER BY coalesce(p.priority, 999999999) ASC, p.created_at ASC
        """, session_id=session_id)
        return [_decode_project_node(dict(record["p"])) for record in result]


def _supervisor_badge_seed(supervisor: str) -> Dict[str, Any]:
    return {
        "supervisor": supervisor,
        "state": "IDLE",
        "label": "idle",
        "summary": "No open task or question is present on this supervisor's projects.",
        "open_task_count": 0,
        "in_progress_count": 0,
        "await_count": 0,
        "human_review_hold_count": 0,
        "dependency_wait_count": 0,
        "live_resolver_wait_count": 0,
        "open_question_count": 0,
        "question_ids": [],
        "reasons": [],
    }


def _supervisor_badge_session(value: Any) -> str:
    return _dashboard_supervisor_session(value)


def _supervisor_badge_fallback_session(dashboard_set: set[str], cfg: OrchConfig) -> str:
    configured = str(getattr(cfg, "badge_fallback_supervisor", "") or "").strip()
    if not configured:
        return ""
    normalized = _configured_dashboard_supervisor_session(configured, cfg)
    if normalized in dashboard_set:
        return normalized
    return ""


def get_supervisor_badges(config: Optional[OrchConfig] = None) -> Dict[str, Dict[str, Any]]:
    """Derived per-supervisor work badge for the dashboard.

    This is intentionally a view over existing OrchProject/OrchTask/OrchQuestion
    state. No stopped state is inferred in this view.
    """
    cfg = config or OrchConfig()
    dashboard_supervisors = sorted({
        supervisor
        for supervisor in (_supervisor_badge_session(item) for item in list_dashboard_sessions(config=cfg))
        if supervisor and supervisor not in {"unassigned", "unknown", "none", "null"}
    })
    dashboard_set = set(dashboard_supervisors)
    fallback_bucket = _supervisor_badge_fallback_session(dashboard_set, cfg)
    badges: Dict[str, Dict[str, Any]] = {
        supervisor: _supervisor_badge_seed(supervisor)
        for supervisor in dashboard_supervisors
    }
    question_ids_by_supervisor: Dict[str, set[str]] = {supervisor: set() for supervisor in badges}
    unknown_questions_by_supervisor: Dict[str, int] = {supervisor: 0 for supervisor in badges}

    def row_for(raw_supervisor: Any) -> Optional[Dict[str, Any]]:
        supervisor = _supervisor_badge_session(raw_supervisor)
        if supervisor in dashboard_set:
            return badges[supervisor]
        if fallback_bucket:
            return badges[fallback_bucket]
        return None

    def add_question_id(supervisor: str, raw_record: Any) -> bool:
        record = _decode_json_field(raw_record, {})
        if not isinstance(record, dict):
            return False
        question_id = str(record.get("id") or record.get("question_id") or "").strip()
        if not question_id:
            return False
        question_ids_by_supervisor.setdefault(supervisor, set()).add(question_id)
        return True

    driver = get_neo4j_driver(cfg)
    terminal_statuses = list(_TERMINAL_TASK_STATUSES)
    with driver.session(database=cfg.neo4j_db) as session:
        task_rows = session.run("""
            MATCH (p:OrchProject)-[:HAS_PHASE]->(:OrchPhase)-[:HAS_TASK]->(t:OrchTask)
            WITH p, t,
                 coalesce(toLower(trim(p.supervisor)), '') IN ['', 'unassigned', 'unknown', 'none', 'null'] AS owner_fallback,
                 CASE
                   WHEN coalesce(toLower(trim(p.supervisor)), '') IN ['', 'unassigned', 'unknown', 'none', 'null']
                   THEN toLower(trim(coalesce(t.owner, '')))
                   ELSE toLower(trim(p.supervisor))
                 END AS effective_supervisor
            WHERE coalesce(effective_supervisor, '') <> ''
              AND (coalesce(p.migration_exempt, false) = false OR owner_fallback)
              AND coalesce(toLower(trim(p.status)), '') IN ['active', 'in_progress']
              AND NOT (coalesce(t.status, 'pending') IN $terminal_statuses)
            RETURN effective_supervisor AS supervisor,
                   count(DISTINCT t) AS open_task_count,
                   count(DISTINCT CASE WHEN coalesce(t.status, '') = 'in_progress' THEN t END) AS in_progress_count,
                   count(DISTINCT CASE
                       WHEN toUpper(trim(coalesce(t.blocked_on, ''))) STARTS WITH 'AWAIT:' THEN t
                   END) AS await_count,
                   count(DISTINCT CASE
                       WHEN toUpper(trim(coalesce(t.blocked_on, ''))) STARTS WITH 'AWAIT:HUMAN-REVIEW:' THEN t
                   END) AS human_review_hold_count
        """, terminal_statuses=terminal_statuses)
        for record in task_rows:
            badge = row_for(record["supervisor"])
            if badge is None:
                continue
            badge["open_task_count"] += int(record["open_task_count"] or 0)
            badge["in_progress_count"] += int(record["in_progress_count"] or 0)
            badge["await_count"] += int(record["await_count"] or 0)
            badge["human_review_hold_count"] += int(record["human_review_hold_count"] or 0)

        dependency_rows = session.run("""
            MATCH (p:OrchProject)-[:HAS_PHASE]->(:OrchPhase)-[:HAS_TASK]->(t:OrchTask)-[:DEPENDS_ON]->(dep:OrchTask)
            WITH p, t, dep,
                 coalesce(toLower(trim(p.supervisor)), '') IN ['', 'unassigned', 'unknown', 'none', 'null'] AS owner_fallback,
                 CASE
                   WHEN coalesce(toLower(trim(p.supervisor)), '') IN ['', 'unassigned', 'unknown', 'none', 'null']
                   THEN toLower(trim(coalesce(t.owner, '')))
                   ELSE toLower(trim(p.supervisor))
                 END AS effective_supervisor
            WHERE coalesce(effective_supervisor, '') <> ''
              AND (coalesce(p.migration_exempt, false) = false OR owner_fallback)
              AND coalesce(toLower(trim(p.status)), '') IN ['active', 'in_progress']
              AND NOT (coalesce(t.status, 'pending') IN $terminal_statuses)
              AND NOT (coalesce(dep.status, 'pending') IN $terminal_statuses)
            RETURN effective_supervisor AS supervisor,
                   count(DISTINCT t) AS dependency_wait_count
        """, terminal_statuses=terminal_statuses)
        for record in dependency_rows:
            badge = row_for(record["supervisor"])
            if badge is not None:
                badge["dependency_wait_count"] += int(record["dependency_wait_count"] or 0)

        resolver_rows = session.run("""
            MATCH (p:OrchProject)-[:HAS_PHASE]->(:OrchPhase)-[:HAS_TASK]->(t:OrchTask)
            MATCH (resolver:OrchTask {id: t.blocked_on})
            WITH p, t, resolver,
                 coalesce(toLower(trim(p.supervisor)), '') IN ['', 'unassigned', 'unknown', 'none', 'null'] AS owner_fallback,
                 CASE
                   WHEN coalesce(toLower(trim(p.supervisor)), '') IN ['', 'unassigned', 'unknown', 'none', 'null']
                   THEN toLower(trim(coalesce(t.owner, '')))
                   ELSE toLower(trim(p.supervisor))
                 END AS effective_supervisor
            WHERE coalesce(effective_supervisor, '') <> ''
              AND (coalesce(p.migration_exempt, false) = false OR owner_fallback)
              AND coalesce(toLower(trim(p.status)), '') IN ['active', 'in_progress']
              AND NOT (coalesce(t.status, 'pending') IN $terminal_statuses)
              AND resolver.status IN ['in_progress', 'dispatched']
            RETURN effective_supervisor AS supervisor,
                   count(DISTINCT t) AS live_resolver_wait_count
        """, terminal_statuses=terminal_statuses)
        for record in resolver_rows:
            badge = row_for(record["supervisor"])
            if badge is not None:
                badge["live_resolver_wait_count"] += int(record["live_resolver_wait_count"] or 0)

        question_rows = session.run("""
            MATCH (p:OrchProject)-[:HAS_PHASE]->(:OrchPhase)-[:HAS_TASK]->(t:OrchTask)
            MATCH (q:OrchQuestion {status: 'open'})-[:CONCERNS_TASK]->(t)
            WITH p, t, q,
                 coalesce(toLower(trim(p.supervisor)), '') IN ['', 'unassigned', 'unknown', 'none', 'null'] AS owner_fallback,
                 CASE
                   WHEN coalesce(toLower(trim(p.supervisor)), '') IN ['', 'unassigned', 'unknown', 'none', 'null']
                   THEN toLower(trim(coalesce(t.owner, '')))
                   ELSE toLower(trim(p.supervisor))
                 END AS effective_supervisor
            WHERE coalesce(effective_supervisor, '') <> ''
              AND (coalesce(p.migration_exempt, false) = false OR owner_fallback)
              AND coalesce(toLower(trim(p.status)), '') IN ['active', 'in_progress']
              AND NOT (coalesce(t.status, 'pending') IN $terminal_statuses)
            RETURN effective_supervisor AS supervisor,
                   count(DISTINCT q) AS open_question_count,
                   collect(DISTINCT q.id) AS question_ids
        """, terminal_statuses=terminal_statuses)
        for record in question_rows:
            badge = row_for(record["supervisor"])
            if badge is None:
                continue
            supervisor = badge["supervisor"]
            ids = [str(item) for item in (record["question_ids"] or []) if item]
            question_ids_by_supervisor.setdefault(supervisor, set()).update(ids)
            unknown_questions_by_supervisor[supervisor] = (
                unknown_questions_by_supervisor.get(supervisor, 0)
                + max(0, int(record["open_question_count"] or 0) - len(ids))
            )

    from .config import get_redis_sync

    redis_client = get_redis_sync(cfg)
    for supervisor in list(badges):
        lineage = _chat_lineage(supervisor)
        open_questions = redis_client.lrange(_chat_key("openq", lineage), 0, -1) or []
        for raw_question in open_questions:
            if not add_question_id(supervisor, raw_question):
                unknown_questions_by_supervisor[supervisor] = unknown_questions_by_supervisor.get(supervisor, 0) + 1
        needs_you = redis_client.get(_chat_key("needs_you", lineage))
        if needs_you and not add_question_id(supervisor, needs_you) and not open_questions:
            unknown_questions_by_supervisor[supervisor] = unknown_questions_by_supervisor.get(supervisor, 0) + 1

    for badge in badges.values():
        supervisor = badge["supervisor"]
        question_ids = sorted(question_ids_by_supervisor.get(supervisor, set()))
        badge["question_ids"] = question_ids[:5]
        badge["open_question_count"] = len(question_ids) + unknown_questions_by_supervisor.get(supervisor, 0)
        reasons: List[str] = []
        if badge["open_question_count"]:
            reasons.append("open_question")
        if badge["human_review_hold_count"]:
            reasons.append("human_review_hold")
        if badge["in_progress_count"]:
            reasons.append("in_progress")
        if badge["await_count"]:
            reasons.append("await_signal")
        if badge["dependency_wait_count"]:
            reasons.append("dependency_wait")
        if badge["live_resolver_wait_count"]:
            reasons.append("live_resolver_wait")
        if badge["open_task_count"] and not reasons:
            reasons.append("open_work")
        badge["reasons"] = reasons
        if badge["open_question_count"] or badge["human_review_hold_count"]:
            badge["state"] = "NEEDS-YOU"
            badge["label"] = "needs you"
            badge["summary"] = (
                f"{badge['open_question_count']} open question(s) and "
                f"{badge['human_review_hold_count']} human-review hold(s) need a UI answer."
            )
        elif badge["open_task_count"]:
            badge["state"] = "ACTIVE"
            badge["label"] = "active"
            badge["summary"] = (
                f"{badge['open_task_count']} open task(s): "
                f"{', '.join(reasons)}."
            )
        else:
            badge["state"] = "IDLE"
            badge["label"] = "idle"
            badge["summary"] = "No open task or question is present on this supervisor's projects."
    return dict(sorted(badges.items()))


def get_project_ready_tasks(project_id: str, owner: Optional[str] = None,
                            config: Optional[OrchConfig] = None) -> List[Dict[str, Any]]:
    cfg = config or OrchConfig()
    project = _project_record(project_id, cfg)
    stop_state = _project_stop_reason_state(project)
    # Fail-closed normalized allowlist — unified with the readiness/wake sites
    # (get_session_current_work / get_session_next_ready / _raw_stop_decision). A
    # concluded project (stopped/completed/unknown) never surfaces ready work. This
    # replaces the prior weaker guard (stopped excluded only when stop_state.valid),
    # which let a stopped project with no valid stop reason leak ready tasks into
    # get_session_stop_status's WAKE_WITH_QUEUE.
    status_norm = str(project.get("status") or "").strip().lower()
    if status_norm not in ("active", "in_progress"):
        return []
    if stop_state["valid"]:
        return []
    owner_value = owner if owner is not None else str(project.get("supervisor") or "")
    if not owner_value:
        return []
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        result = session.run(f"""
            MATCH (p:OrchProject {{id: $project_id}})-[:HAS_PHASE]->(ph:OrchPhase)-[:HAS_TASK]->(t:OrchTask)
            WHERE t.status = 'pending'
              AND coalesce(t.owner, '') = $owner
              AND NOT (
                  coalesce(t.task_type, '') = $human_review_task_type
                  AND EXISTS {{
                      MATCH (q:OrchQuestion)-[:CONCERNS_TASK]->(t)
                      WHERE q.question_type = $human_review_question_type
                        AND q.status = 'open'
                        AND q.gate_task_id = t.id
                  }}
              )
              AND coalesce(t.blocked_on, '') = ''
              AND {_READY_DEPENDENCIES_SATISFIED_CYPHER}
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
            ORDER BY {_READY_TASK_ORDER_CYPHER}
        """,
            project_id=project_id,
            owner=owner_value,
            human_review_task_type=HUMAN_REVIEW_TASK_TYPE,
            human_review_question_type=HUMAN_REVIEW_QUESTION_TYPE,
        )
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


def complete_project(project_id: str, *, force: bool = False,
                     completed_by: str = "unknown",
                     closure_reason: Optional[str] = None,
                     config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    completed_actor = str(completed_by or "unknown").strip() or "unknown"
    reason = str(closure_reason or "").strip()
    if force and not reason:
        raise ValueError(
            'closure_reason is required when force=true. Use body '
            '{"force":true,"closure_reason":"<why>","completed_by":"<session-id>"} '
            f"with POST /api/projects/{project_id}/complete."
        )
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        record = session.run("""
            MATCH (p:OrchProject {id: $project_id})
            OPTIONAL MATCH (p)-[:HAS_PHASE]->(:OrchPhase)-[:HAS_TASK]->(t:OrchTask)
            WITH p, sum(CASE
                WHEN t IS NOT NULL AND coalesce(t.status, 'pending') <> 'completed' THEN 1
                ELSE 0
            END) AS incomplete_tasks
            WHERE $force OR incomplete_tasks = 0
            SET p.status = 'completed',
                p.completed_at = datetime(),
                p.completed_by = $completed_by,
                p.forced_closure = CASE
                    WHEN $force AND incomplete_tasks > 0 THEN true
                    ELSE false
                END,
                p.closure_reason = CASE
                    WHEN $force AND incomplete_tasks > 0 THEN $closure_reason
                    ELSE NULL
                END,
                p.updated_at = datetime()
            RETURN p.id AS id,
                   p.forced_closure AS forced_closure,
                   p.closure_reason AS closure_reason,
                   p.completed_by AS completed_by
        """,
            project_id=project_id,
            force=bool(force),
            closure_reason=reason,
            completed_by=completed_actor,
        ).single()
        if not record:
            _project_record(project_id, cfg)
            raise ReadyWorkConflictError(
                f"project {project_id} has incomplete tasks. Inspect remaining work with "
                f"`taey-plan show {project_id}` or GET /api/projects/{project_id}; complete tasks with "
                "`taey-task update <task-id> completed --evidence "
                '\'{"commit_sha":"<sha>","repo":"OWNER/REPO","production_observation":"<what you verified>"}\'`, '
                "or force close with body "
                '{"force":true,"closure_reason":"<why>","completed_by":"<session-id>"} '
                f"to POST /api/projects/{project_id}/complete."
            )
    return {
        "ok": True,
        "project_id": project_id,
        "status": "completed",
        "force": bool(force),
        "forced_closure": bool(record["forced_closure"]),
        "closure_reason": record["closure_reason"],
        "completed_by": record["completed_by"],
    }


def reset_project(project_id: str, *, reset_by: str = "unknown",
                  config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    cfg = config or OrchConfig()
    project = _project_record(project_id, cfg)
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        session.run("""
            MATCH (p:OrchProject {id: $project_id})
            SET p.status = 'active',
                p.completed_at = NULL,
                p.completed_by = NULL,
                p.forced_closure = false,
                p.closure_reason = NULL,
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


def update_project_priority(project_id: str, new_priority: int, set_by: str,
                            source_surface: str, reason: str,
                            config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    if not set_by or not source_surface or not reason.strip():
        raise PriorityAuditError(
            "set_by, source_surface, and reason are required. Use PATCH "
            f"/api/projects/{project_id} body "
            '{"priority":10,"set_by":"<session-id>","source_surface":"api","reason":"<why>"}; '
            "allowed source_surface values are the caller surface you are using, e.g. api, cli, or ui."
        )
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
    active_conditions = _active_conditions(conditions)
    active = _condition_lookup(active_conditions, condition_id)
    if not active:
        if active_conditions:
            choices = ", ".join(
                f"{condition['id']} v{int(condition['version'])} ({condition['label']})"
                for condition in active_conditions
            )
        else:
            choices = "none"
        raise ConditionValidationError(
            f"active condition {condition_id!r} not found for project {project_id}; "
            f"active conditions: {choices}. Inspect with "
            f"`taey-plan stop-conditions {project_id} get` or "
            f"GET /api/projects/{project_id}."
        )
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


def get_session_stop_status(session_id: str,
                            config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    cfg = config or OrchConfig()
    projects = get_session_supervised_projects(session_id, config=cfg)
    project_statuses: List[Dict[str, Any]] = []
    decision: Dict[str, Any] = {
        "can_stop": True,
        "next_action": f"No ready work or required stop reason is pending for {session_id}.",
    }
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
                "next_action": (
                    f"Run `taey-plan next {session_id}` or GET "
                    f"/api/sessions/{session_id}/next-ready; then work or dispatch the returned task."
                ),
            }
            break
        # Only an ACTIVE project with no ready work + no valid stop reason demands a
        # stop_reason. Normalized (strip+lower) + fail-closed: in_progress stays exempt
        # (work underway); concluded/unknown (stopped/completed/archived/NULL/case-variant)
        # must NOT fall through to WAKE_REASON_REQUIRED. Unifies this 7th status-decision
        # surface inside get_session_stop_status with the readiness/wake allowlist
        # from leaking into stop status.
        status_norm = str(project.get("status") or "").strip().lower()
        if status_norm != "active":
            continue
        if stop_state["valid"]:
            continue
        if stop_state["deprecated_only"]:
            continue
        decision = {
            "can_stop": False,
            "wake_type": "WAKE_REASON_REQUIRED",
            "wake_reason": f"stop_reason_required:{project['id']}",
            "next_action": (
                f"Set a stop reason with POST /api/projects/{project['id']}/stop-reason body "
                '{"condition_id":"<id>","condition_version":<version>,"detail":"<why>",'
                '"set_by":"<session-id>"} or continue work with '
                f"`taey-plan next {session_id}`."
            ),
        }
        break
    return {"projects": project_statuses, "decision": decision}


def set_session_pause(session_id: str, pause_source: str, pause_reason: str,
                      pause_expires_at: Optional[str] = None,
                      paused_by: Optional[str] = None,
                      config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    if pause_source not in _PAUSE_SOURCES:
        raise PauseValidationError(f"pause_source invalid. {PAUSE_NEXT_STEP}")
    r = _fleet_state_redis()
    expires_at = _parse_pause_expires_at(pause_expires_at)
    ttl_seconds: Optional[int] = None
    if expires_at is not None:
        delta = (expires_at - dt.datetime.now(dt.timezone.utc)).total_seconds()
        if delta <= 0:
            raise PauseValidationError(f"pause_expires_at must be in the future. {PAUSE_NEXT_STEP}")
        ttl_seconds = max(1, int(math.ceil(delta)))
    meta = {
        "paused_by": paused_by or session_id,
        "paused_at": _utc_now_iso(),
        "pause_source": pause_source,
        "pause_reason": pause_reason or "",
        "pause_expires_at": expires_at.isoformat() if expires_at else None,
    }
    pause_key = _state_key(session_id, "pause")
    meta_key = _state_key(session_id, "pause_meta")
    if ttl_seconds is None:
        r.set(pause_key, "1")
        r.set(meta_key, _json_encode(meta))
    else:
        r.set(pause_key, "1", ex=ttl_seconds)
        r.set(meta_key, _json_encode(meta), ex=ttl_seconds)
    return meta


def clear_session_pause(session_id: str, cleared_by: str,
                        config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    r = _fleet_state_redis()
    meta_key = _state_key(session_id, "pause_meta")
    pause_key = _state_key(session_id, "pause")
    meta_raw = r.get(meta_key)
    meta = _decode_json_field(meta_raw, {})
    meta["cleared_by"] = cleared_by
    meta["cleared_at"] = _utc_now_iso()
    r.delete(pause_key)
    r.delete(meta_key)
    return meta


def get_agent_tasks(agent_id: str, config: Optional[OrchConfig] = None) -> List[Dict[str, Any]]:
    """Get tasks owned by an agent."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        result = session.run("""
            MATCH (t:OrchTask)
            WHERE (t.owner = $agent_id OR t.dispatched_to = $agent_id)
              AND t.status IN ['pending', 'in_progress']
            RETURN t.id AS id, t.description AS description,
                   t.status AS status, t.priority AS priority
            ORDER BY coalesce(t.priority, 999999999) ASC
            LIMIT 10
        """, agent_id=agent_id)
        return [dict(r) for r in result]


def _chat_lineage(value: str) -> str:
    lineage = str(value or "").strip()
    if not lineage:
        raise ValueError(f"lineage must be non-empty. {CHAT_NEXT_STEP}")
    if len(lineage) > 160:
        raise ValueError(f"lineage must be <= 160 characters. {CHAT_NEXT_STEP}")
    if not all(ch.isalnum() or ch in "._-" for ch in lineage) or ".." in lineage:
        raise ValueError(f"lineage contains unsupported characters. {CHAT_NEXT_STEP}")
    return lineage


def _chat_key(kind: str, lineage: str) -> str:
    prefix = os.environ.get("NOTIFY_KEY_PREFIX", "taey")
    return f"{prefix}:{kind}:{_chat_lineage(lineage)}"


def _surface_question_to_chat(question: Dict[str, Any], *,
                              config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    from .config import get_redis_sync

    lineage = _chat_lineage(str(question.get("lineage") or question.get("reviewer") or question.get("asked_by") or "supervisor"))
    reason = str(question.get("text") or "").strip()
    if not reason:
        raise ValueError(
            "question text must be non-empty. Use POST /api/human-review-gates body "
            '{"phase_id":"<phase-id>","task_id":"<task-id>","prompt":"<review question>",'
            '"reviewer":"<session-id>"} or answer an existing gate through `/ui/`.'
        )
    record = {
        "id": str(question["id"]),
        "lineage": lineage,
        "session": str(question.get("reviewer") or lineage).strip() or lineage,
        "reason": reason,
        "status": "open",
        "ts": _utc_now_iso(),
        "source": "orch_question",
        "task_id": str(question.get("task_id") or ""),
        "gate_task_id": str(question.get("gate_task_id") or question.get("task_id") or ""),
        "question_id": str(question["id"]),
    }
    encoded = _json_encode(record)
    message = {
        "id": uuid.uuid4().hex,
        "lineage": lineage,
        "sender": record["session"],
        "role": "system",
        "type": "escalation",
        "text": reason,
        "metadata": {"open_question_id": record["id"], "needs_you": True, "source": "orch_question"},
        "ts": record["ts"],
    }
    r = get_redis_sync(config)
    r.rpush(_chat_key("openq", lineage), encoded)
    r.set(_chat_key("needs_you", lineage), encoded)
    r.rpush(_chat_key("chat", lineage), _json_encode(message))
    return record


def _resolve_chat_question(question_id: str, *,
                           config: Optional[OrchConfig] = None,
                           lineage: str = "") -> None:
    from .config import get_redis_sync

    if not lineage:
        return
    line = _chat_lineage(lineage)
    r = get_redis_sync(config)
    key = _chat_key("openq", line)
    remaining: List[str] = []
    for raw in r.lrange(key, 0, -1):
        try:
            record = json.loads(raw)
        except Exception:
            remaining.append(raw)
            continue
        if not isinstance(record, dict) or str(record.get("id") or record.get("question_id") or "") != question_id:
            remaining.append(raw)
    pipe = r.pipeline()
    pipe.delete(key)
    if remaining:
        pipe.rpush(key, *remaining)
    needs_key = _chat_key("needs_you", line)
    needs_raw = r.get(needs_key)
    if needs_raw:
        try:
            needs = json.loads(needs_raw)
        except Exception:
            needs = {}
        if isinstance(needs, dict) and str(needs.get("id") or needs.get("question_id") or "") == question_id:
            pipe.delete(needs_key)
    pipe.execute()


def _notify_human_review_gate(reviewer: str, prompt: str, *, requested_by: str,
                              task_id: str, question_id: str,
                              config: Optional[OrchConfig] = None) -> None:
    if not reviewer:
        return
    cfg = config or OrchConfig()
    body = (
        f"HUMAN REVIEW NEEDED [{task_id}]\n"
        f"question_id: {question_id}\n"
        f"{prompt}"
    )
    result = subprocess.run(
        [cfg.notify_cli_path, reviewer, body, "--from", requested_by or "orch-human-review",
         "--type", "escalation", "--priority", "high"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"{cfg.notify_cli_path} failed")


def create_question(question_id: str, text: str, context: str = "",
                    task_id: str = "", asked_by: str = "",
                    reviewer: str = "", lineage: str = "",
                    question_type: str = "question",
                    gate_task_id: str = "",
                    refs: Optional[List[Dict[str, Any]]] = None,
                    surface: bool = False,
                    config: Optional[OrchConfig] = None) -> str:
    """Create an OrchQuestion node linked to a task."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        # No f-string in the executed query (F19 defense-in-depth): the query is built
        # by concatenating only developer-controlled CONSTANTS; every user value
        # (id/text/context/task_id/asked_by) is a Cypher parameter, never interpolated.
        _BASE_Q = (
            "MERGE (q:OrchQuestion {id: $id}) "
            "SET q.text = $text, q.context = $context, q.task_id = $task_id, "
            "q.asked_by = $asked_by, q.reviewer = $reviewer, q.lineage = $lineage, "
            "q.question_type = $question_type, q.gate_task_id = $gate_task_id, "
            "q.refs = $refs, q.status = 'open', q.created_at = datetime() "
        )
        _LINK_TASK = (
            "WITH q MATCH (t:OrchTask {id: $task_id}) "
            "MERGE (q)-[:CONCERNS_TASK]->(t) "
        )
        if task_id:
            task_record = session.run(
                "MATCH (t:OrchTask {id: $task_id}) RETURN t.id AS id",
                task_id=task_id,
            ).single()
            if task_record is None:
                raise TaskParentNotFoundError(
                    f"task '{task_id}' not found; cannot create question '{question_id}'. "
                    f"Inspect with `taey-task status {task_id}` or GET /api/tasks/{task_id}; "
                    "create a human-review gate with POST /api/human-review-gates body "
                    '{"phase_id":"<phase-id>","task_id":"<task-id>","prompt":"<review question>",'
                    '"reviewer":"<session-id>"} after the task exists.'
                )
            query = _BASE_Q + _LINK_TASK + "RETURN q.id AS id"
        else:
            query = _BASE_Q + "RETURN q.id AS id"

        result = session.run(
            query, id=question_id, text=text, context=context,
            task_id=task_id, asked_by=asked_by, reviewer=reviewer,
            lineage=lineage, question_type=question_type,
            gate_task_id=gate_task_id, refs=_encode_refs_or_none(refs))
        record = result.single()
        if record is None:
            raise TaskParentNotFoundError(
                f"task '{task_id}' not found; cannot create question '{question_id}'. "
                f"Inspect with `taey-task status {task_id}` or GET /api/tasks/{task_id}; "
                "create a human-review gate with POST /api/human-review-gates body "
                '{"phase_id":"<phase-id>","task_id":"<task-id>","prompt":"<review question>",'
                '"reviewer":"<session-id>"} after the task exists.'
            )
        if surface:
            _surface_question_to_chat({
                "id": question_id,
                "text": text,
                "task_id": task_id,
                "gate_task_id": gate_task_id,
                "asked_by": asked_by,
                "reviewer": reviewer,
                "lineage": lineage,
            }, config=cfg)
        return record["id"]


def answer_question(question_id: str, answer: str, answered_by: str,
                    config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    """Provide an answer to an open question."""
    if not str(answer or "").strip():
        raise ValueError(
            f"answer must be non-empty. POST /api/questions/{question_id}/answer with body "
            '{"answer":"<answer text>","answered_by":"<session-id>"}; for human-review '
            "gate completion use `/ui/` or "
            f"POST /api/ui/questions/{question_id}/answer with the same body."
        )
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        found = session.run("""
            MATCH (q:OrchQuestion {id: $id})
            RETURN q.id AS id, q.question_type AS question_type,
                   q.gate_task_id AS gate_task_id, q.lineage AS lineage,
                   q.reviewer AS reviewer, properties(q) AS props
        """, id=question_id).single()
        if found is None:
            return {"ok": False, "question_id": question_id, "gate_completed": False, "verified": False}
        record = dict(found)
        is_human_review_gate = record.get("question_type") == "human_review_gate"
        if is_human_review_gate:
            props = record.get("props") if isinstance(record.get("props"), dict) else {}
            unverified = _decode_json_field(props.get("unverified_answers"), [])
            if not isinstance(unverified, list):
                unverified = []
            unverified.append({
                "answer": str(answer).strip(),
                "answered_by": answered_by,
                "received_at": _utc_now_iso(),
                "verified": False,
                "source": "unauthenticated_api",
            })
            session.run("""
                MATCH (q:OrchQuestion {id: $id})
                SET q.unverified_answer = $answer,
                    q.unverified_answered_by = $answered_by,
                    q.unverified_answered_at = datetime(),
                    q.unverified_answers = $unverified_answers,
                    q.status = 'open'
            """,
                id=question_id,
                answer=str(answer).strip(),
                answered_by=answered_by,
                unverified_answers=_json_encode(unverified),
            )
            return {
                "ok": True,
                "question_id": question_id,
                "answered_by": answered_by,
                "verified": False,
                "gate_completed": False,
                "question_type": record.get("question_type"),
                "gate_task_id": record.get("gate_task_id") or "",
                "next_step": _human_review_answer_next_step(question_id),
            }
        result = session.run("""
            MATCH (q:OrchQuestion {id: $id})
            SET q.answer = $answer,
                q.answered_by = $answered_by,
                q.status = 'answered',
                q.answered_at = datetime()
            RETURN q.id AS id, q.question_type AS question_type,
                   q.gate_task_id AS gate_task_id, q.lineage AS lineage,
                   q.reviewer AS reviewer
        """, id=question_id, answer=answer, answered_by=answered_by)
        record = result.single()
    if record is None:
        return {"ok": False, "question_id": question_id, "gate_completed": False, "verified": False}
    lineage = str(record.get("lineage") or record.get("reviewer") or "")
    _resolve_chat_question(question_id, config=cfg, lineage=lineage)
    return {
        "ok": True,
        "question_id": question_id,
        "answered_by": answered_by,
        "verified": False,
        "gate_completed": False,
        "question_type": record.get("question_type"),
        "gate_task_id": record.get("gate_task_id") or "",
        "next_step": _human_review_answer_next_step(question_id),
    }


def complete_human_review_gate(question_id: str, answer: str, answered_by: str,
                               config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    """Single-user UI path for completing a human-review gate.

    Normal agent/CLI task-completion paths cannot accidentally complete a
    human-review task; the dashboard's dedicated review action can.
    """
    answer_text = str(answer or "").strip()
    reviewer = str(answered_by or "").strip() or "dashboard-ui"
    if not answer_text:
        raise ValueError(
            f"answer must be non-empty. POST /api/ui/questions/{question_id}/answer with body "
            '{"answer":"<verdict>","answered_by":"<reviewer>"} or use `/ui/`.'
        )
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    evidence = _normalize_completion_evidence({
        "production_observation": f"human-review verdict recorded via dashboard UI by {reviewer}",
    })
    with driver.session(database=cfg.neo4j_db) as session:
        row = session.run("""
            MATCH (q:OrchQuestion {id: $question_id})-[:CONCERNS_TASK]->(t:OrchTask {id: q.gate_task_id})
            WHERE q.question_type = 'human_review_gate'
              AND t.task_type = 'human-review'
            SET q.answer = $answer,
                q.answered_by = $answered_by,
                q.answered_at = datetime(),
                q.status = 'answered',
                q.verified = true,
                q.answer_source = 'dashboard_ui',
                t.status = 'completed',
                t.completion_evidence = $completion_evidence,
                t.completed_by = $answered_by,
                t.completed_at = datetime(),
                t.blocked_on = NULL,
                t.updated_at = datetime()
            RETURN q.id AS question_id, q.lineage AS lineage, q.reviewer AS reviewer,
                   t.id AS gate_task_id, t.status AS task_status
        """,
            question_id=question_id,
            answer=answer_text,
            answered_by=reviewer,
            completion_evidence=_json_encode(evidence),
        ).single()
        if row is None:
            return {"ok": False, "question_id": question_id, "gate_completed": False, "verified": False}
        gate_task_id = row["gate_task_id"]
        session.run("""
            MATCH (p:OrchProject)-[:HAS_PHASE]->(:OrchPhase)-[:HAS_TASK]->(t:OrchTask {id: $task_id})
            WITH p
            OPTIONAL MATCH (p)-[:HAS_PHASE]->(:OrchPhase)-[:HAS_TASK]->(sibling:OrchTask)
            WHERE sibling.id <> $task_id AND sibling.status = 'in_progress'
            WITH p, count(sibling) AS in_progress_siblings
            SET p.in_progress_heartbeat_at = CASE
                    WHEN in_progress_siblings = 0 THEN ''
                    ELSE p.in_progress_heartbeat_at
                END,
                p.status = CASE
                    WHEN p.status = 'in_progress' AND in_progress_siblings = 0 THEN 'active'
                    ELSE p.status
                END,
                p.updated_at = datetime()
        """, task_id=gate_task_id)
    lineage = str(row.get("lineage") or row.get("reviewer") or "")
    _resolve_chat_question(question_id, config=cfg, lineage=lineage)
    return {
        "ok": True,
        "question_id": question_id,
        "answered_by": reviewer,
        "verified": True,
        "gate_completed": True,
        "question_type": "human_review_gate",
        "gate_task_id": row["gate_task_id"],
    }


def create_human_review_gate(
    *,
    phase_id: str,
    task_id: str,
    question_id: str,
    prompt: str,
    reviewer: str,
    requested_by: str,
    refs: Optional[List[Dict[str, Any]]] = None,
    priority: int = 50,
    notify: bool = True,
    config: Optional[OrchConfig] = None,
) -> Dict[str, Any]:
    task = create_task(
        phase_id=phase_id,
        task_id=task_id,
        description=prompt,
        priority=priority,
        owner=requested_by,
        created_by=requested_by,
        task_type="human-review",
        refs=refs,
        wake_owner_if_ready=False,
        config=config,
    )
    question = create_question(
        question_id=question_id,
        text=prompt,
        context=_json_encode({"refs": _normalize_refs(refs)}),
        task_id=task,
        asked_by=requested_by,
        reviewer=reviewer,
        lineage=reviewer,
        question_type="human_review_gate",
        gate_task_id=task,
        refs=refs,
        surface=True,
        config=config,
    )
    if notify:
        _notify_human_review_gate(reviewer, prompt, requested_by=requested_by,
                                  task_id=task, question_id=question, config=config)
    return {"task_id": task, "question_id": question, "reviewer": reviewer}


def get_open_questions(config: Optional[OrchConfig] = None) -> List[Dict[str, Any]]:
    """Get all questions with status 'open'."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        result = session.run("""
            MATCH (q:OrchQuestion {status: 'open'})
            RETURN q.id AS id, q.text AS text, q.context AS context,
                   q.task_id AS task_id, q.asked_by AS asked_by,
                   q.reviewer AS reviewer, q.question_type AS question_type,
                   q.gate_task_id AS gate_task_id, q.created_at AS created_at
            ORDER BY q.created_at ASC
        """)
        return [dict(r) for r in result]
