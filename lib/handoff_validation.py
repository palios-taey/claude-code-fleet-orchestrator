from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any, Optional


DEFAULT_PREFIX = os.environ.get("NOTIFY_KEY_PREFIX", "taey")
_EXECUTOR = ThreadPoolExecutor(max_workers=2)
_FLAG_CACHE_TTL_S = 2.0
_FLAG_CACHE_PATH: Optional[str] = None
_FLAG_CACHE_AT = 0.0
_FLAG_CACHE_DATA: dict[str, dict[str, bool]] = {}
_FLAG_CACHE_LOCK = threading.Lock()


def _truthy(raw: Any) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def _session_items(raw: str) -> set[str]:
    return {item.strip() for item in raw.replace(";", ",").split(",") if item.strip()}


def _flag_file_map() -> dict[str, dict[str, bool]]:
    path = os.environ.get("CF_HANDOFF_SESSION_FLAGS_FILE", "").strip()
    if not path:
        return {}
    ttl_raw = os.environ.get("CF_HANDOFF_SESSION_FLAGS_TTL_SECS", str(_FLAG_CACHE_TTL_S)).strip()
    try:
        ttl_s = max(0.0, float(ttl_raw))
    except Exception:
        ttl_s = _FLAG_CACHE_TTL_S
    global _FLAG_CACHE_PATH, _FLAG_CACHE_AT, _FLAG_CACHE_DATA
    now = time.time()
    with _FLAG_CACHE_LOCK:
        if path == _FLAG_CACHE_PATH and (now - _FLAG_CACHE_AT) <= ttl_s:
            return dict(_FLAG_CACHE_DATA)
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    result: dict[str, dict[str, bool]] = {}
    for session_id, flags in payload.items():
        if not isinstance(flags, dict):
            continue
        result[str(session_id)] = {
            "enforce": _truthy(flags.get("enforce")),
            "ack_passive": _truthy(flags.get("ack_passive")),
        }
    with _FLAG_CACHE_LOCK:
        _FLAG_CACHE_PATH = path
        _FLAG_CACHE_AT = now
        _FLAG_CACHE_DATA = dict(result)
    return result


def flags_for_session(session_id: str) -> dict[str, bool]:
    file_map = _flag_file_map()
    file_flags = file_map.get(session_id, {})
    enforce_requested = _truthy(os.environ.get("CF_HANDOFF_ENFORCE"))
    ack_requested = _truthy(os.environ.get("CF_HANDOFF_ACK_PASSIVE"))
    enforce_sessions = _session_items(os.environ.get("CF_HANDOFF_ENFORCE_SESSIONS", ""))
    ack_sessions = _session_items(os.environ.get("CF_HANDOFF_ACK_PASSIVE_SESSIONS", ""))
    enforce = bool(file_flags.get("enforce")) or (enforce_requested and session_id in enforce_sessions)
    ack_passive = bool(file_flags.get("ack_passive")) or (ack_requested and session_id in ack_sessions)
    return {"enforce": enforce, "ack_passive": ack_passive}


def handoff_key(prefix: str, dispatcher: str, msg_id: str) -> str:
    return f"{prefix}:handoff:{dispatcher}:{msg_id}"


def ack_key(prefix: str, dispatcher: str, target: str, msg_id: str) -> str:
    return f"{prefix}:handoff-ack:{dispatcher}:{target}:{msg_id}"


def _default_pickup_poll_budget() -> int:
    raw = os.environ.get("CF_HANDOFF_PICKUP_POLL_BUDGET", "5").strip()
    try:
        return max(1, int(raw))
    except Exception:
        return 5


def _json_dict(raw: Any) -> Optional[dict[str, Any]]:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except Exception:
        return None
    return parsed if isinstance(parsed, dict) else None


def _scan_dispatcher_handoffs(redis_client, dispatcher_session_id: str,
                              prefix: str) -> list[dict[str, Any]]:
    pattern = f"{prefix}:handoff:{dispatcher_session_id}:*"
    records: list[dict[str, Any]] = []
    for key in redis_client.scan_iter(match=pattern):
        record = _json_dict(redis_client.get(key))
        if not record:
            continue
        if record.get("kind") != "explicit_handoff":
            continue
        if record.get("dispatcher_session_id") != dispatcher_session_id:
            continue
        record["_key"] = key
        records.append(record)
    records.sort(key=lambda item: float(item.get("created_at", 0) or 0), reverse=True)
    return records


def mark_superseded_for_task(redis_client, dispatcher_session_id: str,
                             dispatcher_task_id: str, prefix: str = DEFAULT_PREFIX) -> None:
    if not dispatcher_task_id:
        return
    for record in _scan_dispatcher_handoffs(redis_client, dispatcher_session_id, prefix):
        if str(record.get("dispatcher_task_id") or "") != dispatcher_task_id:
            continue
        if record.get("state") in {"resolved", "dead", "superseded"}:
            continue
        record["state"] = "superseded"
        redis_client.set(record["_key"], json.dumps(record, separators=(",", ":")))


def _timeout_call(fn, timeout_s: float):
    # Timed-out futures are not cancelled here. The thread may continue to
    # completion in the background, but the caller fail-opens immediately to
    # keep the stop path non-blocking.
    future = _EXECUTOR.submit(fn)
    try:
        return future.result(timeout=timeout_s)
    except FuturesTimeoutError as exc:
        raise TimeoutError("handoff validation timed out") from exc


def _write_record(redis_client, record: dict[str, Any]) -> None:
    redis_client.set(record["_key"], json.dumps(record, separators=(",", ":")))


def _handoff_resolution(
    redis_client,
    dispatcher_session_id: str,
    record: dict[str, Any],
    *,
    prefix: str,
    now: Optional[float] = None,
) -> dict[str, Any]:
    now = now if now is not None else time.time()
    target = str(record.get("target_session_id") or "")
    msg_id = str(record.get("msg_id") or "")
    ack = _json_dict(redis_client.get(ack_key(prefix, dispatcher_session_id, target, msg_id)))
    if ack and ack.get("ack_by") == target and ack.get("message_hash") == record.get("message_hash"):
        record["state"] = "receipt_acked"
        _write_record(redis_client, record)
        return {"state": "receipt_acked", "record": record}

    state = str(record.get("state") or "")
    if state == "dead":
        return {"state": "dead", "record": record}

    delivery_state = str(record.get("delivery_state") or "queued")
    if delivery_state == "not_deliverable":
        record["state"] = "delivery_failed"
        _write_record(redis_client, record)
        return {"state": "delivery_failed", "record": record}

    poll_budget = int(record.get("pickup_poll_budget", _default_pickup_poll_budget()) or _default_pickup_poll_budget())
    delivery_poll_count = int(record.get("delivery_poll_count", 0) or 0)
    if delivery_state == "injected_waiting_ack" and delivery_poll_count >= poll_budget:
        record["state"] = "delivery_failed"
        record.setdefault("delivery_failure_reason", "poll_budget_exhausted")
        _write_record(redis_client, record)
        return {"state": "delivery_failed", "record": record}

    backstop = float(record.get("ack_backstop_at", record.get("ack_deadline_at", 0)) or 0)
    if backstop and backstop < now:
        record["state"] = "delivery_failed"
        record.setdefault("delivery_failure_reason", "backstop_expired")
        _write_record(redis_client, record)
        return {"state": "delivery_failed", "record": record}

    record["state"] = "pending_unacked"
    _write_record(redis_client, record)
    return {"state": "pending_unacked", "record": record}


def validate_stop_handoff(
    redis_client,
    dispatcher_session_id: str,
    dispatcher_task_id: Optional[str],
    *,
    prefix: str = DEFAULT_PREFIX,
    timeout_s: float = 0.2,
) -> dict[str, Any]:
    def _read() -> dict[str, Any]:
        if not dispatcher_task_id:
            return {"state": "no_handoff", "record": None}
        for record in _scan_dispatcher_handoffs(redis_client, dispatcher_session_id, prefix):
            if str(record.get("dispatcher_task_id") or "") != dispatcher_task_id:
                continue
            state = str(record.get("state") or "pending_unacked")
            if state in {"resolved", "superseded"}:
                continue
            return _handoff_resolution(
                redis_client,
                dispatcher_session_id,
                record,
                prefix=prefix,
            )
        return {"state": "no_handoff", "record": None}

    return _timeout_call(_read, timeout_s)


def actionability_for_nudge(task: Optional[dict[str, Any]], session_id: str) -> tuple[bool, str]:
    if not task:
        return False, "unknown_task"
    status = str(task.get("status") or "pending")
    if status not in {"pending", "resumed"}:
        return False, f"status={status}"
    owner_lane = str(task.get("owner_lane") or task.get("owner") or "")
    if owner_lane != session_id:
        return False, "wrong_lane"
    if str(task.get("execution_mode") or "execute") != "execute":
        return False, f"execution_mode={task.get('execution_mode') or 'execute'}"
    if str(task.get("effect_class") or "") == "outward_irreversible":
        return False, "outward_irreversible"
    deps_status = str(task.get("deps_status") or "met")
    if deps_status != "met":
        return False, f"deps_status={deps_status}"
    required_inputs = task.get("required_inputs") or []
    if not isinstance(required_inputs, list):
        return False, "required_inputs_invalid"
    for item in required_inputs:
        if not isinstance(item, dict):
            return False, "required_inputs_invalid"
        path = str(item.get("path") or "")
        if not path or not os.path.exists(path) or os.path.getsize(path) <= 0:
            return False, "required_inputs_missing"
    return True, "execute"


def process_expired_handoffs(
    redis_client,
    *,
    session_id: str,
    load_task,
    send_wake,
    prefix: str = DEFAULT_PREFIX,
    base_backoff_s: int = 60,
    max_attempts: int = 3,
) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    flags = flags_for_session(session_id)
    if not flags["enforce"]:
        return events
    now = time.time()
    backoff_exponent_cap = 10
    for record in _scan_dispatcher_handoffs(redis_client, session_id, prefix):
        state = str(record.get("state") or "")
        if state in {"resolved", "superseded", "dead", "receipt_acked"}:
            continue
        resolution = _handoff_resolution(
            redis_client,
            session_id,
            record,
            prefix=prefix,
            now=now,
        )
        state = str(resolution.get("state") or "")
        record = resolution.get("record") or record
        if state in {"resolved", "superseded", "dead", "receipt_acked", "no_handoff"}:
            continue
        if state == "pending_unacked":
            continue
        next_wake_after = float(record.get("next_wake_after", 0) or 0)
        if next_wake_after and next_wake_after > now:
            continue
        wake_count = int(record.get("wake_count", 0) or 0)
        if wake_count >= max_attempts:
            record["state"] = "dead"
            record["dead_at"] = now
            redis_client.set(record["_key"], json.dumps(record, separators=(",", ":")))
            events.append({"type": "handoff_dead", "dispatcher_task_id": record.get("dispatcher_task_id")})
            continue
        task = load_task(str(record.get("dispatcher_task_id") or "")) if record.get("dispatcher_task_id") else None
        actionable, reason = actionability_for_nudge(task, session_id)
        advisory_type = "handoff_wake" if actionable else "handoff_triage"
        event = {
            "type": advisory_type,
            "dispatcher_task_id": record.get("dispatcher_task_id"),
            "msg": "re-dispatch or choose another",
            "actionability": reason,
            "delivery_failure_reason": record.get("delivery_failure_reason"),
            "last_delivery_signal": record.get("last_delivery_signal"),
            "delivery_signal_source": record.get("delivery_signal_source"),
        }
        wake_delivery_ok = True
        wake_delivery_error = None
        try:
            send_result = send_wake(event)
            if send_result is False:
                wake_delivery_ok = False
                wake_delivery_error = "send_wake_returned_false"
        except Exception as exc:
            wake_delivery_ok = False
            wake_delivery_error = exc.__class__.__name__
        wake_count += 1
        record["state"] = "redispatch_requested"
        record["last_wake_at"] = now
        record["wake_count"] = wake_count
        if wake_delivery_error:
            record["last_wake_error"] = wake_delivery_error
            event["wake_delivery_ok"] = False
            event["wake_delivery_error"] = wake_delivery_error
        else:
            record.pop("last_wake_error", None)
        record["next_wake_after"] = now + (base_backoff_s * (2 ** min(max(wake_count - 1, 0), backoff_exponent_cap)))
        _write_record(redis_client, record)
        events.append(event)
    return events
