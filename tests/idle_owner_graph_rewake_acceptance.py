#!/usr/bin/env python3
"""Acceptance: orch-watch re-wakes idle owners with graph-only current work."""
from __future__ import annotations

import json
import os
import re
import sys
import uuid
from pathlib import Path
from unittest import mock


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


PFX = f"{_require_test_namespace()}-owner-rewake-{uuid.uuid4().hex[:8]}"
os.environ["NOTIFY_KEY_PREFIX"] = PFX

from fleet_orchestrator import cli_orch_watch as watch  # noqa: E402
from fleet_orchestrator.config import OrchConfig, get_neo4j_driver  # noqa: E402
from fleet_orchestrator.notify_state import redis_connect as notify_redis_connect  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    _state_key,
    create_phase,
    create_project,
    create_task,
    init_schema,
    update_task_status,
)


CFG = OrchConfig()
OWNER = f"{PFX}-owner"
PROJECT = f"{PFX}-project"
PHASE = f"{PROJECT}::phase"
CURRENT = f"{PROJECT}::current"
NEXT = f"{PROJECT}::next"
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _redis():
    return notify_redis_connect()


def _cleanup() -> None:
    r = _redis()
    cursor = 0
    while True:
        cursor, keys = r.scan(cursor=cursor, match=f"{PFX}:*", count=100)
        if keys:
            r.delete(*keys)
        if cursor == 0:
            break
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=PFX)


def _setup() -> None:
    init_schema(config=CFG)
    create_project(project_id=PROJECT, name=PROJECT, supervisor=OWNER, priority=1, config=CFG)
    create_phase(project_id=PROJECT, phase_id=PHASE, name="phase", config=CFG)
    create_task(
        phase_id=PHASE,
        task_id=CURRENT,
        description="graph-only in-progress work",
        owner=OWNER,
        priority=20,
        wake_owner_if_ready=False,
        config=CFG,
    )
    create_task(
        phase_id=PHASE,
        task_id=NEXT,
        description="ready downstream owner work",
        owner=OWNER,
        priority=10,
        wake_owner_if_ready=False,
        config=CFG,
    )
    update_task_status(CURRENT, "in_progress", owner=OWNER, config=CFG)


def main() -> int:
    _cleanup()
    try:
        _setup()
        r = _redis()
        r.set(_state_key(OWNER, "idle"), "1")
        r.delete(_state_key(OWNER, "current_task"))
        sent: list[tuple[str, str]] = []

        def fake_send(_r, target, body, **_kwargs):
            sent.append((target, body))
            return True

        with mock.patch.object(watch, "_send_wake", side_effect=fake_send):
            count = watch._process_idle_owner_graph_work(r, dedup_ttl_sec=30, task_id_prefix=PFX)
        _check("idle graph owner got one wake", count == 1 and len(sent) == 1, {"count": count, "sent": sent})
        _check("wake targets owner", sent and sent[0][0] == OWNER, sent)
        _check("wake surfaces ready task", sent and NEXT in sent[0][1], sent[0][1] if sent else "")

        with mock.patch.object(watch, "_send_wake", side_effect=fake_send):
            deduped = watch._process_idle_owner_graph_work(r, dedup_ttl_sec=30, task_id_prefix=PFX)
        _check("owner graph wake dedups", deduped == 0 and len(sent) == 1, {"deduped": deduped, "sent": sent})

        r.delete(f"{PFX}:orch-watch-owner-graph-work:{OWNER}:{NEXT}")
        r.set(_state_key(OWNER, "current_task"), json.dumps({"task_id": CURRENT}))
        with mock.patch.object(watch, "_send_wake", side_effect=fake_send):
            bound = watch._process_idle_owner_graph_work(r, dedup_ttl_sec=30, task_id_prefix=PFX)
        _check("Redis-bound owner is left to current_task sweep", bound == 0 and len(sent) == 1, {"bound": bound, "sent": sent})
    finally:
        _cleanup()

    if FAILURES:
        print(f"\nFAIL -- {len(FAILURES)}: {FAILURES}")
        return 1
    print("\nPASS -- orch-watch re-wakes idle self-owned graph work without a Redis current_task.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
