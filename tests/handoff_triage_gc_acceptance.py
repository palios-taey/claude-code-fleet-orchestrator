#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _require_test_namespace() -> str:
    raw = (os.environ.get("ORCH_TEST_NAMESPACE") or "").strip()
    if not raw:
        raise SystemExit("ORCH_TEST_NAMESPACE is required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{5,80}", raw):
        raise SystemExit("ORCH_TEST_NAMESPACE must be 6-80 chars of letters/digits/._:-")
    if not any(marker in raw.lower() for marker in ("test", "ci", "acceptance")):
        raise SystemExit("ORCH_TEST_NAMESPACE must include test, ci, or acceptance")
    return raw


_NAMESPACE = _require_test_namespace()
PREFIX = f"{_NAMESPACE}-hfgc-{uuid.uuid4().hex[:8]}"
DISPATCHER = f"{PREFIX}-codex"
TARGET = f"{PREFIX}-gemini"
os.environ["NOTIFY_KEY_PREFIX"] = PREFIX
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
os.environ.setdefault("ORCH_NEO4J_URI", "bolt://localhost:7687")
os.environ.setdefault("ORCH_NEO4J_DB", "neo4j")

from fleet_orchestrator.config import OrchConfig, get_redis_sync  # noqa: E402
from fleet_orchestrator.handoff_validation import (  # noqa: E402
    handoff_index_key,
    process_expired_handoffs,
    write_handoff_record,
)


CFG = OrchConfig()
FAILURES: list[str] = []


def _check(label: str, condition: bool, detail: object = "") -> None:
    print(("PASS " if condition else "FAIL ") + label)
    if not condition:
        FAILURES.append(f"{label}: {detail}")


def _cleanup() -> None:
    r = get_redis_sync(CFG)
    cursor = 0
    while True:
        cursor, keys = r.scan(cursor=cursor, match=f"{PREFIX}:*", count=100)
        if keys:
            r.delete(*keys)
        if cursor == 0:
            break


def _record(task_id: str, *, wake_count: int = 0, next_wake_after: float = 0) -> dict:
    now = time.time()
    record = {
        "kind": "explicit_handoff",
        "dispatcher_session_id": DISPATCHER,
        "target_session_id": TARGET,
        "dispatcher_task_id": task_id,
        "msg_id": str(uuid.uuid4()),
        "message_hash": "hash-1",
        "actionable_inputs": {},
        "created_at": now - 3600,
        "ack_deadline_at": now - 300,
        "ack_backstop_at": now - 300,
        "pickup_poll_budget": 5,
        "delivery_state": "not_deliverable",
        "delivery_failure_reason": "backstop_expired",
        "delivery_poll_count": 0,
        "wake_count": wake_count,
        "next_wake_after": next_wake_after,
    }
    record["_key"] = f"{PREFIX}:handoff:{DISPATCHER}:{record['msg_id']}"
    return record


def _write(record: dict) -> str:
    r = get_redis_sync(CFG)
    write_handoff_record(r, record, prefix=PREFIX)
    return str(record["_key"])


def _is_deleted(record: dict) -> bool:
    r = get_redis_sync(CFG)
    index_key = handoff_index_key(PREFIX, DISPATCHER)
    return r.get(record["_key"]) is None and not r.sismember(index_key, record["msg_id"])


def main() -> int:
    _cleanup()
    try:
        completed = _record("completed-task", wake_count=99, next_wake_after=time.time() + 3600)
        killed = _record("killed-task")
        orphaned = _record("missing-task")
        inflight = _record("inflight-task")
        for record in (completed, killed, orphaned, inflight):
            _write(record)

        tasks = {
            "completed-task": {"status": "completed", "owner_lane": DISPATCHER},
            "killed-task": {"status": "killed", "owner_lane": DISPATCHER},
            "inflight-task": {"status": "in_progress", "owner_lane": DISPATCHER},
        }
        sent: list[dict] = []
        events = process_expired_handoffs(
            get_redis_sync(CFG),
            session_id=DISPATCHER,
            load_task=lambda task_id: tasks.get(task_id),
            send_wake=lambda event: sent.append(dict(event)),
            prefix=PREFIX,
            max_attempts=3,
        )

        _check("terminal completed handoff record deleted", _is_deleted(completed), completed)
        _check("terminal killed handoff record deleted", _is_deleted(killed), killed)
        _check("orphaned handoff record deleted", _is_deleted(orphaned), orphaned)
        _check(
            "terminal and orphaned records do not wake",
            all(event.get("dispatcher_task_id") not in {"completed-task", "killed-task", "missing-task"} for event in sent + events),
            {"sent": sent, "events": events},
        )

        r = get_redis_sync(CFG)
        inflight_record = json.loads(r.get(inflight["_key"]))
        _check("in-flight handoff record remains", not _is_deleted(inflight), inflight_record)
        _check(
            "in-flight handoff still triages",
            len(sent) == 1
            and sent[0].get("type") == "handoff_triage"
            and sent[0].get("dispatcher_task_id") == "inflight-task"
            and sent[0].get("actionability") == "status=in_progress",
            sent,
        )
        _check(
            "in-flight handoff advances retry state",
            inflight_record.get("state") == "redispatch_requested" and inflight_record.get("wake_count") == 1,
            inflight_record,
        )
    finally:
        _cleanup()

    if FAILURES:
        print(f"FAIL - {len(FAILURES)} failure(s): {FAILURES}")
        return 1
    print("PASS - terminal and orphaned handoff triage records self-GC without wakes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
