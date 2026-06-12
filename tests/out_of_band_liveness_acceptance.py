"""Ship-gate e2e — out-of-band harness liveness suppresses false supervisor wakes.

Long-running subprocess/harness work does not run inside the peer session, so
the peer's Redis current_task can be clear while real work continues. The
canonical register path is task-scoped: a fresh out-of-band heartbeat means the
task is actively in-flight; a stale/missing heartbeat falls back to the existing
supervisor gate/investigate wake.
"""
from __future__ import annotations

import os
import re
import sys
import time
import uuid
import json

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet_orchestrator.config import OrchConfig  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    _raw_stop_decision,
    create_phase,
    create_project,
    create_task,
    get_neo4j_driver,
    init_schema,
    update_task_status,
)
from fleet_orchestrator.out_of_band import (  # noqa: E402
    clear_out_of_band_task,
    out_of_band_task_key,
    register_out_of_band_task,
)


def _require_test_namespace() -> str:
    raw = (os.environ.get("ORCH_TEST_NAMESPACE") or "").strip()
    if not raw:
        raise SystemExit("ORCH_TEST_NAMESPACE is required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{5,80}", raw):
        raise SystemExit("ORCH_TEST_NAMESPACE must be 6-80 chars of letters/digits/._:-")
    if not any(marker in raw.lower() for marker in ("test", "ci", "acceptance")):
        raise SystemExit("ORCH_TEST_NAMESPACE must include test, ci, or acceptance")
    return raw


CFG = OrchConfig()
PFX = f"{_require_test_namespace()}-oob-{uuid.uuid4().hex[:8]}"
SUP = f"{PFX}-sup"
PEER = f"{SUP}-codex"
PROJECT = f"{PFX}-project"
PHASE = f"{PROJECT}::phase"
TASK = f"{PROJECT}::task"
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _cleanup() -> None:
    clear_out_of_band_task(TASK, config=CFG)
    driver = get_neo4j_driver(CFG)
    with driver.session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=PFX)


def main() -> int:
    _cleanup()
    try:
        init_schema(config=CFG)
        create_project(project_id=PROJECT, name=PROJECT, supervisor=SUP, priority=1, config=CFG)
        create_phase(project_id=PROJECT, phase_id=PHASE, name="phase", config=CFG)
        create_task(phase_id=PHASE, task_id=TASK, description="out-of-band harness task", owner=PEER, priority=1, wake_owner_if_ready=False, config=CFG)
        update_task_status(TASK, "in_progress", owner=PEER, config=CFG)

        stale = _raw_stop_decision(SUP, config=CFG)
        _check("unregistered in-progress peer task BLOCKS for gate/investigate",
               stale.get("block") is True and stale.get("task_id") == TASK and stale.get("gate_for") == PEER,
               stale)

        payload = register_out_of_band_task(
            TASK,
            supervisor=SUP,
            owner=PEER,
            runner=f"{PFX}-harness",
            heartbeat_ttl_secs=5,
            details="acceptance subprocess",
            config=CFG,
        )
        active = _raw_stop_decision(SUP, config=CFG)
        _check("registered fresh out-of-band heartbeat -> ALLOW_STOP",
               active.get("wake_type") == "ALLOW_STOP" and active.get("block") is False,
               active)
        _check("registration is task-scoped, not peer-session current_task",
               out_of_band_task_key(TASK).endswith(TASK) and payload["owner"] == PEER,
               payload)

        from fleet_orchestrator.config import get_redis_sync

        payload["heartbeat_at"] = time.time() - 10
        get_redis_sync(CFG).set(out_of_band_task_key(TASK), json.dumps(payload), ex=30)
        blocked = _raw_stop_decision(SUP, config=CFG)
        _check("stale out-of-band heartbeat BLOCKS for gate/investigate",
               blocked.get("block") is True and blocked.get("task_id") == TASK and blocked.get("gate_for") == PEER,
               blocked)
    finally:
        _cleanup()

    if FAILURES:
        print(f"\nFAIL — {len(FAILURES)}: {FAILURES}")
        return 1
    print("\nPASS — out-of-band registered liveness suppresses false wake while fresh and blocks when stale.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
