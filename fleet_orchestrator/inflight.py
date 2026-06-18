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


@dataclass(frozen=True)
class InFlightSignal:
    source: str
    worker: Optional[str] = None


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
        except Exception:
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
        except Exception:
            pass
        try:
            raw = r.get(state_key(worker, "last_tool_activity"))
        except Exception:
            continue
        if raw is None:
            continue
        try:
            if current_time - float(raw) < ttl:
                return InFlightSignal(source="tool_heartbeat", worker=worker)
        except (TypeError, ValueError):
            continue
    return None


def task_actively_in_flight(task_id: Optional[str], **kwargs: Any) -> bool:
    return active_inflight_signal(task_id, **kwargs) is not None
