#!/usr/bin/env python3
"""Acceptance: notify state is not split from dispatch when Redis configs diverge.

Issue #138: dispatch and the Stop hook use fleet-notify Redis
(``REDIS_HOST``/``REDIS_PORT``), while some orchestrator readers used
``ORCH_REDIS_*`` for ``${NOTIFY_KEY_PREFIX}:...`` keys. This test deliberately
points the two configs at different Redis instances. Dispatch writes a
``current_task`` to notify Redis; stop-engine and worker-liveness readers must
see that same binding.
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
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
        raise SystemExit("redis_split_brain_acceptance requires ORCH_REDIS_* and REDIS_* to point at different Redis instances")


_NAMESPACE = _require_test_namespace()
_require_divergent_redis()
PFX = f"{_NAMESPACE}-split-brain-{uuid.uuid4().hex[:8]}"
os.environ["NOTIFY_KEY_PREFIX"] = PFX
os.environ["ORCH_WORKER_TASK_LIVENESS"] = "1"
os.environ["ORCH_WORKER_TASK_LIVENESS_TTL_SEC"] = "1"

from fleet_orchestrator import dispatch as dispatch_module  # noqa: E402
from fleet_orchestrator.config import OrchConfig, get_neo4j_driver, get_redis_sync  # noqa: E402
from fleet_orchestrator.dispatch import _state_key  # noqa: E402
from fleet_orchestrator.notify_state import redis_connect as notify_redis_connect  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    _observed_stop_task_id,
    _peer_actively_working_task,
    create_phase,
    create_project,
    create_task,
    get_session_liveness,
    get_task,
    init_schema,
)
from fleet_orchestrator.worker_liveness import (  # noqa: E402
    escalate_stale_worker_tasks,
    register_worker_task_liveness,
)


CFG = OrchConfig()
PROJECT = f"{PFX}-project"
PHASE = f"{PROJECT}::phase"
TASK = f"{PROJECT}::task"
SUP = f"{PFX}-sup"
WORKER = f"{SUP}-codex"
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _delete_matching(r, *patterns: str) -> None:
    for pattern in patterns:
        cursor = 0
        while True:
            cursor, keys = r.scan(cursor=cursor, match=pattern, count=100)
            if keys:
                r.delete(*keys)
            if cursor == 0:
                break


def _cleanup() -> None:
    patterns = (
        f"{PFX}:*",
        f"{PFX}:worker-task-liveness:{PFX}*",
        f"{PFX}:worker-task-liveness-escalated:{PFX}*",
    )
    _delete_matching(notify_redis_connect(), *patterns)
    _delete_matching(get_redis_sync(CFG), *patterns)
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=PFX)


def main() -> int:
    _cleanup()
    try:
        init_schema(config=CFG)
        create_project(project_id=PROJECT, name="redis split brain acceptance", supervisor=SUP, config=CFG)
        create_phase(project_id=PROJECT, phase_id=PHASE, name="phase", config=CFG)
        create_task(
            phase_id=PHASE,
            task_id=TASK,
            description="redis split brain regression",
            owner=WORKER,
            priority=10,
            wake_owner_if_ready=False,
            config=CFG,
        )

        ok = SimpleNamespace(returncode=0, stdout="OK", stderr="")
        with mock.patch.object(dispatch_module.subprocess, "run", return_value=ok):
            dispatch_module.dispatch(WORKER, TASK, "redis split brain regression", supervisor=SUP)

        notify_r = notify_redis_connect()
        orch_r = get_redis_sync(CFG)
        notify_raw = notify_r.get(_state_key(WORKER, "current_task"))
        orch_raw = orch_r.get(_state_key(WORKER, "current_task"))
        current_task = json.loads(notify_raw) if notify_raw else {}

        _check("dispatch writes current_task to notify Redis", current_task.get("task_id") == TASK, current_task)
        _check("divergent ORCH Redis has no notify current_task copy", not orch_raw, orch_raw)
        _check("stop-engine observed current_task via notify Redis", _observed_stop_task_id(WORKER, config=CFG) == TASK)

        notify_r.delete(_state_key(WORKER, "idle"))
        notify_r.set(_state_key(WORKER, "last_tool_activity"), str(time.time()))
        liveness = get_session_liveness(WORKER, config=CFG)
        _check("session liveness reads notify heartbeat", liveness.get("active") is True, liveness)
        _check("peer working check reads notify current_task and heartbeat",
               _peer_actively_working_task([WORKER], TASK, config=CFG) is True)

        stale_started_at = time.time() - 5
        register_worker_task_liveness(
            WORKER,
            TASK,
            "redis split brain regression",
            supervisor=SUP,
            started_at=stale_started_at,
            ttl_secs=1,
            config=CFG,
        )
        notify_r.set(_state_key(WORKER, "last_tool_activity"), str(time.time()))
        escalated = escalate_stale_worker_tasks(now=time.time(), config=CFG, task_id_prefix=PFX, project_id_prefix=PFX)
        task = get_task(TASK, config=CFG)
        _check("worker liveness sees notify current_task and does not false-escalate",
               not escalated and task.get("status") == "in_progress",
               {"escalated": escalated, "task": task})
    finally:
        _cleanup()

    if FAILURES:
        print(f"\nFAIL -- {len(FAILURES)}: {FAILURES}")
        return 1
    print("\nPASS -- notify Redis current_task remains coherent when ORCH_REDIS and REDIS diverge.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
