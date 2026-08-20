"""Derived current-task liveness for supervisor status surfaces."""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional

from redis import RedisError

from .config import OrchConfig
from .inflight import active_turn_valid_for_task
from .notify_state import redis_connect, state_key
from .worker_liveness import worker_task_liveness_ttl_secs

LOGGER = logging.getLogger(__name__)


def _float_or_none(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode()
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _current_task_payload(raw: Any) -> Dict[str, Any]:
    if raw in (None, ""):
        return {}
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _task_record(task_id: str, config: Optional[OrchConfig]) -> Dict[str, Any]:
    if not task_id:
        return {}
    from .orch_schema import get_task

    return get_task(task_id, config=config) or {}


def _liveness_worker(session_id: str, task: Dict[str, Any]) -> str:
    dispatched_to = str(task.get("dispatched_to") or "").strip()
    owner = str(task.get("owner") or "").strip()
    if dispatched_to:
        return dispatched_to
    if owner:
        return owner
    return session_id


def current_task_liveness(session_id: str,
                          work: Optional[Dict[str, Any]],
                          *,
                          now: Optional[float] = None,
                          config: Optional[OrchConfig] = None) -> Optional[Dict[str, Any]]:
    """Derive current-task handoff confidence from existing notify-state signals."""
    if not work:
        return None
    checked_at = float(now if now is not None else time.time())
    task_id = str(work.get("top_task_id") or "")
    task = _task_record(task_id, config)
    worker = _liveness_worker(session_id, task)
    redis_client = redis_connect()
    try:
        current_payload = _current_task_payload(redis_client.get(state_key(worker, "current_task")))
        last_activity = _float_or_none(redis_client.get(state_key(worker, "last_activity")))
        idle = redis_client.get(state_key(worker, "idle")) is not None
    except RedisError:
        current_payload = {}
        last_activity = None
        idle = None

    dispatch_started_at = _float_or_none(task.get("worker_liveness_started_at"))
    if dispatch_started_at is None and str(current_payload.get("task_id") or "") == task_id:
        dispatch_started_at = _float_or_none(current_payload.get("started_at"))

    ttl = _float_or_none(task.get("worker_liveness_ttl_secs"))
    if ttl is None:
        ttl = float(worker_task_liveness_ttl_secs())
    ttl = max(1.0, ttl)

    current_matches = str(current_payload.get("task_id") or "") == task_id
    # Use shared validator: requires matching turn_context task_id + valid ctx + future score.
    # Naked future ZSET without valid ctx/task match does not make it working.
    has_valid_active_turn = current_matches and active_turn_valid_for_task(
        redis_client, worker, task_id, checked_at
    )
    if has_valid_active_turn:
        state = "working"
        age_base = last_activity or dispatch_started_at or checked_at
        age_seconds = int(max(0.0, checked_at - age_base))
        summary = "active turn (durable lease)"
    elif dispatch_started_at is None or last_activity is None or last_activity <= dispatch_started_at:
        state = "awaiting_start"
        age_seconds = int(max(0.0, checked_at - dispatch_started_at)) if dispatch_started_at is not None else None
        summary = "dispatched, peer not yet started"
        if age_seconds is not None:
            summary = f"dispatched {age_seconds}s ago, peer not yet started"
    else:
        age_seconds = int(max(0.0, checked_at - last_activity))
        if checked_at - last_activity >= ttl:
            state = "stale"
            summary = f"no activity {age_seconds}s (possibly wedged/stopped)"
        else:
            state = "working"
            summary = f"active {age_seconds}s ago"

    return {
        "state": state,
        "label": state.upper().replace("_", " "),
        "summary": summary,
        "worker": worker,
        "task_id": task_id or None,
        "idle": idle,
        "last_activity": last_activity,
        "dispatch_started_at": dispatch_started_at,
        "age_seconds": age_seconds,
        "threshold_seconds": int(ttl),
    }


def safe_current_task_liveness(session_id: str,
                               work: Optional[Dict[str, Any]],
                               *,
                               now: Optional[float] = None,
                               config: Optional[OrchConfig] = None) -> Optional[Dict[str, Any]]:
    """Best-effort liveness sidecar for status endpoints.

    Current work is authoritative; liveness is diagnostic. A Redis/Neo4j sidecar
    failure must not turn a valid current-task read into a 500.
    """
    try:
        return current_task_liveness(session_id, work, now=now, config=config)
    except Exception as exc:
        LOGGER.warning("current-task liveness unavailable session=%s error=%s", session_id, exc)
        return None
