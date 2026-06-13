"""Ship-gate e2e — terminal task writes clear only the matching owner current_task.

Supervisor-side task closure can move an OrchTask to a terminal status without
the worker's Stop hook running. The status transition is therefore the canonical
place to reconcile Redis session state: clear taey:<owner>:current_task iff it
still points at the terminal task, and preserve it if the session has moved on.
"""
from __future__ import annotations

import json
import os
import re
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet_orchestrator.config import OrchConfig, get_redis_sync  # noqa: E402
from fleet_orchestrator.dispatch import _state_key  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    create_phase,
    create_project,
    create_task,
    get_neo4j_driver,
    init_schema,
    update_task_status,
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
PFX = f"{_require_test_namespace()}-ctclear-{uuid.uuid4().hex[:8]}"
SUP = f"{PFX}-sup"
OWNER = f"{SUP}-codex"
NEW_OWNER = f"{SUP}-gemini"
PROJECT = f"{PFX}-project"
PHASE = f"{PROJECT}::phase"
MATCHING = f"{PROJECT}::matching"
OTHER = f"{PROJECT}::other"
INTERRUPTED = f"{PROJECT}::interrupted"
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _redis():
    return get_redis_sync(CFG)


def _bind(task_id: str) -> None:
    _redis().set(
        _state_key(OWNER, "current_task"),
        json.dumps({"task_id": task_id, "description": task_id, "supervisor": SUP, "started_at": 123.0}),
    )


def _current_task_id() -> str:
    raw = _redis().get(_state_key(OWNER, "current_task"))
    if not raw:
        return ""
    return str(json.loads(raw).get("task_id") or "")


def _new_owner_current_task_id() -> str:
    raw = _redis().get(_state_key(NEW_OWNER, "current_task"))
    if not raw:
        return ""
    return str(json.loads(raw).get("task_id") or "")


def _cleanup() -> None:
    _redis().delete(_state_key(OWNER, "current_task"))
    _redis().delete(_state_key(NEW_OWNER, "current_task"))
    driver = get_neo4j_driver(CFG)
    with driver.session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=PFX)


def main() -> int:
    _cleanup()
    try:
        init_schema(config=CFG)
        create_project(project_id=PROJECT, name=PROJECT, supervisor=SUP, priority=1, config=CFG)
        create_phase(project_id=PROJECT, phase_id=PHASE, name="phase", config=CFG)
        for task_id in (MATCHING, OTHER, INTERRUPTED):
            create_task(phase_id=PHASE, task_id=task_id, description=task_id, owner=OWNER, wake_owner_if_ready=False, config=CFG)
            update_task_status(task_id, "in_progress", owner=OWNER, config=CFG)

        _bind(MATCHING)
        update_task_status(
            MATCHING,
            "completed",
            completion_evidence={"production_observation": "terminal clear acceptance completed"},
            config=CFG,
        )
        _check("completed clears matching owner current_task", _current_task_id() == "", _current_task_id())

        _bind(OTHER)
        update_task_status(
            MATCHING,
            "failed",
            completion_evidence={"reason": "terminal clear acceptance nonmatching fixture"},
            config=CFG,
        )
        _check("failed terminal write leaves nonmatching current_task untouched", _current_task_id() == OTHER, _current_task_id())

        _bind(INTERRUPTED)
        update_task_status(
            INTERRUPTED,
            "interrupted",
            completion_evidence={"reason": "terminal clear acceptance interrupted fixture"},
            config=CFG,
        )
        _check("interrupted clears matching owner current_task", _current_task_id() == "", _current_task_id())

        _bind(OTHER)
        _redis().set(
            _state_key(NEW_OWNER, "current_task"),
            json.dumps({"task_id": "unrelated", "description": "unrelated", "supervisor": SUP, "started_at": 456.0}),
        )
        update_task_status(
            OTHER,
            "completed",
            owner=NEW_OWNER,
            completion_evidence={"production_observation": "terminal clear acceptance reassign"},
            config=CFG,
        )
        _check("terminal owner reassign clears previous owner's matching current_task", _current_task_id() == "", _current_task_id())
        _check("terminal owner reassign preserves new owner's nonmatching current_task",
               _new_owner_current_task_id() == "unrelated",
               _new_owner_current_task_id())
    finally:
        _cleanup()

    if FAILURES:
        print(f"\nFAIL — {len(FAILURES)}: {FAILURES}")
        return 1
    print("\nPASS — terminal status writes clear only matching owner current_task.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
