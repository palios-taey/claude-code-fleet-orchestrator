#!/usr/bin/env python3
"""Acceptance: stale Default Project in-progress tasks do not poison current.

Default Project tasks are ad-hoc task API work. A real current Default task is
bound in Redis current_task by the in-progress write/dispatch path; old DB-only
Default in-progress rows must not outrank or mask actual planned work.
"""
from __future__ import annotations

import json
import os
import re
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet_orchestrator.config import OrchConfig  # noqa: E402
from fleet_orchestrator.dispatch import _state_key  # noqa: E402
from fleet_orchestrator.notify_state import redis_connect as notify_redis_connect  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    create_phase,
    create_project,
    create_task,
    get_neo4j_driver,
    get_session_current_work,
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
PFX = f"{_require_test_namespace()}-cur-{uuid.uuid4().hex[:8]}"
OWNER = f"{PFX}-worker"
DEFAULT_PHASE = "default::main"
STALE_DEFAULT = f"default::{PFX}-stale"
LIVE_DEFAULT = f"default::{PFX}-live"
PROJECT = f"{PFX}-project"
PHASE = f"{PROJECT}::phase"
PLANNED = f"{PROJECT}::planned"
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _cleanup() -> None:
    notify_redis_connect().delete(_state_key(OWNER, "current_task"))
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=PFX)
        session.run(
            "MATCH (n) WHERE n.id IN $ids DETACH DELETE n",
            ids=[STALE_DEFAULT, LIVE_DEFAULT],
        )


def _bind(task_id: str) -> None:
    notify_redis_connect().set(
        _state_key(OWNER, "current_task"),
        json.dumps({"task_id": task_id, "description": "bound default", "supervisor": OWNER, "started_at": 1.0}),
    )


def main() -> int:
    _cleanup()
    try:
        init_schema(config=CFG)
        create_project(project_id="default", name="Default Project", config=CFG)
        create_phase(project_id="default", phase_id=DEFAULT_PHASE, name="Main", config=CFG)
        create_task(phase_id=DEFAULT_PHASE, task_id=STALE_DEFAULT, description="stale default", priority=1, owner=OWNER, wake_owner_if_ready=False, config=CFG)
        update_task_status(STALE_DEFAULT, "in_progress", owner=OWNER, config=CFG)

        _check("DB-only Default in_progress is not current", get_session_current_work(OWNER, config=CFG) is None, get_session_current_work(OWNER, config=CFG))

        create_project(project_id=PROJECT, name=PROJECT, priority=1, config=CFG)
        create_phase(project_id=PROJECT, phase_id=PHASE, name="phase", config=CFG)
        create_task(phase_id=PHASE, task_id=PLANNED, description="planned", priority=50, owner=OWNER, wake_owner_if_ready=False, config=CFG)
        update_task_status(PLANNED, "in_progress", owner=OWNER, config=CFG)
        current = get_session_current_work(OWNER, config=CFG)
        _check("planned in_progress outranks stale Default task", bool(current) and current["top_task_id"] == PLANNED, current)

        create_task(phase_id=DEFAULT_PHASE, task_id=LIVE_DEFAULT, description="live default", priority=0, owner=OWNER, wake_owner_if_ready=False, config=CFG)
        update_task_status(LIVE_DEFAULT, "in_progress", owner=OWNER, config=CFG)
        _bind(LIVE_DEFAULT)
        current = get_session_current_work(OWNER, config=CFG)
        _check("Redis-bound Default task can still be current", bool(current) and current["top_task_id"] == LIVE_DEFAULT, current)
    finally:
        _cleanup()

    if FAILURES:
        print(f"\nFAIL — {len(FAILURES)}: {FAILURES}")
        return 1
    print("\nPASS — Default Project current work reconciles with Redis current_task.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
