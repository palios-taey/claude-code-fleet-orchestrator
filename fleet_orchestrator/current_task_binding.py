"""Shared current_task binding lifecycle helpers."""
from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, Optional

from redis import RedisError, WatchError

from .config import OrchConfig, get_neo4j_driver
from .notify_state import redis_connect, state_key

LOG = logging.getLogger(__name__)

LIVE_BINDING_TASK_STATUSES = {"in_progress", "dispatched"}


def task_status(task_id: str, *, config: Optional[OrchConfig] = None) -> Optional[str]:
    if not task_id:
        return None
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        row = session.run(
            "MATCH (t:OrchTask {id: $task_id}) RETURN coalesce(t.status, 'pending') AS status",
            task_id=task_id,
        ).single()
    return str(row["status"]) if row else None


def is_live_binding_status(status: Optional[str]) -> bool:
    return str(status or "").strip().lower() in LIVE_BINDING_TASK_STATUSES


def decode_current_task(raw: Any) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def clear_matching_current_task(
    worker: str,
    task_id: str,
    *,
    redis_client: Any = None,
    config: Optional[OrchConfig] = None,  # noqa: ARG001 - keeps call sites symmetric with task_status.
    reason: str = "",
) -> bool:
    if not worker or not task_id:
        return False
    try:
        r = redis_client or redis_connect()
        key = state_key(worker, "current_task")
        for attempt in range(5):
            with r.pipeline() as pipe:
                try:
                    pipe.watch(key)
                    current = decode_current_task(pipe.get(key))
                    if not current or str(current.get("task_id") or "") != task_id:
                        pipe.unwatch()
                        return False
                    pipe.multi()
                    pipe.delete(key)
                    pipe.execute()
                    suffix = f" reason={reason}" if reason else ""
                    LOG.warning("cleared current_task binding worker=%s task=%s%s", worker, task_id, suffix)
                    return True
                except WatchError:
                    time.sleep(0.01 * (attempt + 1))
                    continue
        return False
    except RedisError as exc:
        LOG.warning("current_task clear failed worker=%s task=%s: %s", worker, task_id, exc)
        return False


def clear_session_current_task(session_id: str, *, redis_client: Any = None) -> Dict[str, Any]:
    r = redis_client or redis_connect()
    current_key = state_key(session_id, "current_task")
    outcome_key = state_key(session_id, "last_outcome")
    raw_current = r.get(current_key)
    raw_outcome = r.get(outcome_key)
    current = decode_current_task(raw_current) or {}
    outcome = decode_current_task(raw_outcome) or {}
    deleted = int(r.delete(current_key, outcome_key) or 0)
    return {
        "session": session_id,
        "cleared": deleted > 0,
        "deleted_keys": deleted,
        "previous_task_id": str(current.get("task_id") or ""),
        "previous_outcome": str(outcome.get("outcome") or ""),
    }
