#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PREFIX = f"hvacc-{uuid.uuid4().hex[:8]}"
os.environ["NOTIFY_KEY_PREFIX"] = PREFIX
os.environ["CF_HANDOFF_ENFORCE"] = "1"
os.environ["CF_HANDOFF_ENFORCE_SESSIONS"] = "conductor-codex"
if "ORCH_DOTENV" not in os.environ:
    for candidate in (
        ROOT / ".env",
        Path.home() / "claude-code-fleet-orchestrator/.env",
    ):
        if candidate.is_file():
            os.environ["ORCH_DOTENV"] = str(candidate)
            break
os.environ.setdefault("ORCH_REDIS_HOST", "127.0.0.1")
os.environ.setdefault("ORCH_REDIS_PORT", "6379")

from lib.config import OrchConfig, get_redis_sync  # noqa: E402
from lib.handoff_validation import actionability_for_nudge, process_expired_handoffs, validate_stop_handoff  # noqa: E402

CFG = OrchConfig()


def _cleanup(prefix: str) -> None:
    r = get_redis_sync(CFG)
    cursor = 0
    while True:
        cursor, keys = r.scan(cursor=cursor, match=f"{prefix}:*", count=100)
        if keys:
            r.delete(*keys)
        if cursor == 0:
            break


def _record(
    dispatcher: str,
    target: str,
    task_id: str,
    *,
    deadline_delta: float = 300.0,
    delivery_state: str = "queued",
    delivery_poll_count: int = 0,
    delivery_failure_reason: str | None = None,
) -> dict:
    now = time.time()
    record = {
        "kind": "explicit_handoff",
        "dispatcher_session_id": dispatcher,
        "target_session_id": target,
        "dispatcher_task_id": task_id,
        "msg_id": str(uuid.uuid4()),
        "message_hash": "hash-1",
        "actionable_inputs": {},
        "created_at": now - 30,
        "ack_deadline_at": now + deadline_delta,
        "ack_backstop_at": now + deadline_delta,
        "pickup_poll_budget": 5,
        "delivery_state": delivery_state,
        "delivery_poll_count": delivery_poll_count,
    }
    if delivery_failure_reason:
        record["delivery_failure_reason"] = delivery_failure_reason
    return record


def _write_record(r, record: dict) -> str:
    key = f"{PREFIX}:handoff:{record['dispatcher_session_id']}:{record['msg_id']}"
    r.set(key, json.dumps(record, separators=(",", ":")))
    return key


def main() -> int:
    _cleanup(PREFIX)
    try:
        r = get_redis_sync(CFG)

        events: list[dict] = []
        no_wake = process_expired_handoffs(
            r,
            session_id="conductor-codex",
            load_task=lambda task_id: None,
            send_wake=lambda event: events.append(event),
            prefix=PREFIX,
        )
        print("PASS cross-project-no-wake" if no_wake == [] and events == [] else f"FAIL cross-project-no-wake events={events}")

        pending = _record("conductor-codex", "worker-codex", "task-pending", deadline_delta=120)
        _write_record(r, pending)
        validation = validate_stop_handoff(r, "conductor-codex", "task-pending", prefix=PREFIX, timeout_s=1.0)
        print("PASS pending-unacked-not-validated" if validation.get("state") == "pending_unacked" else f"FAIL pending-unacked-not-validated {validation}")

        inject_failed = _record(
            "conductor-codex",
            "worker-codex",
            "task-inject-failed",
            delivery_state="not_deliverable",
            delivery_failure_reason="inject_failed",
        )
        _write_record(r, inject_failed)
        inject_failed_events: list[dict] = []
        process_expired_handoffs(
            r,
            session_id="conductor-codex",
            load_task=lambda task_id: {
                "status": "pending",
                "owner_lane": "conductor-codex",
                "execution_mode": "execute",
                "effect_class": "internal",
                "deps_status": "met",
                "required_inputs": [],
            },
            send_wake=lambda event: inject_failed_events.append(event),
            prefix=PREFIX,
        )
        print(
            "PASS inject-failed-immediate-wake"
            if inject_failed_events and inject_failed_events[0]["delivery_failure_reason"] == "inject_failed"
            else f"FAIL inject-failed-immediate-wake {inject_failed_events}"
        )

        poll_budget = _record(
            "conductor-codex",
            "worker-codex",
            "task-poll-budget",
            delivery_state="injected_waiting_ack",
            delivery_poll_count=5,
        )
        _write_record(r, poll_budget)
        poll_budget_events: list[dict] = []
        process_expired_handoffs(
            r,
            session_id="conductor-codex",
            load_task=lambda task_id: {
                "status": "pending",
                "owner_lane": "conductor-codex",
                "execution_mode": "execute",
                "effect_class": "internal",
                "deps_status": "met",
                "required_inputs": [],
            },
            send_wake=lambda event: poll_budget_events.append(event),
            prefix=PREFIX,
        )
        print(
            "PASS poll-budget-short-window"
            if poll_budget_events and poll_budget_events[0]["delivery_failure_reason"] == "poll_budget_exhausted"
            else f"FAIL poll-budget-short-window {poll_budget_events}"
        )

        redispatch_backstop = _record(
            "conductor-codex",
            "worker-codex",
            "task-redispatch-backstop",
            deadline_delta=-10,
        )
        redispatch_backstop["state"] = "redispatch_requested"
        redispatch_backstop["next_wake_after"] = 0
        _write_record(r, redispatch_backstop)
        redispatch_backstop_events: list[dict] = []
        process_expired_handoffs(
            r,
            session_id="conductor-codex",
            load_task=lambda task_id: {
                "status": "pending",
                "owner_lane": "conductor-codex",
                "execution_mode": "execute",
                "effect_class": "internal",
                "deps_status": "met",
                "required_inputs": [],
            },
            send_wake=lambda event: redispatch_backstop_events.append(event),
            prefix=PREFIX,
        )
        print(
            "PASS redispatch-state-still-honors-backstop"
            if redispatch_backstop_events and redispatch_backstop_events[0]["delivery_failure_reason"] == "backstop_expired"
            else f"FAIL redispatch-state-still-honors-backstop {redispatch_backstop_events}"
        )

        outward = _record(
            "conductor-codex",
            "worker-codex",
            "task-outward",
            delivery_state="not_deliverable",
            delivery_failure_reason="tmux_missing",
        )
        _write_record(r, outward)
        outward_events: list[dict] = []
        process_expired_handoffs(
            r,
            session_id="conductor-codex",
            load_task=lambda task_id: {
                "status": "pending",
                "owner_lane": "conductor-codex",
                "execution_mode": "execute",
                "effect_class": "outward_irreversible",
                "deps_status": "met",
                "required_inputs": [],
            },
            send_wake=lambda event: outward_events.append(event),
            prefix=PREFIX,
        )
        print(
            "PASS outward-no-execute-nudge"
            if outward_events and outward_events[0]["type"] == "handoff_triage" and outward_events[0]["actionability"] == "outward_irreversible"
            else f"FAIL outward-no-execute-nudge {outward_events}"
        )

        triage = _record(
            "conductor-codex",
            "worker-codex",
            "task-triage",
            delivery_state="not_deliverable",
            delivery_failure_reason="tmux_missing",
        )
        _write_record(r, triage)
        triage_events: list[dict] = []
        process_expired_handoffs(
            r,
            session_id="conductor-codex",
            load_task=lambda task_id: {
                "status": "pending",
                "owner_lane": "conductor-codex",
                "execution_mode": "triage_only",
                "effect_class": "internal",
                "deps_status": "met",
                "required_inputs": [],
            },
            send_wake=lambda event: triage_events.append(event),
            prefix=PREFIX,
        )
        print(
            "PASS packet-less-triage-only"
            if triage_events and triage_events[0]["type"] == "handoff_triage" and triage_events[0]["actionability"] == "execution_mode=triage_only"
            else f"FAIL packet-less-triage-only {triage_events}"
        )

        backstop = _record(
            "conductor-codex",
            "worker-codex",
            "task-backstop",
            deadline_delta=-10,
        )
        _write_record(r, backstop)
        backstop_events: list[dict] = []
        process_expired_handoffs(
            r,
            session_id="conductor-codex",
            load_task=lambda task_id: {
                "status": "pending",
                "owner_lane": "conductor-codex",
                "execution_mode": "execute",
                "effect_class": "internal",
                "deps_status": "met",
                "required_inputs": [],
            },
            send_wake=lambda event: backstop_events.append(event),
            prefix=PREFIX,
        )
        print(
            "PASS long-backstop-safety-net"
            if backstop_events and backstop_events[0]["delivery_failure_reason"] == "backstop_expired"
            else f"FAIL long-backstop-safety-net {backstop_events}"
        )

        failed_wake = _record(
            "conductor-codex",
            "worker-codex",
            "task-failed-wake",
            delivery_state="not_deliverable",
            delivery_failure_reason="inject_failed",
        )
        failed_key = _write_record(r, failed_wake)
        failed_wake_events: list[dict] = []
        process_expired_handoffs(
            r,
            session_id="conductor-codex",
            load_task=lambda task_id: {
                "status": "pending",
                "owner_lane": "conductor-codex",
                "execution_mode": "execute",
                "effect_class": "internal",
                "deps_status": "met",
                "required_inputs": [],
            },
            send_wake=lambda event: (failed_wake_events.append(event), False)[1],
            prefix=PREFIX,
        )
        failed_record = json.loads(r.get(failed_key))
        print(
            "PASS wake-failure-still-advances"
            if failed_record.get("wake_count") == 1 and failed_record.get("last_wake_error") == "send_wake_returned_false"
            else f"FAIL wake-failure-still-advances {failed_record}"
        )

        capped_backoff = _record(
            "conductor-codex",
            "worker-codex",
            "task-capped-backoff",
            delivery_state="not_deliverable",
            delivery_failure_reason="inject_failed",
        )
        capped_backoff["wake_count"] = 12
        capped_backoff["next_wake_after"] = 0
        capped_key = _write_record(r, capped_backoff)
        before = time.time()
        process_expired_handoffs(
            r,
            session_id="conductor-codex",
            load_task=lambda task_id: {
                "status": "pending",
                "owner_lane": "conductor-codex",
                "execution_mode": "execute",
                "effect_class": "internal",
                "deps_status": "met",
                "required_inputs": [],
            },
            send_wake=lambda event: True,
            prefix=PREFIX,
            max_attempts=20,
        )
        capped_record = json.loads(r.get(capped_key))
        next_wake_after = float(capped_record["next_wake_after"])
        delay = round(next_wake_after - before)
        print(
            "PASS capped-backoff-exponent"
            if 60000 <= delay <= 62000
            else f"FAIL capped-backoff-exponent delay={delay} record={capped_record}"
        )

        module_text = (ROOT / "lib" / "handoff_validation.py").read_text(encoding="utf-8")
        print(
            "PASS import-guard-zero-neo4j"
            if "neo4j" not in module_text and "DEPENDS_ON" not in module_text
            else "FAIL import-guard-zero-neo4j"
        )
        return 0
    finally:
        _cleanup(PREFIX)


if __name__ == "__main__":
    raise SystemExit(main())
