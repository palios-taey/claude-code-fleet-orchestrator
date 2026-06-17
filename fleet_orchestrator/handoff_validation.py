from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from typing import Any, Optional


DEFAULT_PREFIX = os.environ.get("NOTIFY_KEY_PREFIX", "taey")
_EXECUTOR = ThreadPoolExecutor(max_workers=2)
_BACKFILL_LOCK = threading.Lock()
_BACKFILLED_PREFIXES: set[str] = set()


def handoff_key(prefix: str, dispatcher: str, msg_id: str) -> str:
    return f"{prefix}:handoff:{dispatcher}:{msg_id}"


def handoff_index_key(prefix: str, dispatcher: str) -> str:
    return f"{prefix}:handoff-index:{dispatcher}"


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


def _text(value: Any) -> str:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return str(value)


def _member_key(prefix: str, dispatcher: str, member: Any) -> tuple[str, str]:
    value = _text(member)
    if f"{prefix}:handoff:" in value:
        return value, value.rsplit(":", 1)[-1]
    return handoff_key(prefix, dispatcher, value), value


def _record_msg_id(record: dict[str, Any]) -> str:
    return str(record.get("msg_id") or "").strip()


def _index_record(redis_client, record: dict[str, Any], *, prefix: str) -> None:
    dispatcher = str(record.get("dispatcher_session_id") or "").strip()
    msg_id = _record_msg_id(record)
    if not dispatcher or not msg_id:
        return
    index_key = handoff_index_key(prefix, dispatcher)
    redis_client.sadd(index_key, msg_id)
    key = record.get("_key")
    if not key:
        return
    try:
        record_ttl_ms = int(redis_client.pttl(key))
        index_ttl_ms = int(redis_client.pttl(index_key))
    except Exception:
        return
    if record_ttl_ms > 0 and (index_ttl_ms < 0 or index_ttl_ms < record_ttl_ms):
        try:
            redis_client.pexpire(index_key, record_ttl_ms)
        except Exception:
            return


def write_handoff_record(redis_client, record: dict[str, Any], *, prefix: str = DEFAULT_PREFIX) -> None:
    redis_client.set(record["_key"], json.dumps(record, separators=(",", ":")))
    if record.get("kind") == "explicit_handoff":
        _index_record(redis_client, record, prefix=prefix)


def backfill_handoff_index(redis_client, *, prefix: str = DEFAULT_PREFIX) -> int:
    """One-time migration for records created before the per-dispatcher index existed."""
    count = 0
    pattern = f"{prefix}:handoff:*"
    for key in redis_client.scan_iter(match=pattern):
        key_text = _text(key)
        record = _json_dict(redis_client.get(key))
        if not record or record.get("kind") != "explicit_handoff":
            continue
        dispatcher = str(record.get("dispatcher_session_id") or "").strip()
        msg_id = _record_msg_id(record)
        if not dispatcher or not msg_id:
            continue
        record["_key"] = key_text
        _index_record(redis_client, record, prefix=prefix)
        count += 1
    _BACKFILLED_PREFIXES.add(prefix)
    return count


def ensure_handoff_index_backfilled(redis_client, *, prefix: str = DEFAULT_PREFIX) -> int:
    if prefix in _BACKFILLED_PREFIXES:
        return 0
    with _BACKFILL_LOCK:
        if prefix in _BACKFILLED_PREFIXES:
            return 0
        return backfill_handoff_index(redis_client, prefix=prefix)


def _scan_dispatcher_handoffs(redis_client, dispatcher_session_id: str,
                              prefix: str) -> list[dict[str, Any]]:
    index_key = handoff_index_key(prefix, dispatcher_session_id)
    members = list(redis_client.smembers(index_key) or [])
    if not members and prefix not in _BACKFILLED_PREFIXES:
        ensure_handoff_index_backfilled(redis_client, prefix=prefix)
        members = list(redis_client.smembers(index_key) or [])
    key_pairs = [_member_key(prefix, dispatcher_session_id, member) for member in members]
    keys = [key for key, _msg_id in key_pairs]
    payloads = redis_client.mget(keys) if keys else []
    records: list[dict[str, Any]] = []
    stale_members: list[str] = []
    for (key, msg_id), raw in zip(key_pairs, payloads):
        record = _json_dict(raw)
        if not record:
            stale_members.append(msg_id)
            continue
        if record.get("kind") != "explicit_handoff":
            stale_members.append(msg_id)
            continue
        if record.get("dispatcher_session_id") != dispatcher_session_id:
            stale_members.append(msg_id)
            continue
        record["_key"] = key
        records.append(record)
    if stale_members:
        redis_client.srem(index_key, *stale_members)
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
        write_handoff_record(redis_client, record, prefix=prefix)


def _timeout_call(fn, timeout_s: float):
    # Timed-out futures are not cancelled here. The thread may continue to
    # completion in the background, but the caller fail-opens immediately to
    # keep the stop path non-blocking.
    future = _EXECUTOR.submit(fn)
    try:
        return future.result(timeout=timeout_s)
    except FuturesTimeoutError as exc:
        raise TimeoutError("handoff validation timed out") from exc


def _write_record(redis_client, record: dict[str, Any], *, prefix: str) -> None:
    write_handoff_record(redis_client, record, prefix=prefix)


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
        _write_record(redis_client, record, prefix=prefix)
        return {"state": "receipt_acked", "record": record}

    state = str(record.get("state") or "")
    if state == "dead":
        return {"state": "dead", "record": record}

    delivery_state = str(record.get("delivery_state") or "queued")
    if delivery_state == "not_deliverable":
        record["state"] = "delivery_failed"
        _write_record(redis_client, record, prefix=prefix)
        return {"state": "delivery_failed", "record": record}

    poll_budget = int(record.get("pickup_poll_budget", _default_pickup_poll_budget()) or _default_pickup_poll_budget())
    delivery_poll_count = int(record.get("delivery_poll_count", 0) or 0)
    if delivery_state == "injected_waiting_ack" and delivery_poll_count >= poll_budget:
        record["state"] = "delivery_failed"
        record.setdefault("delivery_failure_reason", "poll_budget_exhausted")
        _write_record(redis_client, record, prefix=prefix)
        return {"state": "delivery_failed", "record": record}

    backstop = float(record.get("ack_backstop_at", record.get("ack_deadline_at", 0)) or 0)
    if backstop and backstop < now:
        record["state"] = "delivery_failed"
        record.setdefault("delivery_failure_reason", "backstop_expired")
        _write_record(redis_client, record, prefix=prefix)
        return {"state": "delivery_failed", "record": record}

    record["state"] = "pending_unacked"
    _write_record(redis_client, record, prefix=prefix)
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
            write_handoff_record(redis_client, record, prefix=prefix)
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
        _write_record(redis_client, record, prefix=prefix)
        events.append(event)
    return events
