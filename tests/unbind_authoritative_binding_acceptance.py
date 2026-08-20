#!/usr/bin/env python3
"""Acceptance: unbind reconciles graph then clears Redis (fail-loud).

CONTROL (PR334 comment / task-48c4dbf4 / task-05821fc1):
  1. Graph reconcile is required and ordered before Redis clear (fail loud)
  2. Unbind restores dispatchable shape: pending, clear dispatched_to/liveness, preserve owner
  3. /current stays graph-truthful (no Redis masking gate)
  4. Identity-bound; explicit repair when Redis absent + stale dispatched_to

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
                   t.status AS status,
                   t.owner AS owner
            """,
            id=TASK,
        ).single()
    return dict(row) if row else {}


def _seed_bound() -> float:
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
    return started


def main() -> int:
    init_schema(config=CFG)
    _cleanup()
    try:
        create_project(PROJECT, "unbind binding", supervisor=SUP, priority=10, config=CFG)
        create_phase(PROJECT, PHASE, "phase", config=CFG)
        create_task(PHASE, TASK, "bound work", owner=SUP, priority=1, config=CFG)
        _seed_bound()

        before = CLIENT.get(f"/api/sessions/{PEER}/current").json()
        _check(
            "bound peer /current shows the task",
            (before.get("current") or {}).get("top_task_id") == TASK,
            before,
        )

        resp = CLIENT.delete(f"/api/sessions/{PEER}/current-task")
        body = resp.json()
        _check("DELETE unbind HTTP 200", resp.status_code == 200, body)
        _check("DELETE returns previous_task_id", body.get("previous_task_id") == TASK, body)
        _check("DELETE returns status=pending", body.get("status") == "pending", body)
        _check("DELETE returns dispatched_to null", body.get("dispatched_to") in (None, ""), body)
        _check("Redis current_task cleared", R.get(state_key(PEER, "current_task")) in (None, b""))
        row = _task_row()
        _check("Neo4j status pending after unbind", row.get("status") == "pending", row)
        _check("Neo4j owner preserved", row.get("owner") == SUP, row)
        _check("Neo4j dispatched_to cleared", row.get("dispatched_to") in (None, ""), row)
        _check("Neo4j worker_liveness_worker cleared", row.get("worker_liveness_worker") in (None, ""), row)

        after = CLIENT.get(f"/api/sessions/{PEER}/current").json()
        _check(
            "graph /current empty after reconcile (no Redis gate)",
            after.get("current") is None,
            after,
        )

        # Empty Redis without repair → fail loud (no silent arbitrary clears)
        missing = CLIENT.delete(f"/api/sessions/{PEER}/current-task")
        _check("DELETE without bind/task_id is 409", missing.status_code == 409, missing.json())

        # Explicit repair when Redis empty but stale graph claim remains
        _seed_bound()
        R.delete(state_key(PEER, "current_task"), state_key(PEER, "last_outcome"))
        repair_resp = CLIENT.delete(f"/api/sessions/{PEER}/current-task?task_id={TASK}")
        repaired = repair_resp.json()
        _check("DELETE repair with task_id is 200", repair_resp.status_code == 200, repaired)
        _check("DELETE repair flag set", repaired.get("repair") is True, repaired)
        _check("repair returns pending", repaired.get("status") == "pending", repaired)
        row2 = _task_row()
        _check("repair sets pending", row2.get("status") == "pending", row2)
        _check("repair clears dispatched_to", row2.get("dispatched_to") in (None, ""), row2)
        _check("repair preserves owner", row2.get("owner") == SUP, row2)

        # Adversarial: nonexistent task_id must fail loud (no Redis-only success)
        ghost = CLIENT.delete(f"/api/sessions/{PEER}/current-task?task_id={_PFX}-does-not-exist")
        _check("repair nonexistent task is 404", ghost.status_code == 404, ghost.json())

        # Adversarial: already-pending arbitrary owned task is nothing-to-repair
        other = f"{PROJECT}::other"
        create_task(PHASE, other, "already pending", owner=SUP, priority=2, config=CFG)
        pending_only = CLIENT.delete(f"/api/sessions/{PEER}/current-task?task_id={other}")
        _check(
            "repair already-pending task is 409",
            pending_only.status_code == 409,
            pending_only.json(),
        )
        # Ensure we did not invent a dispatched_to clear on an unbound pending task
        with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
            other_row = session.run(
                "MATCH (t:OrchTask {id:$id}) RETURN t.status AS status, t.dispatched_to AS dispatched_to, t.owner AS owner",
                id=other,
            ).single()
        _check("already-pending task left pending", (other_row or {}).get("status") == "pending", other_row)
        _check("already-pending owner preserved", (other_row or {}).get("owner") == SUP, other_row)

        # Adversarial: concurrent rebind after graph reconcile must not delete new sidecars
        from fleet_orchestrator.tasks_api import (
            _clear_repair_session_unbind_redis,
            _reconcile_session_unbind_graph,
        )

        _seed_bound()
        R.delete(state_key(PEER, "current_task"), state_key(PEER, "last_outcome"))
        graph = _reconcile_session_unbind_graph(PEER, TASK, None, config=CFG, repair=True)
        _check("adversarial reconcile changed live claim", graph.get("changed") is True, graph)
        # Simulate concurrent redispatch: new current_task + liveness/outcome sidecars appear
        rebind_started = time.time()
        R.set(
            state_key(PEER, "current_task"),
            f'{{"task_id":"{TASK}","description":"rebound","supervisor":"{SUP}","started_at":{rebind_started}}}',
        )
        R.set(
            worker_task_liveness_key(TASK),
            f'{{"task_id":"{TASK}","worker":"{PEER}","dispatch_started_at":{rebind_started}}}',
        )
        R.set(state_key(PEER, "last_outcome"), '{"outcome":"interrupted","details":"probe"}')
        raced = _clear_repair_session_unbind_redis(PEER, TASK)
        _check("repair CAS reports superseded on rebind", raced.get("superseded") is True, raced)
        _check(
            "repair CAS left new current_task intact",
            R.get(state_key(PEER, "current_task")) not in (None, b""),
            R.get(state_key(PEER, "current_task")),
        )
        _check(
            "repair CAS left new liveness sidecar intact",
            R.get(worker_task_liveness_key(TASK)) not in (None, b""),
            R.get(worker_task_liveness_key(TASK)),
        )
        _check(
            "repair CAS left last_outcome intact",
            R.get(state_key(PEER, "last_outcome")) not in (None, b""),
            R.get(state_key(PEER, "last_outcome")),
        )
    finally:
        clear_worker_task_liveness(TASK, config=CFG)
        _cleanup()

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print(
        "\nPASS - unbind reconciles graph to pending (clears dispatched_to/liveness), "
        "then Redis; /current stays graph-truthful; repair path is explicit."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
