#!/usr/bin/env python3
"""Acceptance: supervisor-owned out-of-band work is a real in-flight signal."""
from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _require_test_namespace() -> str:
    raw = (os.environ.get("ORCH_TEST_NAMESPACE") or "").strip()
    if not raw:
        raise SystemExit("ORCH_TEST_NAMESPACE is required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{5,80}", raw):
        raise SystemExit("ORCH_TEST_NAMESPACE must be 6-80 chars of letters/digits/._:-")
    if not any(marker in raw.lower() for marker in ("test", "ci", "acceptance")):
        raise SystemExit("ORCH_TEST_NAMESPACE must include test, ci, or acceptance")
    return raw


PFX = f"{_require_test_namespace()}-sup-oob-{uuid.uuid4().hex[:8]}"
os.environ["NOTIFY_KEY_PREFIX"] = PFX
os.environ["ORCH_WORKER_TASK_LIVENESS"] = "1"
os.environ["ORCH_WORKER_TASK_LIVENESS_TTL_SEC"] = "1"

from fleet_orchestrator.config import OrchConfig, get_neo4j_driver, get_redis_sync  # noqa: E402
from fleet_orchestrator.notify_state import redis_connect as notify_redis_connect  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    _raw_stop_decision,
    create_phase,
    create_project,
    create_task,
    get_task,
    init_schema,
    update_task_status,
)
from fleet_orchestrator.out_of_band import (  # noqa: E402
    clear_out_of_band_task,
    out_of_band_task_key,
    register_out_of_band_task,
)
from fleet_orchestrator.worker_liveness import (  # noqa: E402
    escalate_stale_worker_tasks,
    register_worker_task_liveness,
)


CFG = OrchConfig()
SUP = f"{PFX}-sup"
PROJECT = f"{PFX}-project"
PHASE = f"{PROJECT}::phase"
TASK = f"{PROJECT}::supervisor-owned-oob"
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
    clear_out_of_band_task(TASK, config=CFG)
    _delete_matching(get_redis_sync(CFG), f"{PFX}:*")
    _delete_matching(notify_redis_connect(), f"{PFX}:*")
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=PFX)


def _make_oob_stale(payload: dict) -> None:
    payload = dict(payload)
    payload["heartbeat_at"] = time.time() - 10
    get_redis_sync(CFG).set(out_of_band_task_key(TASK), json.dumps(payload, separators=(",", ":")), ex=30)


def main() -> int:
    _cleanup()
    try:
        init_schema(config=CFG)
        create_project(project_id=PROJECT, name=PROJECT, supervisor=SUP, priority=1, config=CFG)
        create_phase(project_id=PROJECT, phase_id=PHASE, name="phase", config=CFG)
        create_task(
            phase_id=PHASE,
            task_id=TASK,
            description="supervisor-owned out-of-band work",
            owner=SUP,
            priority=1,
            wake_owner_if_ready=False,
            config=CFG,
        )
        update_task_status(TASK, "in_progress", owner=SUP, config=CFG)
        register_worker_task_liveness(
            SUP,
            TASK,
            "supervisor-owned out-of-band work",
            supervisor=SUP,
            started_at=time.time() - 10,
            ttl_secs=1,
            config=CFG,
        )
        payload = register_out_of_band_task(
            TASK,
            supervisor=SUP,
            owner=SUP,
            runner=f"{PFX}-spark-runner",
            heartbeat_ttl_secs=5,
            details="acceptance remote process",
            config=CFG,
        )

        fresh_decision = _raw_stop_decision(SUP, config=CFG)
        _check(
            "fresh supervisor-owned out-of-band work allows stop",
            fresh_decision.get("block") is False and fresh_decision.get("wake_type") == "ALLOW_STOP",
            fresh_decision,
        )
        fresh_escalated = escalate_stale_worker_tasks(now=time.time(), config=CFG, task_id_prefix=PFX, project_id_prefix=PFX)
        fresh_task = get_task(TASK, config=CFG)
        _check(
            "fresh supervisor-owned out-of-band work is not reclaimed",
            not fresh_escalated and fresh_task.get("status") == "in_progress" and fresh_task.get("needs_attention") is not True,
            {"escalated": fresh_escalated, "task": fresh_task},
        )

        _make_oob_stale(payload)
        stale_decision = _raw_stop_decision(SUP, config=CFG)
        _check(
            "expired supervisor-owned out-of-band work blocks stop again",
            stale_decision.get("block") is True and stale_decision.get("task_id") == TASK,
            stale_decision,
        )
        stale_escalated = escalate_stale_worker_tasks(now=time.time(), config=CFG, task_id_prefix=PFX, project_id_prefix=PFX)
        stale_task = get_task(TASK, config=CFG)
        _check(
            "expired supervisor-owned out-of-band work is reclaimed by liveness",
            bool(stale_escalated) and stale_task.get("status") == "pending" and stale_task.get("needs_attention") is True,
            {"escalated": stale_escalated, "task": stale_task},
        )
    finally:
        _cleanup()

    if FAILURES:
        print(f"\nFAIL -- {len(FAILURES)}: {FAILURES}")
        return 1
    print("\nPASS -- supervisor-owned out-of-band work gates stop and worker-liveness until heartbeat expiry.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
