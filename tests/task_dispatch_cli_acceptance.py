"""Acceptance: ``taey-task dispatch`` uses the canonical dispatch primitive.

The supervisor-facing CLI verb must not duplicate claim/bind logic. It fetches
the task body, calls ``fleet_orchestrator.dispatch.dispatch()``, and the existing
primitive claims the OrchTask, binds Redis ``current_task``, and wakes the peer.

Env: ORCH_NEO4J_URI, ORCH_NEO4J_DB, ORCH_REDIS_HOST/PORT,
     REDIS_HOST/PORT, ORCH_TEST_NAMESPACE (required; must include test/ci/acceptance).
"""
from __future__ import annotations

import importlib
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
            "ORCH_TEST_NAMESPACE is required so this acceptance cannot run against production Neo4j without an isolated namespace"
        )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{5,80}", raw):
        raise SystemExit("ORCH_TEST_NAMESPACE must be 6-80 chars of letters/digits/._:-")
    lowered = raw.lower()
    if not any(marker in lowered for marker in ("test", "ci", "acceptance")):
        raise SystemExit("ORCH_TEST_NAMESPACE must include test, ci, or acceptance")
    if lowered in {"prod", "production", "neo4j", "default"}:
        raise SystemExit("ORCH_TEST_NAMESPACE must not name a production/default namespace")
    return raw


_NAMESPACE = _require_test_namespace()
os.environ["NOTIFY_KEY_PREFIX"] = f"{_NAMESPACE}:task-dispatch-cli:{uuid.uuid4().hex[:8]}"

from fleet_orchestrator import dispatch as dispatch_module  # noqa: E402
from fleet_orchestrator.config import OrchConfig  # noqa: E402
from fleet_orchestrator.notify_state import redis_connect as notify_redis_connect  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    _raw_stop_decision,
    _state_key,
    create_phase,
    create_project,
    create_task,
    get_neo4j_driver,
    get_task,
    init_schema,
)
from fleet_orchestrator.tasks_api import app  # noqa: E402

CFG = OrchConfig()
_R = notify_redis_connect()
_PFX = f"{_NAMESPACE}-dispatch-cli-{uuid.uuid4().hex[:8]}"
_SUP = f"{_PFX}-sup"
_PEER = f"{_SUP}-codex"
_TASK = f"{_PFX}::peer-work"
_FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        _FAILURES.append(label)


def _load_taey_task_cli():
    return importlib.import_module("fleet_orchestrator.cli_taey_task")


def _cleanup() -> None:
    _R.delete(*[_state_key(_PEER, suffix) for suffix in ("current_task", "idle", "last_tool_activity", "last_outcome", "parent")])
    prefix = os.environ.get("NOTIFY_KEY_PREFIX", "taey")
    for pattern in (
        f"{prefix}:worker-task-liveness:{_PFX}*",
        f"{prefix}:worker-task-liveness-escalated:{_PFX}*",
    ):
        cursor = 0
        while True:
            cursor, keys = _R.scan(cursor=cursor, match=pattern, count=100)
            if keys:
                _R.delete(*keys)
            if cursor == 0:
                break
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=_PFX)


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


def _current_task() -> dict:
    raw = _R.get(_state_key(_PEER, "current_task"))
    return json.loads(raw) if raw else {}


def main() -> int:
    _cleanup()
    init_schema(config=CFG)
    client = TestClient(app)
    cli = _load_taey_task_cli()

    try:
        create_project(project_id=_PFX, name="dispatch cli acceptance", supervisor=_SUP, config=CFG)
        create_phase(project_id=_PFX, phase_id=f"{_PFX}::ph", name="dispatch", config=CFG)
        create_task(
            phase_id=f"{_PFX}::ph",
            task_id=_TASK,
            description="peer dispatch verb acceptance",
            owner=_PEER,
            priority=5,
            wake_owner_if_ready=False,
            config=CFG,
        )

        pending_decision = _raw_stop_decision(_SUP, config=CFG)
        _check(
            "before CLI dispatch, supervisor stop flags undispatched peer work",
            pending_decision.get("dispatch_to") == _PEER and pending_decision.get("task_id") == _TASK,
            pending_decision,
        )

        ok = SimpleNamespace(returncode=0, stdout="OK", stderr="")
        argv = ["taey-task", "dispatch", _TASK, _PEER]
        with mock.patch.object(cli, "api_call", side_effect=lambda method, endpoint, data=None: _api_call(client, method, endpoint, data)), \
             mock.patch.object(cli, "detect_from_node", return_value=_SUP), \
             mock.patch.object(dispatch_module, "hook_installation_status", return_value=SimpleNamespace(ok=True, detail="hooked")), \
             mock.patch.object(dispatch_module.subprocess, "run", return_value=ok) as notify_run, \
             mock.patch.object(sys, "argv", argv):
            cli.main()

        task = get_task(_TASK, config=CFG)
        cur = _current_task()
        notify_calls = [
            call
            for call in notify_run.call_args_list
            if call.args
            and isinstance(call.args[0], list)
            and call.args[0]
            and call.args[0][0] == "taey-notify"
        ]
        git_head_calls = [
            call
            for call in notify_run.call_args_list
            if call.args
            and isinstance(call.args[0], list)
            and call.args[0][:3] == ["git", "rev-parse", "HEAD"]
        ]
        _check("CLI dispatch invokes taey-notify once through dispatch()", len(notify_calls) == 1, notify_run.call_args_list)
        _check("CLI dispatch computes packet git head once", len(git_head_calls) == 1, notify_run.call_args_list)
        _check("dispatch claims task in Neo4j", task.get("status") == "in_progress", task)
        _check("dispatch records dispatched_to peer", task.get("dispatched_to") == _PEER, task)
        _check("dispatch binds Redis current_task", cur.get("task_id") == _TASK, cur)
        _check("current_task records supervisor", cur.get("supervisor") == _SUP, cur)

        _R.delete(_state_key(_PEER, "idle"))
        _R.delete(_state_key(_PEER, "last_outcome"))
        _R.set(_state_key(_PEER, "last_tool_activity"), str(time.time()))
        after_decision = _raw_stop_decision(_SUP, config=CFG)
        _check(
            "after CLI dispatch, stop-engine no longer flags this task as undispatched peer work",
            after_decision.get("dispatch_to") is None,
            after_decision,
        )
        _check(
            "after CLI dispatch, any remaining stop block is not the pre-dispatch peer handoff block",
            "undispatched" not in str(after_decision.get("reason") or ""),
            after_decision,
        )

        _R.set(_state_key(_PEER, "last_outcome"), json.dumps({"outcome": "error", "task_id": _TASK}))
        argv = ["taey-task", "unbind", _PEER]
        with mock.patch.object(cli, "api_call", side_effect=lambda method, endpoint, data=None: _api_call(client, method, endpoint, data)), \
             mock.patch.object(sys, "argv", argv):
            cli.main()
        _check("CLI unbind clears current_task", not _R.get(_state_key(_PEER, "current_task")), _current_task())
        _check("CLI unbind clears last_outcome", not _R.get(_state_key(_PEER, "last_outcome")), _R.get(_state_key(_PEER, "last_outcome")))
    finally:
        _cleanup()

    if _FAILURES:
        print(f"\nFAIL — {len(_FAILURES)}: {_FAILURES}")
        return 1
    print("\nPASS — taey-task dispatch claims, binds, wakes, and clears undispatched peer-work block.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
