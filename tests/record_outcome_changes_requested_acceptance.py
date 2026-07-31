"""Acceptance: audit-rejected peer work has a clean rework primitive.

Repro from issue #89 follow-on:
  - peer task is still in_progress after the peer reports done;
  - Stop-hook cleanup leaves no current_task binding;
  - normal dispatch refuses with ORCH_TASK_NOT_READY;
  - supervisor requests changes and the same peer is rebound+woken.

Env: ORCH_NEO4J_URI, ORCH_NEO4J_DB, ORCH_REDIS_HOST/PORT,
     REDIS_HOST/PORT, ORCH_TEST_NAMESPACE (required; must include test/ci/acceptance).
"""
from __future__ import annotations

import importlib
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
        raise SystemExit("ORCH_TEST_NAMESPACE is required for changes_requested acceptance")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{5,80}", raw):
        raise SystemExit("ORCH_TEST_NAMESPACE must be 6-80 chars of letters/digits/._:-")
    lowered = raw.lower()
    if not any(marker in lowered for marker in ("test", "ci", "acceptance")):
        raise SystemExit("ORCH_TEST_NAMESPACE must include test, ci, or acceptance")
    if lowered in {"prod", "production", "neo4j", "default"}:
        raise SystemExit("ORCH_TEST_NAMESPACE must not name a production/default namespace")
    return raw


NAMESPACE = _require_test_namespace()
os.environ["NOTIFY_KEY_PREFIX"] = f"{NAMESPACE}:changes-requested:{uuid.uuid4().hex[:8]}"

import fleet_orchestrator.dispatch as dispatch_module  # noqa: E402
from fleet_orchestrator.config import OrchConfig  # noqa: E402
from fleet_orchestrator.current_task_binding import clear_session_current_task  # noqa: E402
from fleet_orchestrator.dispatch import (  # noqa: E402
    OrchTaskNotReady,
    _redis_connect,
    _state_key,
    dispatch as dispatch_task,
    record_outcome,
)
from fleet_orchestrator.orch_schema import (  # noqa: E402
    create_phase,
    create_project,
    create_task,
    get_neo4j_driver,
    get_task,
    init_schema,
)
from fleet_orchestrator.tasks_api import app  # noqa: E402


CFG = OrchConfig()
R = _redis_connect()
PFX = f"{NAMESPACE}-changes-requested-{uuid.uuid4().hex[:8]}"
SUP = f"{PFX}-sup"
PEER = f"{SUP}-codex"
PROJECT = f"{PFX}-project"
PHASE = f"{PROJECT}::phase"
API_TASK = f"{PFX}::api-rework"
CLI_TASK = f"{PFX}::cli-rework"
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _cleanup() -> None:
    for suffix in ("current_task", "last_outcome", "parent", "idle", "last_tool_activity"):
        R.delete(_state_key(PEER, suffix))
    prefix = os.environ.get("NOTIFY_KEY_PREFIX", "taey")
    for pattern in (
        f"{prefix}:worker-task-liveness:{PFX}*",
        f"{prefix}:worker-task-liveness-escalated:{PFX}*",
    ):
        cursor = 0
        while True:
            cursor, keys = R.scan(cursor=cursor, match=pattern, count=100)
            if keys:
                R.delete(*keys)
            if cursor == 0:
                break
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=PFX)


def _current_task() -> dict:
    raw = R.get(_state_key(PEER, "current_task"))
    return json.loads(raw) if raw else {}


def _api_call(client: TestClient, method: str, endpoint: str, data=None):
    if method == "GET":
        response = client.get(endpoint)
    elif method == "PATCH":
        response = client.patch(endpoint, json=data)
    elif method == "POST":
        response = client.post(endpoint, json=data)
    elif method == "DELETE":
        response = client.delete(endpoint)
    else:
        raise AssertionError(f"unexpected CLI method {method}")
    if response.status_code >= 400:
        raise AssertionError(f"CLI API call failed HTTP {response.status_code}: {response.text}")
    return response.json()


def _notify_handoffs(calls: list[list[str]], task_id: str) -> list[list[str]]:
    return [call for call in calls if "--handoff" in call and "--dispatcher-task-id" in call and task_id in call]


def _seed_audit_rejected_task(task_id: str, description: str, calls: list[list[str]]) -> None:
    create_task(
        phase_id=PHASE,
        task_id=task_id,
        description=description,
        owner=PEER,
        priority=5,
        wake_owner_if_ready=False,
        config=CFG,
    )
    ok = SimpleNamespace(returncode=0, stdout="OK", stderr="")

    def fake_run(args, **_kwargs):
        calls.append(list(args))
        return ok

    with mock.patch.object(dispatch_module, "hook_installation_status", return_value=SimpleNamespace(ok=True, detail="hooked")), \
         mock.patch.object(dispatch_module.subprocess, "run", side_effect=fake_run):
        dispatch_task(PEER, task_id, description, supervisor=SUP)
        record_outcome(PEER, "done", "prior implementation ready for supervisor audit")

    clear_session_current_task(PEER, redis_client=R)
    task = get_task(task_id, config=CFG) or {}
    _check(f"{task_id}: seeded task remains in_progress awaiting gate", task.get("status") == "in_progress", task)
    _check(f"{task_id}: seeded task remembers dispatched peer", task.get("dispatched_to") == PEER, task)
    _check(f"{task_id}: Stop cleanup removed current_task", not _current_task(), _current_task())


def _assert_normal_dispatch_refuses(task_id: str, description: str) -> None:
    refused = ""
    with mock.patch.object(dispatch_module, "hook_installation_status", return_value=SimpleNamespace(ok=True, detail="hooked")):
        try:
            dispatch_task(PEER, task_id, description, supervisor=SUP)
        except OrchTaskNotReady as exc:
            refused = str(exc)
    _check("plain dispatch refuses audit-rejected in_progress task", "ORCH_TASK_NOT_READY" in refused, refused)


def _assert_rebound(task_id: str, reason: str, calls: list[list[str]]) -> None:
    task = get_task(task_id, config=CFG) or {}
    cur = _current_task()
    handoffs = _notify_handoffs(calls, task_id)
    _check(f"{task_id}: changes_requested leaves task in_progress", task.get("status") == "in_progress", task)
    _check(f"{task_id}: changes_requested re-dispatches same peer", task.get("dispatched_to") == PEER, task)
    _check(f"{task_id}: changes_requested records validator", task.get("last_changes_requested_by") == SUP, task)
    _check(f"{task_id}: changes_requested records reason", task.get("last_changes_requested_reason") == reason, task)
    _check(f"{task_id}: changes_requested binds current_task", cur.get("task_id") == task_id, cur)
    _check(f"{task_id}: current_task records supervisor", cur.get("supervisor") == SUP, cur)
    _check(f"{task_id}: changes_requested wakes peer", bool(handoffs), calls)
    _check(f"{task_id}: wake prompt carries rejection reason", any(reason in call[2] for call in handoffs), handoffs)


def main() -> int:
    _cleanup()
    init_schema(config=CFG)
    client = TestClient(app)
    cli = importlib.import_module("fleet_orchestrator.cli_taey_task")

    try:
        create_project(project_id=PROJECT, name="changes requested acceptance", supervisor=SUP, config=CFG)
        create_phase(project_id=PROJECT, phase_id=PHASE, name="changes requested", config=CFG)

        api_calls: list[list[str]] = []
        _seed_audit_rejected_task(API_TASK, "API rework acceptance", api_calls)
        _assert_normal_dispatch_refuses(API_TASK, "API rework acceptance")
        api_reason = "gate failed: expected receipt was missing"
        api_calls.clear()

        ok = SimpleNamespace(returncode=0, stdout="OK", stderr="")

        def fake_api_run(args, **_kwargs):
            api_calls.append(list(args))
            return ok

        with mock.patch.object(dispatch_module, "hook_installation_status", return_value=SimpleNamespace(ok=True, detail="hooked")), \
             mock.patch.object(dispatch_module.subprocess, "run", side_effect=fake_api_run):
            response = client.patch(
                f"/api/task/{API_TASK}",
                json={"record_outcome": "changes_requested", "from": SUP, "reason": api_reason},
            )

        payload = response.json()
        _check("API changes_requested succeeds", response.status_code == 200 and payload.get("ok") is True, payload)
        _check("API response reports in_progress", payload.get("status") == "in_progress", payload)
        _check("API response reports same peer", payload.get("dispatched_to") == PEER, payload)
        _assert_rebound(API_TASK, api_reason, api_calls)

        R.delete(_state_key(PEER, "current_task"))
        cli_calls: list[list[str]] = []
        _seed_audit_rejected_task(CLI_TASK, "CLI rework acceptance", cli_calls)
        _assert_normal_dispatch_refuses(CLI_TASK, "CLI rework acceptance")
        cli_reason = "CLI gate failed: output contradicted the spec"
        cli_calls.clear()

        def fake_cli_run(args, **_kwargs):
            cli_calls.append(list(args))
            return ok

        argv = ["taey-task", "update", CLI_TASK, "changes_requested", "--reason", cli_reason]
        with mock.patch.object(cli, "api_call", side_effect=lambda method, endpoint, data=None: _api_call(client, method, endpoint, data)), \
             mock.patch.object(cli, "detect_from_node", return_value=SUP), \
             mock.patch.object(dispatch_module, "hook_installation_status", return_value=SimpleNamespace(ok=True, detail="hooked")), \
             mock.patch.object(dispatch_module.subprocess, "run", side_effect=fake_cli_run), \
             mock.patch.object(sys, "argv", argv):
            cli.main()

        _assert_rebound(CLI_TASK, cli_reason, cli_calls)
    finally:
        _cleanup()

    if FAILURES:
        print(f"\nFAIL -- {len(FAILURES)}: {FAILURES}")
        return 1
    print("\nPASS -- changes_requested resets, rebinds, wakes, and preserves audit rejection provenance.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
