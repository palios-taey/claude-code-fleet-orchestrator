from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional

from redis import RedisError, WatchError

from .config import OrchConfig, get_neo4j_driver
from .current_task_binding import clear_matching_current_task
from .inflight import InFlightProbeError, active_inflight_signal, task_actively_in_flight
from .notify_state import key as _notify_key
from .notify_state import redis_connect as _notify_redis_connect
from .notify_state import state_key as _notify_state_key


DEFAULT_WORKER_TASK_LIVENESS_TTL_SECS = 300
LOG = logging.getLogger(__name__)
OUTWARD_EFFECT_CLASSES = frozenset({"outward_irreversible", "outward_action"})


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
    return _notify_key(f"worker-task-liveness:{task_id}")


def worker_task_liveness_dedup_key(task_id: str) -> str:
    return _notify_key(f"worker-task-liveness-escalated:{task_id}")


def _state_key(node_id: str, suffix: str) -> str:
    return _notify_state_key(node_id, suffix)


def _redis_connect():
    return _notify_redis_connect()


def register_worker_task_liveness(
    worker: str,
    task_id: str,
    description: str,
    supervisor: Optional[str],
    started_at: float,
    *,
    ttl_secs: Optional[int] = None,
    config: Optional[OrchConfig] = None,
    require_binding: bool = False,
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
        "claim_bound": require_binding,
    }
    cfg = config or OrchConfig()
    try:
        driver = get_neo4j_driver(cfg)
        with driver.session(database=cfg.neo4j_db) as session:
            record = session.run(
                """
                MATCH (t:OrchTask {id: $task_id})
                WHERE $require_binding = false
                   OR (t.status = 'in_progress'
                       AND t.dispatch_claim_worker = $worker
                       AND t.dispatch_claim_started_at = $started_at)
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
                require_binding=bool(require_binding),
            ).single()
    except Exception as exc:
        LOG.warning("worker task liveness registration failed task=%s worker=%s: %s",
                    task_id, worker, exc)
        return False
    if record is None:
        return False
    if require_binding:
        return _set_liveness_sidecar_if_current_binding(
            worker,
            task_id,
            started_at,
            payload,
        )
    try:
        _redis_connect().set(
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
        _redis_connect().delete(worker_task_liveness_key(task_id))
    except Exception as exc:
        LOG.warning("worker task liveness Redis cleanup failed task=%s: %s", task_id, exc)
        return


def _json_dict(raw: Any) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _current_task_payload(r, worker: str) -> Optional[Dict[str, Any]]:
    return _json_dict(r.get(_state_key(worker, "current_task")))


def _set_liveness_sidecar_if_current_binding(
    worker: str,
    task_id: str,
    binding_nonce: float,
    payload: Dict[str, Any],
) -> bool:
    r = _redis_connect()
    current_key = _state_key(worker, "current_task")
    liveness_key = worker_task_liveness_key(task_id)
    try:
        for attempt in range(5):
            with r.pipeline() as pipe:
                try:
                    pipe.watch(current_key, liveness_key)
                    current = _json_dict(pipe.get(current_key))
                    if (
                        not current
                        or str(current.get("task_id") or "") != task_id
                        or current.get("started_at") != binding_nonce
                    ):
                        pipe.unwatch()
                        return False
                    existing = _json_dict(pipe.get(liveness_key))
                    if existing and str(existing.get("task_id") or "") == task_id:
                        existing_nonce = existing.get("dispatch_started_at")
                        try:
                            existing_nonce_value = float(existing_nonce)
                        except (TypeError, ValueError):
                            existing_nonce_value = None
                        if existing_nonce_value is not None and (
                            existing_nonce_value > binding_nonce
                            or (existing_nonce_value == binding_nonce
                                and str(existing.get("worker") or "") != worker)
                        ):
                            pipe.unwatch()
                            return False
                    pipe.multi()
                    pipe.set(liveness_key, json.dumps(payload, separators=(",", ":")))
                    pipe.execute()
                    return True
                except WatchError:
                    if attempt == 4:
                        return False
                    time.sleep(0.01 * (attempt + 1))
    except RedisError as exc:
        LOG.warning(
            "worker task liveness exact Redis bind failed task=%s worker=%s: %s",
            task_id,
            worker,
            exc,
        )
    return False


def _last_outcome_terminal_for_current_task(r, worker: str, task_id: str) -> bool:
    current = _current_task_payload(r, worker)
    if not current or str(current.get("task_id") or "") != task_id:
        return False
    outcome = _json_dict(r.get(_state_key(worker, "last_outcome"))) or {}
    return str(outcome.get("outcome") or "").strip().lower() in {"done", "error", "interrupted"}


def _mark_liveness_heartbeat(
    task_id: str,
    worker: str,
    binding_nonce: float,
    now: float,
    ttl_secs: int,
    *,
    require_binding: bool,
    config: Optional[OrchConfig] = None,
) -> bool:
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        record = session.run(
            """
            MATCH (t:OrchTask {id: $task_id})
            WHERE t.status = 'in_progress'
              AND t.worker_liveness_worker = $worker
              AND t.worker_liveness_started_at = $binding_nonce
              AND ($require_binding = false
                   OR (t.dispatch_claim_worker = $worker
                       AND t.dispatch_claim_started_at = $binding_nonce))
            SET t.worker_liveness_heartbeat_at = $now,
                t.worker_liveness_ack_at = coalesce(t.worker_liveness_ack_at, $now),
                t.updated_at = datetime()
            RETURN t.id AS task_id
            """,
            task_id=task_id,
            worker=worker,
            binding_nonce=binding_nonce,
            require_binding=require_binding,
            now=float(now),
        ).single()
    if record is None:
        return False
    payload = {
        "task_id": task_id,
        "worker": worker,
        "dispatch_started_at": binding_nonce,
        "heartbeat_at": now,
        "heartbeat_ttl_secs": ttl_secs,
        "ack_at": now,
        "claim_bound": require_binding,
    }
    if require_binding:
        return _set_liveness_sidecar_if_current_binding(
            worker,
            task_id,
            binding_nonce,
            payload,
        )
    _redis_connect().set(worker_task_liveness_key(task_id), json.dumps(payload, separators=(",", ":")))
    return True


def _clear_matching_current_task(r, worker: str, task_id: str) -> None:
    clear_matching_current_task(
        worker,
        task_id,
        redis_client=r,
        reason="worker-liveness-return-to-pending",
    )


def _clear_exact_liveness_state(
    r,
    worker: str,
    task_id: str,
    binding_nonce: float,
) -> bool:
    current_key = _state_key(worker, "current_task")
    liveness_key = worker_task_liveness_key(task_id)
    try:
        for attempt in range(5):
            with r.pipeline() as pipe:
                try:
                    pipe.watch(current_key, liveness_key)
                    current = _json_dict(pipe.get(current_key))
                    liveness = _json_dict(pipe.get(liveness_key))
                    current_is_exact = bool(
                        current
                        and str(current.get("task_id") or "") == task_id
                        and current.get("started_at") == binding_nonce
                    )
                    liveness_is_exact = bool(
                        liveness
                        and str(liveness.get("task_id") or "") == task_id
                        and str(liveness.get("worker") or "") == worker
                        and liveness.get("dispatch_started_at") == binding_nonce
                    )
                    same_task_superseded = bool(
                        (
                            current
                            and str(current.get("task_id") or "") == task_id
                            and current.get("started_at") != binding_nonce
                        )
                        or (
                            liveness
                            and str(liveness.get("task_id") or "") == task_id
                            and not liveness_is_exact
                        )
                    )
                    if not current_is_exact and not liveness_is_exact:
                        pipe.unwatch()
                        return same_task_superseded
                    pipe.multi()
                    if current_is_exact:
                        pipe.delete(current_key)
                    if liveness_is_exact:
                        pipe.delete(liveness_key)
                    pipe.execute()
                    return same_task_superseded
                except WatchError:
                    if attempt == 4:
                        return True
                    time.sleep(0.01 * (attempt + 1))
    except RedisError as exc:
        LOG.warning(
            "worker task liveness exact Redis cleanup failed task=%s worker=%s: %s",
            task_id,
            worker,
            exc,
        )
    return True


def _task_out_of_band_workers(task: Dict[str, Any]) -> List[str]:
    return [
        str(worker)
        for worker in (
            task.get("worker"),
            task.get("owner"),
            task.get("dispatched_to"),
            task.get("supervisor"),
            task.get("project_supervisor"),
        )
        if worker
    ]


def _task_effect_class(task: Dict[str, Any], current: Optional[Dict[str, Any]]) -> str:
    for value in (
        task.get("effect_class"),
        (current or {}).get("effect_class"),
        (current or {}).get("action_effect_class"),
    ):
        effect_class = str(value or "").strip().lower()
        if effect_class:
            return effect_class
    return ""


def _live_outward_action_guarded(task: Dict[str, Any], worker: str, task_id: str, now: float,
                                 ttl_secs: int, *, config: Optional[OrchConfig] = None) -> bool:
    r = _redis_connect()
    current = _current_task_payload(r, worker)
    if not current or str(current.get("task_id") or "") != task_id:
        return False
    if _task_effect_class(task, current) not in OUTWARD_EFFECT_CLASSES:
        return False
    try:
        return active_inflight_signal(
            task_id,
            workers=[worker],
            oob_workers=_task_out_of_band_workers(task),
            now=now,
            config=config,
            heartbeat_ttl_secs=ttl_secs,
            heartbeat_mode="current_task",
            raise_on_probe_error=True,
        ) is not None
    except InFlightProbeError:
        return True


def _escalate_task(task: Dict[str, Any], now: float,
                   *, config: Optional[OrchConfig] = None) -> Optional[Dict[str, Any]]:
    task_id = str(task.get("task_id") or "")
    worker = str(task.get("worker") or "")
    if not task_id or not worker:
        return None
    cfg = config or OrchConfig()
    ttl_secs = max(1, int(task.get("ttl_secs") or worker_task_liveness_ttl_secs()))
    binding_nonce = task.get("binding_nonce")
    if binding_nonce is None:
        return None
    binding_nonce = float(binding_nonce)
    require_binding = bool(task.get("claim_bound"))
    if _live_outward_action_guarded(task, worker, task_id, now, ttl_secs, config=cfg):
        return None
    if task_actively_in_flight(
        task_id,
        workers=_task_out_of_band_workers(task),
        now=now,
        config=cfg,
        heartbeat_mode="none",
    ):
        return None
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        record = session.run(
            """
            MATCH (t:OrchTask {id: $task_id})
            WHERE t.status = 'in_progress'
              AND t.worker_liveness_worker = $worker
              AND t.worker_liveness_started_at = $binding_nonce
              AND ($require_binding = false
                   OR (t.dispatch_claim_worker = $worker
                       AND t.dispatch_claim_started_at = $binding_nonce))
            SET t.status = 'pending',
                t.dispatched_to = NULL,
                t.dispatch_claim_worker = NULL,
                t.dispatch_claim_started_at = NULL,
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
            binding_nonce=binding_nonce,
            require_binding=require_binding,
            now=float(now),
        ).single()
    if record is None:
        return None
    r = _redis_connect()
    if require_binding:
        if _clear_exact_liveness_state(r, worker, task_id, binding_nonce):
            return None
    else:
        r.delete(worker_task_liveness_key(task_id))
        _clear_matching_current_task(r, worker, task_id)
    result = dict(record)
    result["worker"] = worker
    result["stale_for_sec"] = int(max(0.0, now - float(task.get("heartbeat_at") or now)))
    result["expired_blocked_on"] = str(task.get("blocked_on") or "").strip()
    return result


def _task_is_declared_wait(task: Dict[str, Any], *, config: Optional[OrchConfig] = None) -> bool:
    from .orch_schema import is_awaiting_human_review_gate, is_declared_await_signal

    if is_declared_await_signal(task.get("blocked_on")):
        return True
    return is_awaiting_human_review_gate(task, config=config)


def _registered_in_progress_tasks(
    *,
    config: Optional[OrchConfig] = None,
    task_id_prefix: Optional[str] = None,
    project_id_prefix: Optional[str] = None,
) -> List[Dict[str, Any]]:
    cfg = config or OrchConfig()
    task_prefix = str(task_id_prefix or "")
    project_prefix = str(project_id_prefix or "")
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        return [dict(record) for record in session.run(
            """
            MATCH (p:OrchProject)-[:HAS_PHASE]->(:OrchPhase)-[:HAS_TASK]->(t:OrchTask)
            WHERE t.status = 'in_progress'
              AND coalesce(t.worker_liveness_worker, '') <> ''
              AND coalesce(t.worker_liveness_started_at, 0.0) > 0.0
              AND ((t.dispatch_claim_worker IS NULL AND t.dispatch_claim_started_at IS NULL)
                   OR (t.dispatch_claim_worker = t.worker_liveness_worker
                       AND t.dispatch_claim_started_at = t.worker_liveness_started_at))
              AND ($task_prefix = '' OR t.id STARTS WITH $task_prefix)
              AND ($project_prefix = '' OR p.id STARTS WITH $project_prefix)
            RETURN t.id AS task_id,
                   t.description AS description,
                   t.owner AS owner,
                   t.blocked_on AS blocked_on,
                   t.task_type AS task_type,
                   t.effect_class AS effect_class,
                   t.dispatched_to AS dispatched_to,
                   t.worker_liveness_worker AS worker,
                   t.worker_liveness_supervisor AS supervisor,
                   t.worker_liveness_started_at AS binding_nonce,
                   (t.dispatch_claim_started_at IS NOT NULL) AS claim_bound,
                   coalesce(t.worker_liveness_heartbeat_at, t.worker_liveness_started_at) AS heartbeat_at,
                   coalesce(t.worker_liveness_ttl_secs, $default_ttl) AS ttl_secs,
                   p.id AS project_id,
                   p.supervisor AS project_supervisor
            """,
            default_ttl=DEFAULT_WORKER_TASK_LIVENESS_TTL_SECS,
            task_prefix=task_prefix,
            project_prefix=project_prefix,
        )]


def escalate_stale_worker_tasks(now: Optional[float] = None,
                                *,
                                config: Optional[OrchConfig] = None,
                                task_id_prefix: Optional[str] = None,
                                project_id_prefix: Optional[str] = None) -> List[Dict[str, Any]]:
    if not worker_task_liveness_enabled():
        return []
    cfg = config or OrchConfig()
    r = _redis_connect()
    current_time = time.time() if now is None else float(now)
    escalated: List[Dict[str, Any]] = []
    for task in _registered_in_progress_tasks(
        config=cfg,
        task_id_prefix=task_id_prefix,
        project_id_prefix=project_id_prefix,
    ):
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
        signal = active_inflight_signal(
            task_id,
            workers=[worker],
            oob_workers=_task_out_of_band_workers(task),
            now=current_time,
            config=cfg,
            heartbeat_ttl_secs=ttl_secs,
            heartbeat_mode="current_task",
        )
        if signal and signal.source in {"tool_heartbeat", "tool_running"}:
            _mark_liveness_heartbeat(
                task_id,
                worker,
                float(task["binding_nonce"]),
                current_time,
                ttl_secs,
                require_binding=bool(task.get("claim_bound")),
                config=cfg,
            )
            continue
        if signal:
            continue
        if heartbeat_at <= 0 or current_time - heartbeat_at < ttl_secs:
            continue
        result = _escalate_task(task, current_time, config=cfg)
        if result:
            escalated.append(result)
    return escalated
