#!/usr/bin/env python3
"""Acceptance: peer done reports drive supervisor audit/closure.

Issue #154 root cause: a supervised peer could mark its own task completed,
which removed it from the supervisor stop-engine path before the supervisor
audited the result. The contract is now:

1. A supervised autonomous peer, including a cross-family worker, cannot PATCH
   its own task to completed.
2. The peer reports done with record_outcome().
3. The done report wakes the supervisor and leaves the task gateable.
4. The supervisor closes the task with evidence.
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
PFX = f"{NAMESPACE}-supdrive-{uuid.uuid4().hex[:8]}"
os.environ["NOTIFY_KEY_PREFIX"] = PFX

from fleet_orchestrator import dispatch as dispatch_module  # noqa: E402
from fleet_orchestrator import current_liveness  # noqa: E402
from fleet_orchestrator.config import OrchConfig, get_neo4j_driver  # noqa: E402
from fleet_orchestrator.notify_state import redis_connect as notify_redis_connect  # noqa: E402
from fleet_orchestrator.notify_state import state_key  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    create_phase,
    create_project,
    create_task,
    get_session_stop_decision,
    get_task,
    init_schema,
)
from fleet_orchestrator.tasks_api import app  # noqa: E402


CFG = OrchConfig()
R = notify_redis_connect()
PROJECT = f"{PFX}-project"
PHASE = f"{PROJECT}::phase"
TASK = f"{PROJECT}::peer-task"
SUP = f"{PFX}-sup"
PEER = f"{PFX}-specialist-gemini"
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _delete_matching(*patterns: str) -> None:
    for pattern in patterns:
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


def _last_outcome() -> dict:
    raw = R.get(state_key(PEER, "last_outcome"))
    return json.loads(raw) if raw else {}


def _create_fixture() -> None:
    init_schema(config=CFG)
    create_project(project_id=PROJECT, name="supervisor forward drive", supervisor=SUP, priority=1, config=CFG)
    create_phase(project_id=PROJECT, phase_id=PHASE, name="phase", config=CFG)
    create_task(
        phase_id=PHASE,
        task_id=TASK,
        description="supervised peer must report done, not self-close",
        owner=PEER,
        priority=5,
        wake_owner_if_ready=False,
        config=CFG,
    )
    ok = SimpleNamespace(returncode=0, stdout="OK", stderr="")
    with mock.patch.object(dispatch_module.subprocess, "run", return_value=ok):
        dispatch_module.dispatch(PEER, TASK, "supervisor forward-drive acceptance", supervisor=SUP)


def _response_ready_call(args: list[str]) -> bool:
    return (
        len(args) >= 2
        and args[1] == SUP
        and "--type" in args
        and args[args.index("--type") + 1] == "response_ready"
        and "--from" in args
        and args[args.index("--from") + 1] == PEER
    )


def main() -> int:
    _cleanup()
    client = TestClient(app)
    try:
        _create_fixture()

        rejected = client.patch(
            f"/api/task/{TASK}",
            json={
                "status": "completed",
                "from": PEER,
                "evidence": {"production_observation": "peer tried to self-complete"},
            },
        )
        _check("peer PATCH completed is rejected", rejected.status_code == 409, rejected.text)
        task = get_task(TASK, config=CFG)
        _check("rejected peer self-completion leaves task in_progress", task.get("status") == "in_progress", task)

        with mock.patch.object(
            current_liveness,
            "current_task_liveness",
            side_effect=RuntimeError("boom"),
        ):
            current_payload = client.get(f"/api/sessions/{PEER}/current").json()
        _check("task API current survives liveness failure", current_payload.get("current") is not None, current_payload)
        _check("task API current liveness degrades to None", current_payload.get("liveness") is None, current_payload)

        os.environ["ORCH_PUBLIC_SHOW_SESSIONS"] = f"{SUP},{PEER}"
        from fleet_orchestrator import public_readonly  # noqa: E402

        with mock.patch.object(
            current_liveness,
            "current_task_liveness",
            side_effect=RuntimeError("boom"),
        ):
            public_current = public_readonly._current_visible(PEER)
        _check("public current survives liveness failure", public_current.get("current") is not None, public_current)
        _check("public current liveness degrades to None", public_current.get("liveness") is None, public_current)

        R.set(state_key(PEER, "last_tool_activity"), str(time.time()))
        notify_calls: list[list[str]] = []
        ok = SimpleNamespace(returncode=0, stdout="OK", stderr="")

        def fake_run(args, **_kwargs):
            notify_calls.append(list(args))
            return ok

        with mock.patch.object(dispatch_module.subprocess, "run", side_effect=fake_run):
            dispatch_module.record_outcome(PEER, "done", "acceptance finished")

        outcome = _last_outcome()
        _check("record_outcome stores done", outcome.get("outcome") == "done", outcome)
        _check("record_outcome stores task_id with done", outcome.get("task_id") == TASK, outcome)
        _check("record_outcome details include task id for stop-engine matching", TASK in str(outcome.get("details") or ""), outcome)
        _check("done report sends supervisor response_ready wake", any(_response_ready_call(call) for call in notify_calls), notify_calls)

        R.delete(state_key(PEER, "current_task"))
        R.set(state_key(PEER, "idle"), "1")
        reported_done_decision = get_session_stop_decision(SUP, config=CFG)
        _check(
            "reported-done peer task blocks supervisor for audit",
            reported_done_decision.get("block") is True
            and reported_done_decision.get("wake_type") == "WAKE_WITH_QUEUE"
            and reported_done_decision.get("task_id") == TASK,
            reported_done_decision,
        )

        closed = client.patch(
            f"/api/task/{TASK}",
            json={
                "status": "completed",
                "from": SUP,
                "evidence": {"production_observation": "supervisor audited peer done report"},
            },
        )
        _check("supervisor can close peer task with evidence", closed.status_code == 200, closed.text)
        completed = get_task(TASK, config=CFG)
        _check("supervisor closure marks task completed", completed.get("status") == "completed", completed)
        after_close = get_session_stop_decision(SUP, config=CFG)
        _check("completed peer task no longer drives supervisor", after_close.get("wake_type") == "ALLOW_STOP", after_close)
    finally:
        _cleanup()

    if FAILURES:
        print(f"\nFAIL -- {len(FAILURES)}: {FAILURES}")
        return 1
    print("\nPASS -- peer done reports wake and gate supervisors until audited closure.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
