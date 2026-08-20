#!/usr/bin/env python3
"""Acceptance: unbind clears authoritative live executor binding.

Defect (task-48c4dbf4): taey-task unbind cleared Redis current_task only, while
GET /api/sessions/{peer}/current and taey-task status still derived WORKING from
Neo4j in_progress + worker_liveness_* / last_activity.

Env: ORCH_NEO4J_URI, ORCH_NEO4J_DB, Redis, ORCH_TEST_NAMESPACE required.
"""
from __future__ import annotations

import os
import re
import sys
import time
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _require_test_namespace() -> str:
    raw = (os.environ.get("ORCH_TEST_NAMESPACE") or "").strip()
    if not raw:
        raise SystemExit("ORCH_TEST_NAMESPACE is required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{5,80}", raw):
        raise SystemExit("ORCH_TEST_NAMESPACE must be 6-80 chars of letters/digits/._:-")
    lowered = raw.lower()
    if not any(marker in lowered for marker in ("test", "ci", "acceptance")):
        raise SystemExit("ORCH_TEST_NAMESPACE must include test, ci, or acceptance")
    if lowered in {"prod", "production", "neo4j", "default"}:
        raise SystemExit("ORCH_TEST_NAMESPACE must not name a production/default namespace")
    return raw


_NAMESPACE = _require_test_namespace()
_PFX = f"{_NAMESPACE}-unbind-bind-{uuid.uuid4().hex[:8]}"
SUP = f"{_PFX}-sup"
PEER = f"{_PFX}-peer"
PROJECT = f"{_PFX}-project"
PHASE = f"{PROJECT}::phase"
TASK = f"{PROJECT}::task"
os.environ["ORCH_SESSION_IDS"] = f"{SUP},{PEER}"

from fleet_orchestrator.config import OrchConfig, get_neo4j_driver  # noqa: E402
from fleet_orchestrator.dispatch import clear_current_task  # noqa: E402
from fleet_orchestrator.notify_state import redis_connect, state_key  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    create_phase,
    create_project,
    create_task,
    init_schema,
    update_task_status,
)
from fleet_orchestrator.tasks_api import app  # noqa: E402
from fleet_orchestrator.worker_liveness import (  # noqa: E402
    clear_worker_task_liveness,
    register_worker_task_liveness,
    worker_task_liveness_key,
)

CFG = OrchConfig()
CLIENT = TestClient(app)
R = redis_connect()
FAILURES: list[str] = []


def _check(label: str, cond: bool, detail: object = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {detail}"))
    if not cond:
        FAILURES.append(label)


def _cleanup() -> None:
    R.delete(state_key(PEER, "current_task"), state_key(PEER, "last_outcome"), worker_task_liveness_key(TASK))
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=_PFX)


def _task_row() -> dict:
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        row = session.run(
            """
            MATCH (t:OrchTask {id:$id})
            RETURN t.dispatched_to AS dispatched_to,
                   t.worker_liveness_worker AS worker_liveness_worker,
                   t.worker_liveness_started_at AS worker_liveness_started_at,
                   t.status AS status
            """,
            id=TASK,
        ).single()
    return dict(row) if row else {}


def main() -> int:
    init_schema(config=CFG)
    _cleanup()
    try:
        create_project(PROJECT, "unbind binding", supervisor=SUP, priority=10, config=CFG)
        create_phase(PROJECT, PHASE, "phase", config=CFG)
        create_task(PHASE, TASK, "bound work", owner=SUP, priority=1, config=CFG)
        update_task_status(TASK, "in_progress", owner=SUP, config=CFG)
        with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
            session.run(
                "MATCH (t:OrchTask {id:$id}) SET t.dispatched_to=$peer, t.updated_at=datetime()",
                id=TASK,
                peer=PEER,
            )
        started = time.time()
        register_worker_task_liveness(PEER, TASK, "bound work", SUP, started, config=CFG)
        R.set(
            state_key(PEER, "current_task"),
            f'{{"task_id":"{TASK}","description":"bound work","supervisor":"{SUP}","started_at":{started}}}',
        )
        R.set(state_key(PEER, "last_activity"), str(started + 1))

        before = CLIENT.get(f"/api/sessions/{PEER}/current").json()
        _check(
            "bound peer /current shows the task",
            (before.get("current") or {}).get("top_task_id") == TASK,
            before,
        )
        task_before = CLIENT.get(f"/api/tasks/{TASK}").json()
        _check("task still dispatched_to peer before unbind", task_before.get("dispatched_to") == PEER)

        cleared = clear_current_task(PEER)
        _check("clear_current_task returns previous_task_id", cleared.get("previous_task_id") == TASK, cleared)
        _check("Redis current_task cleared", R.get(state_key(PEER, "current_task")) in (None, b""))
        row = _task_row()
        _check(
            "Neo4j worker_liveness_worker cleared",
            row.get("worker_liveness_worker") in (None, ""),
            row,
        )
        _check(
            "durable dispatched_to preserved after unbind",
            row.get("dispatched_to") == PEER,
            row,
        )

        after = CLIENT.get(f"/api/sessions/{PEER}/current").json()
        _check(
            "unbound peer /current no longer reports live current work",
            after.get("current") is None,
            after,
        )
        # CLI-shaped binding view uses the same /current endpoint
        from fleet_orchestrator.cli_handoff_state import executor_bindings

        def api_call(method: str, endpoint: str, data=None):
            if method == "GET":
                return CLIENT.get(endpoint).json()
            raise AssertionError(method)

        bindings = executor_bindings(CLIENT.get(f"/api/tasks/{TASK}").json(), api_call)
        _check("executor_bindings empty after unbind", bindings == [], bindings)
    finally:
        clear_worker_task_liveness(TASK, config=CFG)
        _cleanup()

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - unbind clears Redis bind + Neo4j liveness; /current no longer shows WORKING.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
