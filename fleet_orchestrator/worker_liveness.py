from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from .config import OrchConfig, get_neo4j_driver, get_redis_sync


DEFAULT_WORKER_TASK_LIVENESS_TTL_SECS = 300
LOG = logging.getLogger(__name__)


def worker_task_liveness_enabled() -> bool:
    raw = os.environ.get("ORCH_WORKER_TASK_LIVENESS", "1").strip().lower()
    return raw not in {"0", "false", "no", "off"}


def worker_task_liveness_ttl_secs() -> int:
    raw = os.environ.get("ORCH_WORKER_TASK_LIVENESS_TTL_SEC", "")
    try:
        value = int(raw)
    except (TypeError, ValueError):
        value = DEFAULT_WORKER_TASK_LIVENESS_TTL_SECS
    return max(1, value)


def worker_task_liveness_key(task_id: str) -> str:
    prefix = os.environ.get("NOTIFY_KEY_PREFIX", "taey")
    return f"{prefix}:worker-task-liveness:{task_id}"


def worker_task_liveness_dedup_key(task_id: str) -> str:
    prefix = os.environ.get("NOTIFY_KEY_PREFIX", "taey")
    return f"{prefix}:worker-task-liveness-escalated:{task_id}"


def _state_key(node_id: str, suffix: str) -> str:
    prefix = os.environ.get("NOTIFY_KEY_PREFIX", "taey")
    return f"{prefix}:{node_id}:{suffix}"


def register_worker_task_liveness(
    worker: str,
    task_id: str,
    description: str,
    supervisor: Optional[str],
    started_at: float,
    *,
    ttl_secs: Optional[int] = None,
    config: Optional[OrchConfig] = None,
) -> bool:
    if not worker_task_liveness_enabled():
        return False
    ttl = max(1, int(ttl_secs or worker_task_liveness_ttl_secs()))
    payload: Dict[str, Any] = {
        "task_id": task_id,
        "worker": worker,
        "description": description,
        "supervisor": supervisor or "",
        "dispatch_started_at": started_at,
        "heartbeat_at": started_at,
        "heartbeat_ttl_secs": ttl,
        "ack_at": None,
    }
    cfg = config or OrchConfig()
    try:
        driver = get_neo4j_driver(cfg)
        with driver.session(database=cfg.neo4j_db) as session:
            record = session.run(
                """
                MATCH (t:OrchTask {id: $task_id})
                SET t.worker_liveness_worker = $worker,
                    t.worker_liveness_supervisor = $supervisor,
                    t.worker_liveness_started_at = $started_at,
                    t.worker_liveness_heartbeat_at = $started_at,
                    t.worker_liveness_ttl_secs = $ttl,
                    t.worker_liveness_ack_at = NULL,
                    t.worker_liveness_escalated_at = NULL,
                    t.worker_liveness_escalation_reason = NULL,
                    t.updated_at = datetime()
                RETURN t.id AS id
                """,
                task_id=task_id,
                worker=worker,
                supervisor=supervisor or "",
                started_at=float(started_at),
                ttl=ttl,
            ).single()
    except Exception as exc:
        LOG.warning("worker task liveness registration failed task=%s worker=%s: %s",
                    task_id, worker, exc)
        return False
    if record is None:
        return False
    try:
        get_redis_sync(cfg).set(
            worker_task_liveness_key(task_id),
            json.dumps(payload, separators=(",", ":")),
        )
    except Exception as exc:
        LOG.warning("worker task liveness Redis sidecar failed task=%s worker=%s: %s",
                    task_id, worker, exc)
    return True


def clear_worker_task_liveness(task_id: str, *, config: Optional[OrchConfig] = None) -> None:
    if not task_id:
        return
    try:
        get_redis_sync(config).delete(worker_task_liveness_key(task_id))
    except Exception:
        return


def _json_dict(raw: Any) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _current_task_id(r, worker: str) -> Optional[str]:
    current = _json_dict(r.get(_state_key(worker, "current_task")))
    if not current:
        return None
    task_id = current.get("task_id")
    return str(task_id) if task_id else None


def _last_outcome_terminal_for_current_task(r, worker: str, task_id: str) -> bool:
    if _current_task_id(r, worker) != task_id:
        return False
    outcome = _json_dict(r.get(_state_key(worker, "last_outcome"))) or {}
    return str(outcome.get("outcome") or "").strip().lower() in {"done", "error", "interrupted"}


def _fresh_tool_heartbeat_for_current_task(r, worker: str, task_id: str,
                                           ttl_secs: int, now: float) -> bool:
    if _current_task_id(r, worker) != task_id:
        return False
    raw = r.get(_state_key(worker, "last_tool_activity"))
    if raw is None:
        return False
    try:
        return now - float(raw) < ttl_secs
    except (TypeError, ValueError):
        return False


def _mark_liveness_heartbeat(task_id: str, worker: str, now: float, ttl_secs: int,
                             *, config: Optional[OrchConfig] = None) -> None:
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        session.run(
            """
            MATCH (t:OrchTask {id: $task_id})
            SET t.worker_liveness_heartbeat_at = $now,
                t.worker_liveness_ack_at = coalesce(t.worker_liveness_ack_at, $now),
                t.updated_at = datetime()
            """,
            task_id=task_id,
            now=float(now),
        )
    payload = {
        "task_id": task_id,
        "worker": worker,
        "heartbeat_at": now,
        "heartbeat_ttl_secs": ttl_secs,
        "ack_at": now,
    }
    get_redis_sync(cfg).set(
        worker_task_liveness_key(task_id),
        json.dumps(payload, separators=(",", ":")),
    )


def _clear_matching_current_task(r, worker: str, task_id: str) -> None:
    raw = r.get(_state_key(worker, "current_task"))
    current = _json_dict(raw)
    if current and str(current.get("task_id") or "") == task_id:
        r.delete(_state_key(worker, "current_task"))


def _escalate_task(task: Dict[str, Any], now: float,
                   *, config: Optional[OrchConfig] = None) -> Optional[Dict[str, Any]]:
    task_id = str(task.get("task_id") or "")
    worker = str(task.get("worker") or "")
    if not task_id or not worker:
        return None
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        record = session.run(
            """
            MATCH (t:OrchTask {id: $task_id})
            WHERE t.status = 'in_progress'
              AND t.worker_liveness_worker = $worker
            SET t.status = 'pending',
                t.dispatched_to = NULL,
                t.blocked_on = NULL,
                t.needs_attention = true,
                t.worker_liveness_escalated_at = $now,
                t.worker_liveness_escalation_reason = 'worker-task-liveness-ttl',
                t.updated_at = datetime()
            RETURN t.id AS task_id,
                   t.description AS description,
                   t.owner AS owner,
                   t.worker_liveness_supervisor AS supervisor
            """,
            task_id=task_id,
            worker=worker,
            now=float(now),
        ).single()
    if record is None:
        return None
    r = get_redis_sync(cfg)
    r.delete(worker_task_liveness_key(task_id))
    _clear_matching_current_task(r, worker, task_id)
    result = dict(record)
    result["worker"] = worker
    result["stale_for_sec"] = int(max(0.0, now - float(task.get("heartbeat_at") or now)))
    return result


def _task_is_declared_wait(task: Dict[str, Any], *, config: Optional[OrchConfig] = None) -> bool:
    from .orch_schema import is_awaiting_human_review_gate, is_declared_await_signal

    if is_declared_await_signal(task.get("blocked_on")):
        return True
    return is_awaiting_human_review_gate(task, config=config)


def _registered_in_progress_tasks(*, config: Optional[OrchConfig] = None) -> List[Dict[str, Any]]:
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        return [dict(record) for record in session.run(
            """
            MATCH (p:OrchProject)-[:HAS_PHASE]->(:OrchPhase)-[:HAS_TASK]->(t:OrchTask)
            WHERE t.status = 'in_progress'
              AND coalesce(t.worker_liveness_worker, '') <> ''
              AND coalesce(t.worker_liveness_started_at, 0.0) > 0.0
            RETURN t.id AS task_id,
                   t.description AS description,
                   t.owner AS owner,
                   t.blocked_on AS blocked_on,
                   t.task_type AS task_type,
                   t.dispatched_to AS dispatched_to,
                   t.worker_liveness_worker AS worker,
                   t.worker_liveness_supervisor AS supervisor,
                   coalesce(t.worker_liveness_heartbeat_at, t.worker_liveness_started_at) AS heartbeat_at,
                   coalesce(t.worker_liveness_ttl_secs, $default_ttl) AS ttl_secs,
                   p.id AS project_id,
                   p.supervisor AS project_supervisor
            """,
            default_ttl=DEFAULT_WORKER_TASK_LIVENESS_TTL_SECS,
        )]


def escalate_stale_worker_tasks(now: Optional[float] = None,
                                *, config: Optional[OrchConfig] = None) -> List[Dict[str, Any]]:
    if not worker_task_liveness_enabled():
        return []
    cfg = config or OrchConfig()
    r = get_redis_sync(cfg)
    current_time = time.time() if now is None else float(now)
    escalated: List[Dict[str, Any]] = []
    for task in _registered_in_progress_tasks(config=cfg):
        if _task_is_declared_wait(task, config=cfg):
            continue
        task_id = str(task.get("task_id") or "")
        worker = str(task.get("worker") or "")
        try:
            ttl_secs = max(1, int(task.get("ttl_secs") or worker_task_liveness_ttl_secs()))
            heartbeat_at = float(task.get("heartbeat_at") or 0.0)
        except (TypeError, ValueError):
            continue
        if _last_outcome_terminal_for_current_task(r, worker, task_id):
            continue
        if _fresh_tool_heartbeat_for_current_task(r, worker, task_id, ttl_secs, current_time):
            _mark_liveness_heartbeat(task_id, worker, current_time, ttl_secs, config=cfg)
            continue
        if heartbeat_at <= 0 or current_time - heartbeat_at < ttl_secs:
            continue
        result = _escalate_task(task, current_time, config=cfg)
        if result:
            escalated.append(result)
    return escalated
