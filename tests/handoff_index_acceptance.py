#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import time
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PREFIX = f"hfindex-{uuid.uuid4().hex[:8]}"
DISPATCHER = f"{PREFIX}-codex"
TARGET = f"{PREFIX}-gemini"
TASK_ID = f"{PREFIX}-task"
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

from fleet_orchestrator.config import OrchConfig, get_redis_sync  # noqa: E402
from fleet_orchestrator.handoff_validation import (  # noqa: E402
    _scan_dispatcher_handoffs,
    handoff_index_key,
    handoff_key,
    validate_stop_handoff,
    write_handoff_record,
)

CFG = OrchConfig()
FAILURES: list[str] = []


class NoScanRedis:
    def __init__(self, inner):
        self._inner = inner

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def scan_iter(self, *args, **kwargs):
        raise AssertionError("no scan in hot path")


def _check(label: str, condition: bool, detail: object = "") -> None:
    print(("PASS " if condition else "FAIL ") + label)
    if not condition:
        FAILURES.append(f"{label}: {detail}")


def _cleanup(prefix: str) -> None:
    r = get_redis_sync(CFG)
    cursor = 0
    while True:
        cursor, keys = r.scan(cursor=cursor, match=f"{prefix}:*", count=100)
        if keys:
            r.delete(*keys)
        if cursor == 0:
            break


def _record() -> dict:
    now = time.time()
    msg_id = str(uuid.uuid4())
    return {
        "kind": "explicit_handoff",
        "dispatcher_session_id": DISPATCHER,
        "target_session_id": TARGET,
        "dispatcher_task_id": TASK_ID,
        "msg_id": msg_id,
        "message_hash": "hash-1",
        "actionable_inputs": {},
        "created_at": now - 30,
        "ack_deadline_at": now + 300,
        "ack_backstop_at": now + 300,
        "pickup_poll_budget": 5,
        "delivery_state": "queued",
        "delivery_poll_count": 0,
        "_key": handoff_key(PREFIX, DISPATCHER, msg_id),
    }


def main() -> int:
    _cleanup(PREFIX)
    try:
        r = get_redis_sync(CFG)
        record = _record()
        index_key = handoff_index_key(PREFIX, DISPATCHER)
        write_handoff_record(r, record, prefix=PREFIX)
        _check("handoff-write-populates-dispatcher-index", bool(r.sismember(index_key, record["msg_id"])))

        guarded = NoScanRedis(r)
        validation = validate_stop_handoff(guarded, DISPATCHER, TASK_ID, prefix=PREFIX, timeout_s=0.2)
        _check(
            "handoff-validation-hot-path-does-not-scan",
            validation.get("state") == "pending_unacked"
            and (validation.get("record") or {}).get("dispatcher_task_id") == TASK_ID,
            validation,
        )

        r.sadd(index_key, "missing-record")
        records = _scan_dispatcher_handoffs(guarded, DISPATCHER, PREFIX)
        _check(
            "handoff-index-reader-cleans-stale-members",
            len(records) == 1
            and records[0].get("msg_id") == record["msg_id"]
            and not r.sismember(index_key, "missing-record"),
            records,
        )
    finally:
        _cleanup(PREFIX)

    if FAILURES:
        print(f"FAIL - {len(FAILURES)} failure(s): {FAILURES}")
        return 1
    print("PASS - handoff validation uses dispatcher index without scan")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
