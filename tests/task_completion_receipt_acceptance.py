#!/usr/bin/env python3
"""Acceptance: PATCH-completed owner work writes a completion receipt.

The completion event is the proof for /clear boundary consumers. A Redis
current_task binding may already have been cleared by the terminal status
transition before record_outcome() runs, so the route must write the receipt
from the completed task_id + worker directly.
"""
from __future__ import annotations

import json
import os
import re
import sys
import uuid

from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from fleet_orchestrator.config import OrchConfig, get_neo4j_driver  # noqa: E402
from fleet_orchestrator.current_task_binding import clear_matching_current_task  # noqa: E402
from fleet_orchestrator.dispatch import _redis_connect, _state_key, bind_current_task  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    create_phase,
    create_project,
    create_task,
    get_task,
    init_schema,
)
from fleet_orchestrator.tasks_api import app  # noqa: E402


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
PFX = f"{_require_test_namespace()}-receipt-{uuid.uuid4().hex[:8]}"
SUP = f"{PFX}-sup"
WORKER = f"{PFX}-worker"
PROJECT = f"{PFX}-project"
PHASE = f"{PROJECT}::phase"
LIVE_BOUND = f"{PROJECT}::live-bound"
PRE_CLEARED = f"{PROJECT}::pre-cleared"
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _redis():
    return _redis_connect()


def _cleanup() -> None:
    for suffix in ("current_task", "last_outcome", "last_completion_receipt", "parent", "idle"):
        _redis().delete(_state_key(WORKER, suffix))
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=PFX)


def _setup() -> None:
    init_schema(config=CFG)
    create_project(project_id=PROJECT, name=PROJECT, supervisor=SUP, priority=1, config=CFG)
    create_phase(project_id=PROJECT, phase_id=PHASE, name="phase", config=CFG)
    for task_id in (LIVE_BOUND, PRE_CLEARED):
        create_task(
            phase_id=PHASE,
            task_id=task_id,
            description=task_id,
            owner=WORKER,
            wake_owner_if_ready=False,
            config=CFG,
        )


def _bind(task_id: str) -> None:
    bind_current_task(
        WORKER,
        task_id,
        task_id,
        supervisor=SUP,
        set_parent=False,
    )


def _current_task_id() -> str:
    raw = _redis().get(_state_key(WORKER, "current_task"))
    if not raw:
        return ""
    return str(json.loads(raw).get("task_id") or "")


def _receipt() -> dict:
    raw = _redis().get(_state_key(WORKER, "last_completion_receipt"))
    return json.loads(raw) if raw else {}


def _clear_receipt() -> None:
    _redis().delete(_state_key(WORKER, "last_completion_receipt"))


def _complete(client: TestClient, task_id: str):
    return client.patch(
        f"/api/task/{task_id}",
        json={
            "status": "completed",
            "from": WORKER,
            "evidence": {
                "production_observation": f"route completion receipt acceptance {task_id}",
            },
        },
    )


def main() -> int:
    _cleanup()
    try:
        _setup()
        client = TestClient(app)

        _bind(LIVE_BOUND)
        live_response = _complete(client, LIVE_BOUND)
        live_receipt = _receipt()
        live_task = get_task(LIVE_BOUND, config=CFG) or {}
        _check("live-bound PATCH completion succeeds", live_response.status_code == 200, live_response.text)
        _check("live-bound PATCH completion stores task completed", live_task.get("status") == "completed", live_task)
        _check("live-bound PATCH completion clears current_task", _current_task_id() == "", _current_task_id())
        _check(
            "live-bound PATCH completion writes receipt",
            live_receipt.get("outcome") == "done"
            and live_receipt.get("task_id") == LIVE_BOUND
            and live_receipt.get("worker") == WORKER,
            live_receipt,
        )
        _clear_receipt()

        _bind(PRE_CLEARED)
        cleared = clear_matching_current_task(
            WORKER,
            PRE_CLEARED,
            redis_client=_redis(),
            reason="receipt-acceptance-pre-clear",
        )
        _check("pre-cleared fixture clears current_task before PATCH", cleared and _current_task_id() == "", _current_task_id())
        pre_response = _complete(client, PRE_CLEARED)
        pre_receipt = _receipt()
        pre_task = get_task(PRE_CLEARED, config=CFG) or {}
        _check("pre-cleared PATCH completion succeeds", pre_response.status_code == 200, pre_response.text)
        _check("pre-cleared PATCH completion stores task completed", pre_task.get("status") == "completed", pre_task)
        _check("pre-cleared PATCH completion leaves current_task clear", _current_task_id() == "", _current_task_id())
        _check(
            "pre-cleared PATCH completion still writes receipt",
            pre_receipt.get("outcome") == "done"
            and pre_receipt.get("task_id") == PRE_CLEARED
            and pre_receipt.get("worker") == WORKER,
            pre_receipt,
        )
    finally:
        _cleanup()

    if FAILURES:
        print(f"\nFAIL -- {len(FAILURES)}: {FAILURES}")
        return 1
    print("\nPASS -- PATCH completed writes completion receipt even after current_task was already clear.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
