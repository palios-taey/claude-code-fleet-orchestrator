#!/usr/bin/env python3
"""Acceptance: project stop conditions never become task.blocked_on.

Regression guard for task-a4b618ba: project-scope ``## User Stop Conditions``
labels are unresolvable as task ``blocked_on`` markers. The loader persists
them only on the project and re-ingest clears legacy task contamination.
"""
from __future__ import annotations

import os
import re
import sys
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
    if not any(marker in raw.lower() for marker in ("test", "ci", "acceptance")):
        raise SystemExit("ORCH_TEST_NAMESPACE must include test, ci, or acceptance")
    return raw


PFX = f"{_require_test_namespace()}-stop-scope-{uuid.uuid4().hex[:8]}"
os.environ["NOTIFY_KEY_PREFIX"] = PFX
os.environ.setdefault("ORCH_SESSION_IDS", "conductor")

from fleet_orchestrator import cli_orch_watch as watch  # noqa: E402
from fleet_orchestrator.config import OrchConfig, get_neo4j_driver, get_redis_sync  # noqa: E402
from fleet_orchestrator.notify_state import redis_connect as notify_redis_connect  # noqa: E402
from fleet_orchestrator.orch_schema import get_project_user_stop_conditions, get_task, init_schema  # noqa: E402
from fleet_orchestrator.tasks_api import app  # noqa: E402


CFG = OrchConfig()
PROJECT = f"{PFX}-runthrough"
TASK = f"{PROJECT}::chats-via-taeys-hands"
STOP_CONDITION = "stop_when_all_ready_tasks_dispatched"
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _delete_matching(r, pattern: str) -> None:
    cursor = 0
    while True:
        cursor, keys = r.scan(cursor=cursor, match=pattern, count=100)
        if keys:
            r.delete(*keys)
        if cursor == 0:
            break


def _cleanup() -> None:
    _delete_matching(get_redis_sync(CFG), f"{PFX}:*")
    _delete_matching(notify_redis_connect(), f"{PFX}:*")
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=PFX)


def _plan() -> str:
    return f"""# Project: {PROJECT} - Taey Platform Runthroughs
> exercises the user stop condition scope.

## Phase: chats - Chat lanes [order: 1]

### Task: chats-via-taeys-hands - Chat runthrough via Taey's Hands [priority: 45] [owner: tutor]
- execute the chat lane through the hands stack

## User Stop Conditions
- {STOP_CONDITION}
"""


def _ingest() -> dict[str, object]:
    response = TestClient(app).post(
        "/api/projects/load-md",
        json={
            "md_text": _plan(),
            "source_kind": "markdown",
            "ingested_by": "plan-stop-condition-scope-acceptance",
            "supervisor": "conductor",
        },
    )
    _check("load-md returns HTTP 200", response.status_code == 200, response.text)
    return response.json()


def _contaminate_task_blocked_on() -> None:
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run(
            """
            MATCH (t:OrchTask {id: $task_id})
            SET t.blocked_on = $blocked_on,
                t.updated_at = datetime()
            """,
            task_id=TASK,
            blocked_on=STOP_CONDITION,
        ).consume()


def _check_loader_scope() -> None:
    first = _ingest()
    _check("initial ingest reports no loader errors", first.get("errors") == [], first)
    saved_conditions = get_project_user_stop_conditions(PROJECT, config=CFG)
    _check(
        "project stores user stop condition",
        bool(saved_conditions) and saved_conditions[0].get("label") == STOP_CONDITION,
        saved_conditions,
    )
    _check("fresh task has no blocked_on", not get_task(TASK, config=CFG).get("blocked_on"), get_task(TASK, config=CFG))

    _contaminate_task_blocked_on()
    _check(
        "fixture reproduced contaminated task.blocked_on",
        get_task(TASK, config=CFG).get("blocked_on") == STOP_CONDITION,
        get_task(TASK, config=CFG),
    )
    second = _ingest()
    _check("re-ingest reports no loader errors", second.get("errors") == [], second)
    _check(
        "re-ingest clears stop-condition blocked_on contamination",
        not get_task(TASK, config=CFG).get("blocked_on"),
        get_task(TASK, config=CFG),
    )
    still_saved = get_project_user_stop_conditions(PROJECT, config=CFG)
    _check(
        "re-ingest preserves project user_stop_conditions",
        bool(still_saved) and still_saved[0].get("label") == STOP_CONDITION,
        still_saved,
    )


def _check_stop_gate_does_not_write_blocked_on() -> None:
    def fail_update(*_: object, **__: object) -> None:
        raise AssertionError("project stop condition must not be written to task.blocked_on")

    try:
        with mock.patch.object(
            watch,
            "_load_task_state",
            return_value={"id": TASK, "status": "in_progress", "owner": "conductor", "blocked_on": ""},
        ), mock.patch.object(
            watch,
            "_task_project_context",
            return_value={"project_id": PROJECT, "user_stop_conditions": [STOP_CONDITION]},
        ), mock.patch.object(
            watch,
            "_evaluate_user_stop_conditions",
            return_value=(STOP_CONDITION, None),
        ), mock.patch(
            "fleet_orchestrator.orch_schema.update_task_status",
            side_effect=fail_update,
        ):
            handled = watch._handle_user_stop_gate(object(), "conductor", {"task_id": TASK})
    except AssertionError as exc:
        _check("stop-gate does not persist stop condition as blocked_on", False, exc)
    else:
        _check("stop-gate handles matched condition without blocked_on write", handled is True, handled)


def main() -> int:
    _cleanup()
    try:
        init_schema(config=CFG)
        _check_loader_scope()
        _check_stop_gate_does_not_write_blocked_on()
    finally:
        _cleanup()

    if FAILURES:
        print(f"\nFAIL -- {len(FAILURES)}: {FAILURES}")
        return 1
    print("\nPASS -- user stop conditions remain project-scoped and never task.blocked_on.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
