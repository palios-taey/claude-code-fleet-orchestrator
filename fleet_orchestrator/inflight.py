from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from .config import OrchConfig
from .notify_state import redis_connect as notify_redis_connect
from .notify_state import state_key
from .out_of_band import out_of_band_task_active


PEER_HEARTBEAT_STALE_SEC = 300
_TRUTHY_VALUES = {"1", "true", "yes", "on", "running"}


@dataclass(frozen=True)
class InFlightSignal:
    source: str
    worker: Optional[str] = None


class InFlightProbeError(RuntimeError):
    pass


def _json_dict(raw: Any) -> Optional[dict[str, Any]]:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _current_task_id(r: Any, worker: str) -> Optional[str]:
    current = _json_dict(r.get(state_key(worker, "current_task")))
    if not current:
        return None
    task_id = current.get("task_id")
    return str(task_id) if task_id else None


def _truthy(raw: Any) -> bool:
    if raw is None:
        return False
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    return str(raw).strip().lower() in _TRUTHY_VALUES


def _float_or_none(raw: Any) -> Optional[float]:
    if raw is None:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _fresh_timestamp(raw: Any, current_time: float, ttl: int) -> bool:
    stamp = _float_or_none(raw)
    return stamp is not None and 0 <= current_time - stamp < ttl


def _tool_running_signal_fresh(r: Any, worker: str, current_time: float, ttl: int) -> bool:
    if not _truthy(r.get(state_key(worker, "tool_running"))):
        return False
    return (
        _fresh_timestamp(r.get(state_key(worker, "tool_running_at")), current_time, ttl)
        or _fresh_timestamp(r.get(state_key(worker, "last_activity")), current_time, ttl)
    )


def _terminal_outcome_for_task(r: Any, worker: str, task_id: str, *, details_required: bool) -> bool:
    outcome = _json_dict(r.get(state_key(worker, "last_outcome"))) or {}
    if str(outcome.get("outcome") or "").strip().lower() not in {"done", "error", "interrupted"}:
        return False
    if not details_required:
        return True
    return task_id in str(outcome.get("details") or "")


def active_inflight_signal(
    task_id: Optional[str],
    *,
    workers: Optional[Iterable[str]] = None,
    oob_workers: Optional[Iterable[str]] = None,
    now: Optional[float] = None,
    config: Optional[OrchConfig] = None,
    heartbeat_ttl_secs: Optional[int] = None,
    heartbeat_mode: str = "peer",
    raise_on_probe_error: bool = False,
) -> Optional[InFlightSignal]:
    if not task_id:
        return None
    cfg = config or OrchConfig()
    worker_list = [str(worker) for worker in (workers or []) if worker]
    oob_worker_list = [str(worker) for worker in (oob_workers or worker_list) if worker]
    current_time = time.time() if now is None else float(now)
    if out_of_band_task_active(
        task_id,
        workers=oob_worker_list if (workers is not None or oob_workers is not None) else None,
        now=current_time,
        config=cfg,
    ):
        return InFlightSignal(source="out_of_band")
    if heartbeat_mode == "none" or not worker_list:
        return None

    ttl = max(1, int(heartbeat_ttl_secs or PEER_HEARTBEAT_STALE_SEC))
    r = notify_redis_connect()
    for worker in worker_list:
        try:
            current_task_id = _current_task_id(r, worker)
        except Exception as exc:
            if raise_on_probe_error:
                raise InFlightProbeError(f"current_task probe failed for {worker}") from exc
            current_task_id = None
        if heartbeat_mode == "current_task":
            if current_task_id != task_id:
                continue
            details_required = False
        else:
            if current_task_id and current_task_id != task_id:
                continue
            details_required = True
        try:
            if _terminal_outcome_for_task(r, worker, task_id, details_required=details_required):
                continue
        except Exception as exc:
            if raise_on_probe_error:
                raise InFlightProbeError(f"terminal outcome probe failed for {worker}") from exc
            pass
        # Durable active-turn signal from taey-presence (ZSET with expiry lease).
        # If worker has non-expired active turn for its bound task, treat as in-flight.
        # This prevents stale/awaiting during long blocking proxy turns.
        try:
            turn_key = state_key(worker, "active_turns")
            if r.zrangebyscore(turn_key, current_time, "+inf", start=0, num=1):
                return InFlightSignal(source="active_turn", worker=worker)
        except Exception as exc:
            if raise_on_probe_error:
                raise InFlightProbeError(f"active_turn probe failed for {worker}") from exc
            pass
        try:
            raw_tool_activity = r.get(state_key(worker, "last_tool_activity"))
        except Exception as exc:
            if raise_on_probe_error:
                raise InFlightProbeError(f"tool heartbeat probe failed for {worker}") from exc
            continue
        if _fresh_timestamp(raw_tool_activity, current_time, ttl):
            return InFlightSignal(source="tool_heartbeat", worker=worker)
        try:
            if _tool_running_signal_fresh(r, worker, current_time, ttl):
                return InFlightSignal(source="tool_running", worker=worker)
        except Exception as exc:
            if raise_on_probe_error:
                raise InFlightProbeError(f"tool-running probe failed for {worker}") from exc
            continue
    return None


def task_actively_in_flight(task_id: Optional[str], **kwargs: Any) -> bool:
    return active_inflight_signal(task_id, **kwargs) is not None
