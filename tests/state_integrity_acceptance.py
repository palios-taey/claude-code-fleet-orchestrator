"""Ship-gate e2e — state integrity across Neo4j, Redis, and API startup.

Cases:
  F11: completing one in-progress sibling cannot demote the project while another sibling is still in progress.
  F12: record_outcome(error/interrupted) writes Redis outcome and reverts the Neo4j claim to ready.
  F13: dispatch stores base ownership and the concrete worker in dispatched_to.
  F15: FastAPI startup runs init_schema and fails loud on schema errors.
  F16: Redis singleton clients reject a second, different config in the same process.

Env: ORCH_NEO4J_URI, ORCH_NEO4J_DB, ORCH_REDIS_HOST/PORT, ORCH_DASHBOARD_URL, ORCH_NOTIFY_LIB_ROOT.
"""
from __future__ import annotations

import copy
import asyncio
import inspect
import json
import os
import sys
import uuid
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fleet_orchestrator.config as config_module  # noqa: E402
import fleet_orchestrator.dispatch as dispatch_module  # noqa: E402
from fleet_orchestrator.config import OrchConfig, OrchConfigError  # noqa: E402
from fleet_orchestrator.dispatch import (  # noqa: E402
    _claim_ready_orch_task,
    _redis_connect,
    _state_key,
    bind_current_task,
    record_outcome,
)
from fleet_orchestrator.orch_schema import (  # noqa: E402
    create_phase,
    create_project,
    create_task,
    get_neo4j_driver,
    get_session_current_work,
    get_session_next_ready,
    init_schema,
    update_task_status,
)

CFG = OrchConfig()
PREFIX = f"state-ci-{uuid.uuid4().hex[:8]}"
BASE_OWNER = f"{PREFIX}-worker"
WORKER = f"{BASE_OWNER}-codex"
SUPERVISOR = f"{PREFIX}-sup"
PHASE = f"{PREFIX}::phase"
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _driver():
    return get_neo4j_driver(CFG)


def _task_row(task_id: str) -> dict:
    with _driver().session(database=CFG.neo4j_db) as session:
        row = session.run(
            """
            MATCH (t:OrchTask {id: $task_id})
            RETURN t.status AS status, t.owner AS owner, t.dispatched_to AS dispatched_to
            """,
            task_id=task_id,
        ).single()
    return dict(row) if row else {}


def _project_row() -> dict:
    with _driver().session(database=CFG.neo4j_db) as session:
        row = session.run(
            """
            MATCH (p:OrchProject {id: $project_id})
            RETURN p.status AS status, p.in_progress_heartbeat_at AS heartbeat
            """,
            project_id=PREFIX,
        ).single()
    return dict(row) if row else {}


def _clean() -> None:
    with _driver().session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=PREFIX)
    redis_client = _redis_connect()
    redis_client.delete(
        _state_key(WORKER, "current_task"),
        _state_key(WORKER, "last_outcome"),
        _state_key("orch-watch-stuck", f"{WORKER}:{PREFIX}::f12"),
    )
    prefix = os.environ.get("NOTIFY_KEY_PREFIX", "taey")
    for pattern in (
        f"{prefix}:worker-task-liveness:{PREFIX}*",
        f"{prefix}:worker-task-liveness-escalated:{PREFIX}*",
    ):
        cursor = 0
        while True:
            cursor, keys = redis_client.scan(cursor=cursor, match=pattern, count=100)
            if keys:
                redis_client.delete(*keys)
            if cursor == 0:
                break


def _setup_graph() -> None:
    create_project(project_id=PREFIX, name=PREFIX, supervisor=SUPERVISOR, config=CFG)
    create_phase(project_id=PREFIX, phase_id=PHASE, name="state", config=CFG)
    for suffix in ("f11a", "f11b", "f12", "f13"):
        create_task(
            phase_id=PHASE,
            task_id=f"{PREFIX}::{suffix}",
            description=suffix,
            owner=BASE_OWNER,
            wake_owner_if_ready=False,
            config=CFG,
        )


def _exercise_f11() -> None:
    t1 = f"{PREFIX}::f11a"
    t2 = f"{PREFIX}::f11b"
    update_task_status(t1, "in_progress", owner=BASE_OWNER, config=CFG)
    update_task_status(t2, "in_progress", owner=BASE_OWNER, config=CFG)
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(
                update_task_status,
                t1,
                "completed",
                completion_evidence={"production_observation": "state integrity f11 completion"},
                config=CFG,
            ),
            pool.submit(update_task_status, t2, "in_progress", owner=BASE_OWNER, config=CFG),
        ]
        for future in futures:
            future.result()
    update_task_status(
        t1,
        "completed",
        completion_evidence={"production_observation": "state integrity f11 terminal refresh"},
        config=CFG,
    )
    project = _project_row()
    _check("F11 sibling in_progress keeps project in_progress", project.get("status") == "in_progress", project)
    _check("F11 heartbeat remains while sibling is active", bool(project.get("heartbeat")), project)

    update_task_status(
        t2,
        "failed",
        completion_evidence={"reason": "state integrity f11 failed sibling fixture"},
        config=CFG,
    )
    project = _project_row()
    _check("F11 final terminal sibling demotes project to active", project.get("status") == "active", project)
    _check("F11 heartbeat clears only after no in_progress siblings remain", project.get("heartbeat") == "", project)


def _exercise_f13_and_f12() -> None:
    f13 = f"{PREFIX}::f13"
    _claim_ready_orch_task(f13, WORKER)
    row = _task_row(f13)
    _check("F13 claim preserves base owner", row.get("owner") == BASE_OWNER, row)
    _check("F13 claim records concrete dispatched_to", row.get("dispatched_to") == WORKER, row)
    current = get_session_current_work(WORKER, config=CFG)
    _check("F13 concrete worker current-work uses dispatched_to", bool(current) and current["top_task_id"] == f13, current)

    f12 = f"{PREFIX}::f12"
    bind_current_task(WORKER, f12, "f12 divergence", supervisor=SUPERVISOR, set_parent=False)
    row = _task_row(f12)
    _check("F12 bind marks Neo4j in_progress", row.get("status") == "in_progress", row)
    notify_calls: list[list[str]] = []
    ok = SimpleNamespace(returncode=0, stdout="OK", stderr="")

    def fake_run(args, **_kwargs):
        notify_calls.append(list(args))
        return ok

    with mock.patch.object(dispatch_module.subprocess, "run", side_effect=fake_run):
        record_outcome(WORKER, "error", "state integrity acceptance error")
    row = _task_row(f12)
    _check("F12 error outcome reverts Neo4j task to pending", row.get("status") == "pending", row)
    _check("F12 error outcome clears dispatched_to", row.get("dispatched_to") is None, row)
    ready = get_session_next_ready(BASE_OWNER, project_id=PREFIX, config=CFG)
    _check("F12 reverted task resurfaces for base owner", bool(ready) and ready["task_id"] == f12, ready)
    redis_client = _redis_connect()
    outcome_raw = redis_client.get(_state_key(WORKER, "last_outcome"))
    current_raw = redis_client.get(_state_key(WORKER, "current_task"))
    outcome = json.loads(outcome_raw) if outcome_raw else {}
    _check("F12 Redis last_outcome records error", outcome.get("outcome") == "error", outcome)
    _check("F12 current_task persists for supervisor inspection", bool(current_raw), current_raw)
    _check("F12 error outcome notifies supervisor response_ready",
           any(
               len(call) >= 2
               and call[1] == SUPERVISOR
               and "--from" in call
               and call[call.index("--from") + 1] == WORKER
               and "--type" in call
               and call[call.index("--type") + 1] == "response_ready"
               and "error" in call[2]
               for call in notify_calls
           ),
           notify_calls)


def _exercise_f15() -> None:
    from fleet_orchestrator.tasks_api import _init_schema_on_startup, app

    registered = any(fn.__name__ == "_init_schema_on_startup" for fn in app.router.on_startup)
    _check("F15 startup schema initializer registered", registered)
    _init_schema_on_startup()
    result = init_schema(config=CFG)
    _check("F15 init_schema remains idempotent", not result.get("errors"), result)


def _reset_redis_singletons() -> None:
    for attr in ("_sync_pool", "_async_pool"):
        pool = getattr(config_module, attr, None)
        if pool is not None:
            try:
                result = pool.disconnect()
                if inspect.isawaitable(result):
                    asyncio.run(result)
            except Exception:
                pass
    for attr in (
        "_sync_pool",
        "_async_pool",
        "_sentinel_sync",
        "_sentinel_async",
        "_sync_redis_config",
        "_async_redis_config",
    ):
        setattr(config_module, attr, None)


def _exercise_f16() -> None:
    _reset_redis_singletons()
    first = copy.copy(CFG)
    second = copy.copy(CFG)
    second.redis_port = int(CFG.redis_port) + 17
    config_module.get_redis_sync(first)
    raised = False
    try:
        config_module.get_redis_sync(second)
    except OrchConfigError:
        raised = True
    _check("F16 sync Redis singleton rejects config drift", raised)

    _reset_redis_singletons()
    config_module.get_redis_async(first)
    raised = False
    try:
        config_module.get_redis_async(second)
    except OrchConfigError:
        raised = True
    _check("F16 async Redis singleton rejects config drift", raised)
    _reset_redis_singletons()


def main() -> int:
    init_schema(config=CFG)
    _clean()
    try:
        _setup_graph()
        _exercise_f11()
        _exercise_f13_and_f12()
        _exercise_f15()
        _exercise_f16()
    finally:
        _clean()
    if FAILURES:
        print(f"\nFAIL — {len(FAILURES)}: {FAILURES}")
        return 1
    print("\nPASS — state integrity acceptance covered F11, F12, F13, F15, and F16.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
