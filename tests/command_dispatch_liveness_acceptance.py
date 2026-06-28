#!/usr/bin/env python3
"""Acceptance: explicit session-notify dispatch uses canonical liveness.

Issue #195 remaining gap: the UI/API command path could send ``DISPATCH ...``
through plain ``taey-notify`` without binding current_task or registering
task-keyed worker liveness. A silent peer then had no record_outcome, no Stop
hook wake, and no liveness timeout wake for the supervisor.

Follow-up hardening: dispatch intent is explicit JSON payload state
(``dispatch: true`` + ``task_id``), not inferred from prose tokens.
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
        raise SystemExit("ORCH_TEST_NAMESPACE is required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{5,80}", raw):
        raise SystemExit("ORCH_TEST_NAMESPACE must be 6-80 chars of letters/digits/._:-")
    if not any(marker in raw.lower() for marker in ("test", "ci", "acceptance")):
        raise SystemExit("ORCH_TEST_NAMESPACE must include test, ci, or acceptance")
    return raw


_NAMESPACE = _require_test_namespace()
PFX = f"{_NAMESPACE}-cmdlive-{uuid.uuid4().hex[:8]}"
os.environ["NOTIFY_KEY_PREFIX"] = PFX
os.environ["ORCH_WORKER_TASK_LIVENESS"] = "1"
os.environ["ORCH_WORKER_TASK_LIVENESS_TTL_SEC"] = "1"

from fleet_orchestrator import dispatch as dispatch_module  # noqa: E402
from fleet_orchestrator import tasks_api  # noqa: E402
from fleet_orchestrator.config import OrchConfig, get_neo4j_driver  # noqa: E402
from fleet_orchestrator.dispatch import _state_key  # noqa: E402
from fleet_orchestrator.notify_state import redis_connect as notify_redis_connect  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    create_phase,
    create_project,
    create_task,
    get_task,
    init_schema,
    set_project_user_stop_conditions,
    update_task_status,
)
from fleet_orchestrator.worker_liveness import worker_task_liveness_key  # noqa: E402


CFG = OrchConfig()
SUP = f"{PFX}-sup"
PEER = f"{SUP}-codex"
PROJECT = f"{PFX}-project"
PHASE = f"{PROJECT}::phase"
TASK = f"{PFX}-task-command"
STOP_PROJECT = f"{PFX}-stop-project"
STOP_PHASE = f"{STOP_PROJECT}::phase"
SUP_TASK = f"{PFX}-supervisor-current"
PEER_TASK = f"{PFX}-peer-unconfirmed"
FAILURES: list[str] = []


def _redis():
    return notify_redis_connect()


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _cleanup() -> None:
    r = _redis()
    for pattern in (
        f"{PFX}:*",
        f"{PFX}:worker-task-liveness:{PFX}*",
        f"{PFX}:worker-task-liveness-escalated:{PFX}*",
    ):
        cursor = 0
        while True:
            cursor, keys = r.scan(cursor=cursor, match=pattern, count=100)
            if keys:
                r.delete(*keys)
            if cursor == 0:
                break
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=PFX)


def _setup() -> None:
    init_schema(config=CFG)
    create_project(project_id=PROJECT, name=PROJECT, supervisor=SUP, priority=1, config=CFG)
    create_phase(project_id=PROJECT, phase_id=PHASE, name="phase", config=CFG)
    create_task(
        phase_id=PHASE,
        task_id=TASK,
        description="command notify dispatch path",
        owner=PEER,
        priority=1,
        wake_owner_if_ready=False,
        config=CFG,
    )
    create_project(project_id=STOP_PROJECT, name=STOP_PROJECT, supervisor=SUP, priority=2, config=CFG)
    create_phase(project_id=STOP_PROJECT, phase_id=STOP_PHASE, name="phase", config=CFG)
    create_task(
        phase_id=STOP_PHASE,
        task_id=SUP_TASK,
        description="supervisor current task with stop condition",
        owner=SUP,
        priority=1,
        wake_owner_if_ready=False,
        config=CFG,
    )
    create_task(
        phase_id=STOP_PHASE,
        task_id=PEER_TASK,
        description="unconfirmed peer work",
        owner=PEER,
        priority=2,
        wake_owner_if_ready=False,
        config=CFG,
    )


def _run_liveness_once(sent: list[tuple[str, str]]) -> int:
    from fleet_orchestrator import cli_orch_watch as watch

    def fake_send(_r, target, body, **_kwargs):
        sent.append((target, body))
        return True

    with mock.patch.object(watch, "_send_wake", side_effect=fake_send):
        return watch._process_worker_liveness_expirations(
            _redis(),
            dedup_ttl_sec=1,
            task_id_prefix=PFX,
            project_id_prefix=PFX,
        )


def main() -> int:
    _cleanup()
    try:
        _setup()
        client = TestClient(tasks_api.app)
        calls: list[list[str]] = []

        def fake_notify(args, **_kwargs):
            calls.append(list(args))
            return SimpleNamespace(returncode=0, stdout="OK", stderr="")

        direct_calls: list[list[str]] = []
        with mock.patch.object(tasks_api, "_ensure_registered_session", return_value=None), \
             mock.patch.object(tasks_api.subprocess, "run", side_effect=lambda args, **_kw: direct_calls.append(list(args)) or SimpleNamespace(returncode=0, stdout="OK", stderr="")):
            prose_response = client.post(
                f"/api/sessions/{PEER}/notify",
                json={"type": "command", "message": f"DISPATCH {TASK} - prose mention", "from": SUP},
            )
        _check("prose DISPATCH mention remains plain notify", prose_response.status_code == 200 and prose_response.json().get("dispatch_registered") is None, prose_response.text)
        _check("prose DISPATCH mention does not bind current_task", _redis().get(_state_key(PEER, "current_task")) is None, _redis().get(_state_key(PEER, "current_task")))
        _check("prose DISPATCH mention calls direct taey-notify", direct_calls and "--type" in direct_calls[0] and direct_calls[0][direct_calls[0].index("--type") + 1] == "command", direct_calls)
        _check("direct session notify identifies operator sender", direct_calls and "--from" in direct_calls[0] and direct_calls[0][direct_calls[0].index("--from") + 1] == "operator-ui", direct_calls)
        _check("direct session notify teaches UI response path", direct_calls and f"POST /api/chat/{PEER} with role=assistant" in direct_calls[0][2], direct_calls)

        with mock.patch.object(tasks_api, "_ensure_registered_session", return_value=None), \
             mock.patch.object(dispatch_module.subprocess, "run", side_effect=fake_notify):
            response = client.post(
                f"/api/sessions/{PEER}/notify",
                json={"dispatch": True, "task_id": TASK, "message": "explicit dispatch path", "from": SUP},
            )

        _check("explicit command notify dispatch endpoint succeeds", response.status_code == 200 and response.json().get("dispatch_registered") is True, response.text)
        notify_calls = [call for call in calls if call and call[0] == "taey-notify"]
        _check("canonical dispatch call used handoff metadata", notify_calls and "--handoff" in notify_calls[0] and "--dispatcher-task-id" in notify_calls[0], calls)
        current_raw = _redis().get(_state_key(PEER, "current_task"))
        current = json.loads(current_raw) if current_raw else {}
        task = get_task(TASK, config=CFG)
        liveness_raw = _redis().get(worker_task_liveness_key(TASK))
        _check("command dispatch bound peer current_task", current.get("task_id") == TASK and current.get("supervisor") == SUP, current)
        _check(
            "command dispatch registered OrchTask liveness",
            task.get("status") == "in_progress"
            and task.get("dispatched_to") == PEER
            and task.get("worker_liveness_worker") == PEER
            and bool(liveness_raw),
            task,
        )

        time.sleep(1.2)
        sent: list[tuple[str, str]] = []
        count = _run_liveness_once(sent)
        stalled = get_task(TASK, config=CFG)
        _check(
            "silent command-dispatched peer requeues after worker-liveness TTL",
            count == 1 and stalled.get("status") == "pending" and stalled.get("needs_attention") is True,
            {"count": count, "task": stalled, "sent": sent},
        )
        _check("silent command-dispatched peer wakes supervisor", sent and sent[0][0] == SUP and TASK in sent[0][1], sent)

        update_task_status(SUP_TASK, "in_progress", owner=SUP, config=CFG)
        update_task_status(PEER_TASK, "in_progress", owner=PEER, config=CFG)
        set_project_user_stop_conditions(
            STOP_PROJECT,
            [{"label": "stop_when_all_ready_tasks_dispatched", "active": True}],
            created_by=SUP,
            config=CFG,
        )
        _redis().delete(_state_key(PEER, "current_task"))
        _redis().delete(_state_key(PEER, "last_tool_activity"))
        _redis().delete(_state_key(PEER, "last_outcome"))
        from fleet_orchestrator import cli_orch_watch as watch

        stop_wakes: list[tuple[str, str]] = []
        with mock.patch.object(watch, "_send_wake", side_effect=lambda _r, target, body, **_kw: stop_wakes.append((target, body)) or True):
            handled = watch._handle_user_stop_gate(_redis(), SUP, {"task_id": SUP_TASK})
        refreshed = get_task(SUP_TASK, config=CFG)
        _check(
            "stop_when_all_ready_tasks_dispatched does not suppress unconfirmed peer work",
            handled and (refreshed.get("blocked_on") or "") != "stop_when_all_ready_tasks_dispatched" and stop_wakes,
            {"task": refreshed, "wakes": stop_wakes},
        )
    finally:
        _cleanup()

    if FAILURES:
        print(f"\nFAIL -- {len(FAILURES)}: {FAILURES}")
        return 1
    print("\nPASS -- explicit command notify dispatch binds liveness and unconfirmed peer work prevents stop-condition suppression.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
