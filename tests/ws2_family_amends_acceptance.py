"""Ship-gate e2e — ws2 family amendments.

Covers:
  F18: plan_readiness wakes only tasks that get_session_next_ready would surface.
  Prefix isolation: wake/stuck Redis state respects NOTIFY_KEY_PREFIX.
  F11 amend: bind claim-lock write remains non-growing.

Env: ORCH_NEO4J_URI, ORCH_NEO4J_DB, ORCH_REDIS_HOST/PORT, ORCH_NOTIFY_LIB_ROOT.
"""
from __future__ import annotations

import importlib
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

dispatch_module = importlib.import_module("fleet_orchestrator.dispatch")  # noqa: E402
readiness_module = importlib.import_module("fleet_orchestrator.plan_readiness")  # noqa: E402
from fleet_orchestrator.config import OrchConfig  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    add_dependency,
    create_phase,
    create_project,
    create_task,
    get_neo4j_driver,
    get_session_next_ready,
    update_task_status,
)

CFG = OrchConfig()
PREFIX = f"ws2fam-{uuid.uuid4().hex[:8]}"
SUPERVISOR = f"{PREFIX}-sup"
PHASE = f"{PREFIX}::phase"
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _driver():
    return get_neo4j_driver(CFG)


def _clean_graph() -> None:
    with _driver().session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=PREFIX)


def _task_claim_lock(task_id: str) -> object:
    with _driver().session(database=CFG.neo4j_db) as session:
        row = session.run(
            "MATCH (t:OrchTask {id: $task_id}) RETURN t._claim_lock AS claim_lock",
            task_id=task_id,
        ).single()
    return row["claim_lock"] if row else None


def _make_dep_case(name: str, *, owner: str, status: str = "pending") -> tuple[str, str]:
    completed = f"{PREFIX}::{name}-completed"
    downstream = f"{PREFIX}::{name}-downstream"
    create_task(
        phase_id=PHASE,
        task_id=completed,
        description=f"{name} completed",
        owner=SUPERVISOR,
        wake_owner_if_ready=False,
        config=CFG,
    )
    create_task(
        phase_id=PHASE,
        task_id=downstream,
        description=f"{name} downstream",
        owner=owner,
        initial_status=status,
        wake_owner_if_ready=False,
        config=CFG,
    )
    add_dependency(downstream, completed, config=CFG)
    update_task_status(
        completed,
        "completed",
        completion_evidence={"production_observation": f"{name} dependency completed"},
        config=CFG,
    )
    return completed, downstream


def _exercise_plan_readiness_alignment() -> None:
    create_project(project_id=PREFIX, name=PREFIX, supervisor=SUPERVISOR, config=CFG)
    create_phase(project_id=PREFIX, phase_id=PHASE, name="family-amends", config=CFG)

    completed, downstream = _make_dep_case("owned", owner=SUPERVISOR)
    wake = readiness_module.check_readiness(SUPERVISOR, {"task_id": completed, "description": "owned"})
    next_ready = get_session_next_ready(SUPERVISOR, project_id=PREFIX, config=CFG)
    _check("F18 owned pending dependent wakes", bool(wake and downstream in wake), wake)
    _check(
        "F18 owned pending dependent matches next-ready",
        bool(next_ready) and next_ready["task_id"] == downstream,
        next_ready,
    )
    update_task_status(downstream, "completed",
                       completion_evidence={"production_observation": "owned case consumed"},
                       config=CFG)

    completed, downstream = _make_dep_case("unowned", owner="")
    wake = readiness_module.check_readiness(SUPERVISOR, {"task_id": completed, "description": "unowned"})
    next_ready = get_session_next_ready(SUPERVISOR, project_id=PREFIX, config=CFG)
    _check("F18 unowned dependent does not wake", wake is None, wake)
    _check("F18 unowned dependent is not next-ready for supervisor", next_ready is None, next_ready)
    update_task_status(downstream, "completed",
                       completion_evidence={"production_observation": "unowned case consumed"},
                       config=CFG)

    completed, downstream = _make_dep_case("blocked", owner=SUPERVISOR, status="blocked")
    wake = readiness_module.check_readiness(SUPERVISOR, {"task_id": completed, "description": "blocked"})
    next_ready = get_session_next_ready(SUPERVISOR, project_id=PREFIX, config=CFG)
    _check("F18 blocked dependent does not wake", wake is None, wake)
    _check("F18 blocked dependent is not next-ready", next_ready is None, next_ready)
    update_task_status(
        downstream,
        "failed",
        completion_evidence={"reason": "plan readiness failed downstream fixture"},
        config=CFG,
    )


def _exercise_prefix_isolation() -> None:
    old_prefix = os.environ.get("NOTIFY_KEY_PREFIX")
    custom_prefix = f"{PREFIX}:fleet"
    os.environ["NOTIFY_KEY_PREFIX"] = custom_prefix
    importlib.reload(dispatch_module)
    importlib.reload(readiness_module)
    redis_client = dispatch_module._redis_connect()
    worker = f"{PREFIX}-worker-codex"
    task_id = f"{PREFIX}::prefix-task"
    old_stuck_key = f"taey:orch-watch-stuck:{worker}:{task_id}"
    new_stuck_key = dispatch_module._state_key("orch-watch-stuck", f"{worker}:{task_id}")
    old_wake_key = f"taey:orch-wake-fired:{task_id}"
    new_wake_key = f"{custom_prefix}:orch-wake-fired:{task_id}"
    try:
        redis_client.set(old_stuck_key, "old")
        redis_client.set(new_stuck_key, "new")
        dispatch_module.bind_current_task(worker, task_id, "prefix isolation", supervisor=SUPERVISOR, set_parent=False)
        _check("prefix isolation leaves default stuck key untouched", redis_client.get(old_stuck_key) == "old")
        _check("prefix isolation clears custom stuck key", redis_client.get(new_stuck_key) is None)
        _check(
            "prefix isolation writes custom current_task key",
            bool(redis_client.get(dispatch_module._state_key(worker, "current_task"))),
        )

        redis_client.delete(old_wake_key, new_wake_key)
        owns_wake = readiness_module._dedup_wake(redis_client, task_id, ttl_sec=60)
        _check("prefix isolation dedup owns first custom wake", owns_wake)
        _check("prefix isolation does not write default wake key", redis_client.get(old_wake_key) is None)
        _check("prefix isolation writes custom wake key", redis_client.get(new_wake_key) == "1")
    finally:
        redis_client.delete(
            old_stuck_key,
            new_stuck_key,
            old_wake_key,
            new_wake_key,
            dispatch_module._state_key(worker, "current_task"),
            dispatch_module._state_key(worker, "last_outcome"),
        )
        if old_prefix is None:
            os.environ.pop("NOTIFY_KEY_PREFIX", None)
        else:
            os.environ["NOTIFY_KEY_PREFIX"] = old_prefix
        importlib.reload(dispatch_module)
        importlib.reload(readiness_module)


def _exercise_non_growing_bind_lock() -> None:
    task_id = f"{PREFIX}::bind-lock"
    create_task(
        phase_id=PHASE,
        task_id=task_id,
        description="bind lock",
        owner=SUPERVISOR,
        wake_owner_if_ready=False,
        config=CFG,
    )
    worker = f"{SUPERVISOR}-codex"
    dispatch_module.bind_current_task(worker, task_id, "bind lock", supervisor=SUPERVISOR, set_parent=False)
    first = _task_claim_lock(task_id)
    update_task_status(task_id, "pending", owner=SUPERVISOR, config=CFG)
    dispatch_module.bind_current_task(worker, task_id, "bind lock again", supervisor=SUPERVISOR, set_parent=False)
    second = _task_claim_lock(task_id)
    _check("F11 bind claim lock remains boolean true after repeated binds", first is True and second is True,
           {"first": first, "second": second})


def main() -> int:
    _clean_graph()
    try:
        _exercise_plan_readiness_alignment()
        _exercise_prefix_isolation()
        _exercise_non_growing_bind_lock()
    finally:
        _clean_graph()
    if FAILURES:
        print(f"\nFAIL — {len(FAILURES)}: {FAILURES}")
        return 1
    print("\nPASS — ws2 family amendments match readiness, prefix, and non-growing lock requirements.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
