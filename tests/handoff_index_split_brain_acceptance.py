#!/usr/bin/env python3
"""Acceptance: startup handoff-index backfill uses notify Redis, not ORCH Redis."""
from __future__ import annotations

import copy
import os
import re
import sys
import time
import uuid
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fleet_orchestrator.test_isolation import assert_acceptance_redis_isolated  # noqa: E402


def _require_test_namespace() -> str:
    raw = (os.environ.get("ORCH_TEST_NAMESPACE") or "").strip()
    if not raw:
        raise SystemExit("ORCH_TEST_NAMESPACE is required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{5,80}", raw):
        raise SystemExit("ORCH_TEST_NAMESPACE must be 6-80 chars of letters/digits/._:-")
    if not any(marker in raw.lower() for marker in ("test", "ci", "acceptance")):
        raise SystemExit("ORCH_TEST_NAMESPACE must include test, ci, or acceptance")
    return raw


def _require_divergent_redis() -> None:
    assert_acceptance_redis_isolated()
    orch = (
        (os.environ.get("ORCH_REDIS_HOST") or "").strip(),
        (os.environ.get("ORCH_REDIS_PORT") or "").strip(),
    )
    notify = (
        (os.environ.get("REDIS_HOST") or "").strip(),
        (os.environ.get("REDIS_PORT") or "").strip(),
    )
    if orch == notify:
        raise SystemExit(
            "handoff_index_split_brain_acceptance requires ORCH_REDIS_* "
            "and REDIS_* to point at different Redis instances"
        )


_NAMESPACE = _require_test_namespace()
_require_divergent_redis()
PFX = f"{_NAMESPACE}-handoff-split-{uuid.uuid4().hex[:8]}"
os.environ["NOTIFY_KEY_PREFIX"] = PFX

from fleet_orchestrator import cli_orch_watch, tasks_api  # noqa: E402
from fleet_orchestrator.config import OrchConfig, get_redis_sync  # noqa: E402
from fleet_orchestrator.handoff_validation import (  # noqa: E402
    _BACKFILLED_PREFIXES,
    handoff_index_key,
    handoff_key,
    write_handoff_record,
)
from fleet_orchestrator.notify_state import redis_connect as notify_redis_connect  # noqa: E402


CFG = OrchConfig()
DISPATCHER = f"{PFX}-dispatcher"
TARGET = f"{PFX}-target"
TASK = f"{PFX}-task"
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _delete_matching(r, pattern: str) -> None:
    cursor = 0
    while True:
        cursor, keys = r.scan(cursor=cursor, match=pattern, count=100)
        if keys:
            r.delete(*keys)
        if cursor == 0:
            break


def _cleanup() -> None:
    _BACKFILLED_PREFIXES.discard(PFX)
    _delete_matching(notify_redis_connect(), f"{PFX}:*")
    _delete_matching(get_redis_sync(CFG), f"{PFX}:*")


def _record(msg_id: str, *, key_prefix: str = PFX) -> dict:
    now = time.time()
    return {
        "kind": "explicit_handoff",
        "dispatcher_session_id": DISPATCHER,
        "target_session_id": TARGET,
        "dispatcher_task_id": TASK,
        "msg_id": msg_id,
        "message_hash": f"hash-{msg_id}",
        "actionable_inputs": {},
        "created_at": now - 30,
        "ack_deadline_at": now + 300,
        "ack_backstop_at": now + 300,
        "pickup_poll_budget": 5,
        "delivery_state": "not_deliverable",
        "delivery_failure_reason": "split_brain_acceptance",
        "delivery_poll_count": 0,
        "_key": handoff_key(key_prefix, DISPATCHER, msg_id),
    }


def _task_state(task_id: str) -> dict:
    return {
        "id": task_id,
        "status": "pending",
        "owner_lane": DISPATCHER,
        "execution_mode": "execute",
        "effect_class": "internal",
        "deps_status": "met",
        "required_inputs": [],
    }


def main() -> int:
    _cleanup()
    notify_r = notify_redis_connect()
    orch_r = get_redis_sync(CFG)
    msg_id = str(uuid.uuid4())
    decoy_msg_id = f"orch-decoy-{uuid.uuid4().hex[:8]}"
    index_key = handoff_index_key(PFX, DISPATCHER)

    try:
        notify_record = _record(msg_id)
        write_handoff_record(notify_r, notify_record, prefix=PFX)
        notify_r.delete(index_key)

        orch_decoy = copy.deepcopy(_record(decoy_msg_id))
        write_handoff_record(orch_r, orch_decoy, prefix=PFX)
        orch_r.delete(index_key)

        _check(
            "fixture starts with notify handoff record but empty notify index",
            bool(notify_r.get(notify_record["_key"])) and not notify_r.smembers(index_key),
        )
        _check(
            "fixture starts with ORCH decoy record but empty ORCH index",
            bool(orch_r.get(orch_decoy["_key"])) and not orch_r.smembers(index_key),
        )

        with mock.patch.object(tasks_api, "init_schema", return_value={"errors": []}):
            tasks_api._init_schema_on_startup()

        notify_members = {str(member) for member in notify_r.smembers(index_key)}
        orch_members = {str(member) for member in orch_r.smembers(index_key)}
        _check("startup backfill rebuilds notify Redis handoff index", msg_id in notify_members, notify_members)
        _check("startup backfill does not rebuild ORCH Redis handoff index", decoy_msg_id not in orch_members and not orch_members, orch_members)

        wakes: list[dict] = []
        def _capture_wake(_r, target: str, body: str, **_kwargs) -> bool:
            wakes.append({"target": target, "body": body})
            return True

        with mock.patch.object(cli_orch_watch, "_load_task_state", side_effect=_task_state), \
                mock.patch.object(cli_orch_watch, "_send_wake", side_effect=_capture_wake):
            cli_orch_watch._process_handoff_timeouts(notify_r)

        _check("orch-watch finds backfilled notify handoff", len(wakes) == 1 and wakes[0]["target"] == DISPATCHER, wakes)
        _check("orch-watch does not consume ORCH decoy handoff", not orch_r.sismember(index_key, decoy_msg_id), orch_members)
    finally:
        _cleanup()

    if FAILURES:
        print(f"\nFAIL -- {len(FAILURES)}: {FAILURES}")
        return 1
    print("\nPASS -- handoff-index startup backfill and orch-watch use notify Redis under divergent configs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
