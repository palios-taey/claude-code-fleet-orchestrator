#!/usr/bin/env python3
"""Acceptance: self-started peer tasks bind the parent supervisor.

Peer-owned work can start through ``taey-task update <id> in_progress`` rather
than the supervisor dispatch primitive. That path still has to bind
``current_task.supervisor`` to the parent supervisor so ``record_outcome`` wakes
CONTROL and the supervisor can persist completion evidence.

Env: ORCH_NEO4J_URI, ORCH_NEO4J_DB, ORCH_REDIS_HOST/PORT,
     REDIS_HOST/PORT, ORCH_TEST_NAMESPACE (required).
"""
from __future__ import annotations

import json
import os
import re
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _require_test_namespace() -> str:
    raw = (os.environ.get("ORCH_TEST_NAMESPACE") or "").strip()
    if not raw:
        raise SystemExit(
            "ORCH_TEST_NAMESPACE is required so this acceptance cannot run against production Neo4j"
        )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{5,80}", raw):
        raise SystemExit("ORCH_TEST_NAMESPACE must be 6-80 chars of letters/digits/._:-")
    lowered = raw.lower()
    if not any(marker in lowered for marker in ("test", "ci", "acceptance")):
        raise SystemExit("ORCH_TEST_NAMESPACE must include test, ci, or acceptance")
    if lowered in {"prod", "production", "neo4j", "default"}:
        raise SystemExit("ORCH_TEST_NAMESPACE must not name a production/default namespace")
    return raw


NAMESPACE = _require_test_namespace()
PFX = f"{NAMESPACE}-selfstart-{uuid.uuid4().hex[:8]}"
os.environ["NOTIFY_KEY_PREFIX"] = PFX

from fleet_orchestrator import dispatch as dispatch_module  # noqa: E402
from fleet_orchestrator.config import OrchConfig, get_neo4j_driver  # noqa: E402
from fleet_orchestrator.notify_state import redis_connect as notify_redis_connect  # noqa: E402
from fleet_orchestrator.notify_state import state_key  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    create_phase,
    create_project,
    create_task,
    get_task,
    init_schema,
)
from fleet_orchestrator.tasks_api import app  # noqa: E402


CFG = OrchConfig()
R = notify_redis_connect()
PROJECT = f"{PFX}-project"
PHASE = f"{PROJECT}::phase"
TASK = f"{PROJECT}::peer-task"
ROOT_TASK = f"{PROJECT}::root-task"
SUP = f"{PFX}-sup"
PEER = f"{SUP}-codex"
ROOT_SESSION = f"{PFX}-taey"
STALE_WORKER = f"{PFX}-job-seeker"
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _delete_matching(pattern: str) -> None:
    cursor = 0
    while True:
        cursor, keys = R.scan(cursor=cursor, match=pattern, count=100)
        if keys:
            R.delete(*keys)
        if cursor == 0:
            break


def _cleanup() -> None:
    _delete_matching(f"{PFX}:*")
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=PFX)


def _current_task(session_id: str = PEER) -> dict:
    raw = R.get(state_key(session_id, "current_task"))
    return json.loads(raw) if raw else {}


def _parent(session_id: str = PEER) -> str:
    return str(R.get(state_key(session_id, "parent")) or "")


def _task_liveness(task_id: str) -> dict:
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        row = session.run(
            """
            MATCH (t:OrchTask {id: $task_id})
            RETURN t.worker_liveness_worker AS worker,
                   t.worker_liveness_supervisor AS supervisor,
                   t.worker_liveness_started_at AS started_at,
                   t.worker_liveness_heartbeat_at AS heartbeat_at,
                   t.status AS status
            """,
            task_id=task_id,
        ).single()
    return dict(row) if row else {}


def _seed_stale_liveness(task_id: str) -> None:
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run(
            """
            MATCH (t:OrchTask {id: $task_id})
            SET t.worker_liveness_worker = $worker,
                t.worker_liveness_supervisor = $worker,
                t.worker_liveness_started_at = 1.0,
                t.worker_liveness_heartbeat_at = 1.0,
                t.worker_liveness_ttl_secs = 1
            """,
            task_id=task_id,
            worker=STALE_WORKER,
        )


def _is_response_ready_to_supervisor(args: list[str]) -> bool:
    return (
        len(args) >= 2
        and args[1] == SUP
        and "--from" in args
        and args[args.index("--from") + 1] == PEER
        and "--type" in args
        and args[args.index("--type") + 1] == "response_ready"
    )


def main() -> int:
    _cleanup()
    init_schema(config=CFG)
    client = TestClient(app)
    try:
        create_project(project_id=PROJECT, name="self-start supervisor binding", supervisor=SUP, config=CFG)
        create_phase(project_id=PROJECT, phase_id=PHASE, name="phase", config=CFG)
        create_task(
            phase_id=PHASE,
            task_id=TASK,
            description="peer self-start must bind parent supervisor",
            owner=PEER,
            priority=5,
            wake_owner_if_ready=False,
            config=CFG,
        )
        create_task(
            phase_id=PHASE,
            task_id=ROOT_TASK,
            description="root self-start must refresh stale liveness",
            owner=ROOT_SESSION,
            priority=6,
            wake_owner_if_ready=False,
            config=CFG,
        )

        R.set(state_key(PEER, "parent"), PEER)
        started = client.patch(f"/api/task/{TASK}", json={"status": "in_progress", "from": PEER})
        _check("peer self-start accepted", started.status_code == 200, started.text)

        task = get_task(TASK, config=CFG)
        current = _current_task()
        _check("self-start marks task in progress", task.get("status") == "in_progress", task)
        _check("self-start binds current task", current.get("task_id") == TASK, current)
        _check("self-start binds parent supervisor, not peer", current.get("supervisor") == SUP, current)
        _check("self-start rewrites poisoned parent key", _parent() == SUP, _parent())

        notify_calls: list[list[str]] = []
        ok = SimpleNamespace(returncode=0, stdout="OK", stderr="")

        def fake_run(args, **_kwargs):
            notify_calls.append(list(args))
            return ok

        with mock.patch.object(dispatch_module.subprocess, "run", side_effect=fake_run):
            dispatch_module.record_outcome(PEER, "done", "self-start finished")

        _check(
            "record_outcome wakes parent supervisor",
            any(_is_response_ready_to_supervisor(call) for call in notify_calls),
            notify_calls,
        )

        closed = client.patch(
            f"/api/task/{TASK}",
            json={
                "status": "completed",
                "from": SUP,
                "evidence": {"production_observation": "supervisor audited self-started peer task"},
            },
        )
        _check("supervisor can close self-started peer task", closed.status_code == 200, closed.text)
        completed = get_task(TASK, config=CFG)
        _check("supervisor closure persists completion evidence", bool(completed.get("completion_evidence")), completed)

        _seed_stale_liveness(ROOT_TASK)
        root_started = client.patch(
            f"/api/task/{ROOT_TASK}",
            json={"status": "in_progress", "from": ROOT_SESSION},
        )
        _check("root self-start accepted", root_started.status_code == 200, root_started.text)
        root_task = get_task(ROOT_TASK, config=CFG)
        root_current = _current_task(ROOT_SESSION)
        root_liveness = _task_liveness(ROOT_TASK)
        _check("root self-start marks task in progress", root_task.get("status") == "in_progress", root_task)
        _check("root self-start binds current task", root_current.get("task_id") == ROOT_TASK, root_current)
        _check("root self-start records self supervisor", root_current.get("supervisor") == ROOT_SESSION, root_current)
        _check("root self-start leaves parent key untouched", _parent(ROOT_SESSION) == "", _parent(ROOT_SESSION))
        _check("root self-start replaces stale liveness worker", root_liveness.get("worker") == ROOT_SESSION, root_liveness)
        _check("root self-start replaces stale liveness supervisor", root_liveness.get("supervisor") == ROOT_SESSION, root_liveness)
        _check("root self-start refreshes stale heartbeat", float(root_liveness.get("heartbeat_at") or 0.0) > 1.0, root_liveness)
    finally:
        _cleanup()

    if FAILURES:
        print(f"\nFAIL -- {len(FAILURES)}: {FAILURES}")
        return 1
    print("\nPASS -- self-started work binds current-task state and refreshes liveness.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
