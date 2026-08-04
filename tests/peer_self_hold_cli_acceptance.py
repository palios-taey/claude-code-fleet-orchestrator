"""Acceptance: a bound peer can set its own structured AWAIT hold by CLI.

Regression: peers outside the orchestrator import path could not call raw
``record_outcome`` helpers, and a blocked peer had no explicit self-hold CLI.
The installed ``taey-task`` surface must let the bound peer keep its task
``in_progress`` with a structured AWAIT marker, while unbound cross-owner
task writes still fail through the existing server guard.

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
    lowered = raw.lower()
    if not any(marker in lowered for marker in ("test", "ci", "acceptance")):
        raise SystemExit("ORCH_TEST_NAMESPACE must include test, ci, or acceptance")
    if lowered in {"prod", "production", "neo4j", "default"}:
        raise SystemExit("ORCH_TEST_NAMESPACE must not name a production/default namespace")
    return raw


_NAMESPACE = _require_test_namespace()
os.environ["NOTIFY_KEY_PREFIX"] = f"{_NAMESPACE}:peer-self-hold:{uuid.uuid4().hex[:8]}"

from fleet_orchestrator.config import OrchConfig, get_neo4j_driver  # noqa: E402
from fleet_orchestrator.notify_state import redis_connect as notify_redis_connect  # noqa: E402
from fleet_orchestrator.notify_state import state_key  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    _raw_stop_decision,
    create_phase,
    create_project,
    create_task,
    get_task,
    init_schema,
    update_task_status,
)
from fleet_orchestrator.tasks_api import app  # noqa: E402


CFG = OrchConfig()
R = notify_redis_connect()
PFX = f"{_NAMESPACE}-peer-self-hold-{uuid.uuid4().hex[:8]}"
SUP = f"{PFX}-sup"
PEER = f"{SUP}-codex"
PROJECT = f"{PFX}-project"
PHASE = f"{PROJECT}::phase"
TASK = f"{PROJECT}::bound-peer-task"
CROSS_OWNER = f"{PROJECT}::cross-owner-task"
MARKER = "AWAIT:external-signal:adapter-export"
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _cleanup() -> None:
    keys = [state_key(PEER, suffix) for suffix in ("current_task", "last_outcome", "idle", "last_tool_activity", "parent")]
    R.delete(*keys)
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=PFX)


def _set_dispatched(task_id: str, peer: str) -> None:
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run(
            "MATCH (t:OrchTask {id: $task_id}) SET t.dispatched_to = $peer",
            task_id=task_id,
            peer=peer,
        )


def _bind_current_task(peer: str, task_id: str) -> None:
    R.set(
        state_key(peer, "current_task"),
        json.dumps(
            {
                "task_id": task_id,
                "description": "bound peer task",
                "supervisor": SUP,
                "dispatcher": SUP,
                "started_at": time.time(),
            },
            separators=(",", ":"),
        ),
    )
    R.delete(state_key(peer, "idle"))
    R.set(state_key(peer, "last_tool_activity"), str(time.time()))


def _api_call(client: TestClient, method: str, endpoint: str, data=None) -> dict:
    if method == "GET":
        response = client.get(endpoint)
    elif method == "PATCH":
        response = client.patch(endpoint, json=data)
    else:
        raise AssertionError(f"unexpected CLI method {method}")
    if response.status_code >= 400:
        raise AssertionError(f"CLI API call failed HTTP {response.status_code}: {response.text}")
    return response.json()


def _patch(client: TestClient, task_id: str, body: dict) -> tuple[int, dict]:
    response = client.patch(f"/api/task/{task_id}", json=body)
    try:
        payload = response.json()
    except Exception:
        payload = {"text": response.text}
    return response.status_code, payload


def main() -> int:
    _cleanup()
    init_schema(config=CFG)
    client = TestClient(app)
    cli = importlib.import_module("fleet_orchestrator.cli_taey_task")
    try:
        create_project(project_id=PROJECT, name="peer self hold", supervisor=SUP, config=CFG)
        create_phase(project_id=PROJECT, phase_id=PHASE, name="hold", config=CFG)
        for task_id in (TASK, CROSS_OWNER):
            create_task(
                phase_id=PHASE,
                task_id=task_id,
                description=task_id,
                owner=SUP,
                priority=5,
                wake_owner_if_ready=False,
                config=CFG,
            )
            update_task_status(task_id, "in_progress", owner=SUP, config=CFG)
            _set_dispatched(task_id, PEER)

        _bind_current_task(PEER, TASK)
        argv = ["taey-task", "hold", TASK, MARKER]
        with mock.patch.object(cli, "api_call", side_effect=lambda method, endpoint, data=None: _api_call(client, method, endpoint, data)), \
             mock.patch.object(cli, "detect_from_node", return_value=PEER), \
             mock.patch.object(sys, "argv", argv):
            cli.main()

        held = get_task(TASK, config=CFG)
        _check("bound peer hold stays in_progress", held.get("status") == "in_progress", held)
        _check("bound peer hold records structured AWAIT", held.get("blocked_on") == MARKER, held)
        _check("bound peer hold preserves supervisor owner", held.get("owner") == SUP, held)

        stop_decision = _raw_stop_decision(PEER, config=CFG)
        _check(
            "peer stop treats structured AWAIT as clean hold",
            stop_decision.get("wake_type") == "ALLOW_STOP"
            and stop_decision.get("awaiting_signal", {}).get("kind") == "external-signal",
            stop_decision,
        )

        code, body = _patch(client, CROSS_OWNER, {"status": "in_progress", "from": PEER, "blocked_on": MARKER})
        cross = get_task(CROSS_OWNER, config=CFG)
        _check("unbound cross-owner hold rejects", code == 409 and body.get("ok") is False, body)
        _check("unbound cross-owner hold does not write marker", cross.get("blocked_on") in (None, ""), cross)

        for terminal_status, outcome in (
            ("completed", "done"),
            ("failed", "error"),
            ("interrupted", "interrupted"),
        ):
            code, body = _patch(
                client,
                TASK,
                {
                    "status": terminal_status,
                    "from": PEER,
                    "evidence": {"production_observation": f"acceptance probe for {terminal_status}"},
                },
            )
            refreshed = get_task(TASK, config=CFG)
            _check(
                f"bound peer PATCH {terminal_status} rejects terminal self-write",
                code == 409
                and body.get("ok") is False
                and f"taey-task outcome {outcome}" in str(body),
                body,
            )
            _check(
                f"bound peer PATCH {terminal_status} leaves task in_progress",
                refreshed.get("status") == "in_progress",
                refreshed,
            )

        with mock.patch.object(cli, "detect_from_node", return_value=PEER), \
             mock.patch("fleet_orchestrator.dispatch._notify_supervisor_response_ready", return_value=None), \
             mock.patch.object(sys, "argv", ["taey-task", "outcome", "done", "--details", "ready for supervisor review"]):
            cli.main()
        raw_outcome = R.get(state_key(PEER, "last_outcome"))
        current_task = R.get(state_key(PEER, "current_task"))
        outcome = json.loads(raw_outcome) if raw_outcome else {}
        _check("CLI outcome records done for current task", outcome.get("outcome") == "done" and outcome.get("task_id") == TASK, outcome)
        _check("CLI outcome clears current_task", not current_task, current_task)
    finally:
        _cleanup()

    if FAILURES:
        print(f"\nFAIL -- {len(FAILURES)} failures: {FAILURES}")
        return 1
    print("\nPASS -- peer self-hold CLI preserves ownership guard and stop AWAIT semantics.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
