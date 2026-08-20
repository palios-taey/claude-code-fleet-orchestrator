from __future__ import annotations

import json
import re
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


def active_turn_valid_for_task(r: Any, worker: str, task_id: str, current_time: float) -> bool:
    """Shared read-only validator for durable active-turn lease + binding.

    Requires for at least one future ZSET member:
    - current_task.task_id == queried task_id (the OrchTask binding)
    - ctx.turn_id == ZSET member (tid)
    - ctx.seat_id == worker
    - ctx.event_id and ctx.correlation_id nonempty
    - ctx.tool_profile in production allowlist ("full", "manual-chat-ui")
    - ctx.process_generation exactly 32 [0-9a-f] (full match, not islower)
    - ctx.started_at is finite and 0 < started_at <= current_time
    - ZSET score (lease) > current_time

    Rejects any malformed/mismatched field independently.
    """
    if not task_id:
        return False
    try:
        # 1. current_task binding
        cur_raw = r.get(state_key(worker, "current_task"))
        cur = {}
        if cur_raw:
            try:
                cur = json.loads(cur_raw.decode(errors="replace") if isinstance(cur_raw, (bytes, bytearray)) else cur_raw)
            except Exception:
                cur = {}
        if str(cur.get("task_id") or "") != task_id:
            return False

        # 2. future lease members
        turn_key = state_key(worker, "active_turns")
        members = r.zrangebyscore(turn_key, current_time, "+inf")
        if not members:
            return False

        ctx_key = state_key(worker, "turn_context")
        allowed_profiles = {"full", "manual-chat-ui"}

        for m in members:
            tid_str = m.decode(errors="replace") if isinstance(m, (bytes, bytearray)) else str(m)

            raw_ctx = r.hget(ctx_key, tid_str)
            if not raw_ctx:
                continue
            try:
                ctx = json.loads(raw_ctx.decode(errors="replace") if isinstance(raw_ctx, (bytes, bytearray)) else raw_ctx)
            except Exception:
                continue
            if not isinstance(ctx, dict):
                continue

            # turn_id must match the ZSET member exactly
            if str(ctx.get("turn_id") or "") != tid_str:
                continue
            # seat_id must match worker
            if str(ctx.get("seat_id") or "") != worker:
                continue

            # event_id and correlation_id nonempty
            if not str(ctx.get("event_id") or "").strip():
                continue
            if not str(ctx.get("correlation_id") or "").strip():
                continue

            # tool_profile in allowlist
            profile = str(ctx.get("tool_profile") or "")
            if profile not in allowed_profiles:
                continue

            # process_generation exactly 32 lowercase hex (full match)
            gen = str(ctx.get("process_generation") or "")
            if not re.fullmatch(r"[0-9a-f]{32}", gen):
                continue

            # started_at sane: finite, >0 and <= now
            started = ctx.get("started_at")
            try:
                started_f = float(started)
            except (TypeError, ValueError):
                continue
            if not (0 < started_f <= current_time):
                continue

            # If we reach here, this member has a fully valid ctx + we already have current_task match + future score
            return True

        return False
    except Exception:
        return False


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
        # Use shared validator (requires ctx + task match + future score)
        if active_turn_valid_for_task(r, worker, task_id, current_time):
            return InFlightSignal(source="active_turn", worker=worker)

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
