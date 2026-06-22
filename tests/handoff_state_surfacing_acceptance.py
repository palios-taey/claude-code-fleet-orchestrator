#!/usr/bin/env python3
"""Acceptance: handoff state is visible and cross-session binds are explicit."""
from __future__ import annotations

import contextlib
import importlib
import io
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
        raise SystemExit("ORCH_TEST_NAMESPACE is required for isolated acceptance state")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{5,80}", raw):
        raise SystemExit("ORCH_TEST_NAMESPACE must be 6-80 chars of letters/digits/._:-")
    lowered = raw.lower()
    if not any(marker in lowered for marker in ("test", "ci", "acceptance")):
        raise SystemExit("ORCH_TEST_NAMESPACE must include test, ci, or acceptance")
    return raw


_NAMESPACE = _require_test_namespace()
os.environ["NOTIFY_KEY_PREFIX"] = f"{_NAMESPACE}:handoff-state:{uuid.uuid4().hex[:8]}"

from fleet_orchestrator import dispatch as dispatch_module  # noqa: E402
from fleet_orchestrator.config import OrchConfig  # noqa: E402
from fleet_orchestrator.notify_state import redis_connect as notify_redis_connect  # noqa: E402
from fleet_orchestrator.notify_state import state_key  # noqa: E402
from fleet_orchestrator.orch_schema import create_phase, create_project, create_task, get_neo4j_driver, init_schema  # noqa: E402
from fleet_orchestrator.tasks_api import app  # noqa: E402

CFG = OrchConfig()
R = notify_redis_connect()
PFX = f"{_NAMESPACE}-handoff-state-{uuid.uuid4().hex[:8]}"
SUP = f"{PFX}-sup"
PEER_A = f"{SUP}-codex"
PEER_B = f"{SUP}-gemini"
TASK = f"{PFX}::work"
FAILURES: list[str] = []


def _check(label: str, condition: bool, extra: object = "") -> None:
    print(("  PASS " if condition else "  FAIL ") + label + ("" if condition else f" -> {extra}"))
    if not condition:
        FAILURES.append(label)


def _api_call(client: TestClient, method: str, endpoint: str, data=None):
    if method == "GET":
        response = client.get(endpoint)
    elif method == "PATCH":
        response = client.patch(endpoint, json=data)
    elif method == "POST":
        response = client.post(endpoint, json=data)
    else:
        raise AssertionError(f"unexpected method {method}")
    if response.status_code >= 400:
        raise AssertionError(f"CLI API call failed HTTP {response.status_code}: {response.text}")
    return response.json()


def _cleanup() -> None:
    for session in (PEER_A, PEER_B, SUP):
        for suffix in ("current_task", "idle", "last_activity", "last_outcome", "parent"):
            R.delete(state_key(session, suffix))
    prefix = os.environ.get("NOTIFY_KEY_PREFIX", "taey")
    for pattern in (f"{prefix}:worker-task-liveness:{PFX}*", f"{prefix}:worker-task-liveness-escalated:{PFX}*"):
        cursor = 0
        while True:
            cursor, keys = R.scan(cursor=cursor, match=pattern, count=100)
            if keys:
                R.delete(*keys)
            if cursor == 0:
                break
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=PFX)


def _seed_dispatched_task() -> None:
    create_project(project_id=PFX, name="handoff state", supervisor=SUP, config=CFG)
    create_phase(project_id=PFX, phase_id=f"{PFX}::phase", name="Main", config=CFG)
    create_task(
        phase_id=f"{PFX}::phase",
        task_id=TASK,
        description="surface executor binding",
        owner=PEER_A,
        priority=5,
        wake_owner_if_ready=False,
        config=CFG,
    )
    ok = SimpleNamespace(returncode=0, stdout="OK", stderr="")
    with mock.patch.object(dispatch_module.subprocess, "run", return_value=ok):
        dispatch_module.dispatch(PEER_A, TASK, "surface executor binding", supervisor=SUP)
    R.set(state_key(PEER_A, "last_activity"), str(time.time()))


def main() -> int:
    _cleanup()
    init_schema(config=CFG)
    client = TestClient(app)
    cli_task = importlib.import_module("fleet_orchestrator.cli_taey_task")
    cli_plan = importlib.import_module("fleet_orchestrator.cli_taey_plan")

    try:
        _seed_dispatched_task()

        with mock.patch.object(cli_task, "api_call", side_effect=lambda method, endpoint, data=None: _api_call(client, method, endpoint, data)):
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli_task.cmd_status(SimpleNamespace(task_id=TASK))
        status_output = out.getvalue()
        _check("taey-task status prints executor binding section", "Executor binding:" in status_output, status_output)
        _check("taey-task status names live executor session", PEER_A in status_output, status_output)
        _check("taey-task status shows WORKING state", "WORKING" in status_output, status_output)
        _check("taey-task status shows last-active summary", "active " in status_output, status_output)

        with mock.patch.object(cli_plan, "api_call", side_effect=lambda method, endpoint, data=None: _api_call(client, method, endpoint, data)):
            err = io.StringIO()
            try:
                with contextlib.redirect_stderr(err):
                    cli_plan.cmd_assign(SimpleNamespace(task_id=TASK, session=PEER_B, force=False))
                assign_code = 0
            except SystemExit as exc:
                assign_code = int(exc.code or 0)
        assign_error = err.getvalue()
        _check("taey-plan assign refuses cross-session live bind", assign_code == 1, assign_error)
        _check("assign refusal names conflicting executor", PEER_A in assign_error and "--force" in assign_error, assign_error)

        with mock.patch.object(cli_task, "api_call", side_effect=lambda method, endpoint, data=None: _api_call(client, method, endpoint, data)), \
             mock.patch.object(cli_task, "detect_from_node", return_value=SUP):
            err = io.StringIO()
            try:
                with contextlib.redirect_stderr(err):
                    cli_task.cmd_dispatch(SimpleNamespace(task_id=TASK, peer=PEER_B, priority="normal", force=False))
                dispatch_code = 0
            except SystemExit as exc:
                dispatch_code = int(exc.code or 0)
        dispatch_error = err.getvalue()
        _check("taey-task dispatch refuses cross-session live bind", dispatch_code == 1, dispatch_error)
        _check("dispatch refusal names conflicting executor before generic not-ready", PEER_A in dispatch_error and "ORCH_TASK_NOT_READY" not in dispatch_error, dispatch_error)
    finally:
        _cleanup()

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - handoff state surfaces executor binding and blocks silent double-bind.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
