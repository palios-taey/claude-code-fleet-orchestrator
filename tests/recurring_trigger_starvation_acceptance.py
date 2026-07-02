#!/usr/bin/env python3
"""Acceptance: recurring project triggers release stale idle in-progress blockers."""
from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo


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


PFX = f"{_require_test_namespace()}-trigger-starve-{uuid.uuid4().hex[:8]}"
os.environ["NOTIFY_KEY_PREFIX"] = PFX
os.environ["ORCH_SESSION_IDS"] = f"{PFX}-worker"

from fleet_orchestrator import cli_orch_cron as cron  # noqa: E402
from fleet_orchestrator.config import OrchConfig, get_neo4j_driver  # noqa: E402
from fleet_orchestrator.notify_state import redis_connect as notify_redis_connect  # noqa: E402
from fleet_orchestrator.notify_state import state_key  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    create_phase,
    create_project,
    create_task,
    get_task,
    init_schema,
    update_task_status,
)
from fleet_orchestrator.worker_liveness import worker_task_liveness_key  # noqa: E402


CFG = OrchConfig()
SUP = f"{PFX}-sup"
WORKER = f"{PFX}-worker"
FAILURES: list[str] = []


cron.TRIGGER_STARVATION_SKIP_THRESHOLD = 3
cron.TRIGGER_STARVATION_STALE_TOOL_SEC = 1


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _redis():
    return notify_redis_connect()


def _cleanup() -> None:
    r = _redis()
    cursor = 0
    while True:
        cursor, keys = r.scan(cursor=cursor, match=f"{PFX}:*", count=100)
        if keys:
            r.delete(*keys)
        if cursor == 0:
            break
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=PFX)


def _set_idle_stale(session: str, age_sec: float = 30.0) -> None:
    r = _redis()
    r.set(state_key(session, "idle"), "1")
    r.set(state_key(session, "last_tool_activity"), str(time.time() - age_sec))


def _set_idle_fresh(session: str) -> None:
    r = _redis()
    r.set(state_key(session, "idle"), "1")
    r.set(state_key(session, "last_tool_activity"), str(time.time()))


def _bind_current(task_id: str, session: str = WORKER) -> None:
    r = _redis()
    r.set(
        state_key(session, "current_task"),
        json.dumps({"task_id": task_id, "description": task_id, "supervisor": SUP}),
    )
    r.set(worker_task_liveness_key(task_id), json.dumps({"task_id": task_id, "worker": session}))


def _make_in_progress_project(label: str, *, owner: str = WORKER, blocked_on: str = "",
                              bind_current: bool = True) -> tuple[str, str]:
    project = f"{PFX}-{label}-project"
    phase = f"{project}::phase"
    task = f"{project}::task"
    create_project(project, project, supervisor=SUP, ingested_by=SUP, config=CFG)
    create_phase(project, phase, "Phase", config=CFG)
    create_task(phase, task, f"{label} task", owner=owner, priority=10, wake_owner_if_ready=False, config=CFG)
    update_task_status(
        task,
        "in_progress",
        owner=owner,
        blocked_on=blocked_on,
        config=CFG,
    )
    if bind_current:
        _bind_current(task, owner)
    return project, task


def _trigger(project: str, trigger_id: str, now: datetime) -> dict:
    hours = sorted({(now + timedelta(hours=offset)).hour for offset in range(8)})
    return {
        "id": trigger_id,
        "session": WORKER,
        "supervisor": SUP,
        "project": project,
        "description": f"Run {project}",
        "tz": "UTC",
        "minute": now.minute,
        "hours": hours,
        "enabled": True,
    }


def _fire_three(trigger: dict, now: datetime) -> list[str]:
    return [
        cron.fire_trigger(_redis(), trigger, now + timedelta(hours=offset))
        for offset in range(3)
    ]


def _ok_run(args: list[str], **_kwargs: object) -> SimpleNamespace:
    return SimpleNamespace(returncode=0, stdout="OK", stderr="")


def main() -> int:
    _cleanup()
    sent_notifications: list[list[str]] = []
    real_run = cron.subprocess.run

    def fake_run(args, **kwargs):
        if args and str(args[0]).endswith("taey-notify"):
            sent_notifications.append(list(args))
            return _ok_run(args)
        return real_run(args, **kwargs)

    try:
        init_schema(config=CFG)
        now = datetime.now(ZoneInfo("UTC")).replace(second=0, microsecond=0)

        project, task = _make_in_progress_project("cycle")
        _set_idle_stale(WORKER)
        with mock.patch.object(cron.subprocess, "run", side_effect=fake_run):
            results = _fire_three(_trigger(project, "cycle-trigger", now), now)
        released = get_task(task, config=CFG)
        current = _redis().get(state_key(WORKER, "current_task"))
        liveness = _redis().get(worker_task_liveness_key(task))
        _check("cycle_in_flight stays skipped during release fire", results == ["skipped:cycle_in_flight"] * 3, results)
        _check(
            "third stale idle cycle skip returns blocker to pending",
            released.get("status") == "pending"
            and released.get("needs_attention") is True
            and released.get("worker_liveness_escalation_reason") == "trigger-starvation",
            released,
        )
        _check("trigger-starvation clears matching current_task", not current, current)
        _check("trigger-starvation clears task-keyed liveness sidecar", not liveness, liveness)
        _check(
            "trigger-starvation notifies supervisor",
            len(sent_notifications) == 1
            and sent_notifications[0][1] == SUP
            and "TRIGGER_STARVATION_RELEASE" in sent_notifications[0][2],
            sent_notifications,
        )

        update_task_status(task, "in_progress", owner=WORKER, config=CFG)
        _bind_current(task)
        with mock.patch.object(cron.subprocess, "run", side_effect=fake_run):
            _fire_three(_trigger(project, "cycle-trigger-repeat", now + timedelta(hours=3)), now + timedelta(hours=3))
        repeat = get_task(task, config=CFG)
        _check("per-task release dedup prevents immediate thrash", repeat.get("status") == "in_progress", repeat)

        fresh_project, fresh_task = _make_in_progress_project("fresh")
        _set_idle_fresh(WORKER)
        with mock.patch.object(cron.subprocess, "run", side_effect=fake_run):
            fresh_results = _fire_three(_trigger(fresh_project, "fresh-trigger", now), now)
        fresh = get_task(fresh_task, config=CFG)
        _check("fresh tool activity is not released", fresh_results == ["skipped:cycle_in_flight"] * 3 and fresh.get("status") == "in_progress", fresh)

        other_owner = f"{PFX}-other-owner"
        other_project, other_task = _make_in_progress_project(
            "other-owner",
            owner=other_owner,
            bind_current=False,
        )
        _set_idle_stale(WORKER)
        with mock.patch.object(cron.subprocess, "run", side_effect=fake_run):
            other_results = _fire_three(_trigger(other_project, "other-owner-trigger", now), now)
        other = get_task(other_task, config=CFG)
        _check(
            "stale trigger session does not release a different owner's task",
            other_results == ["skipped:cycle_in_flight"] * 3 and other.get("status") == "in_progress",
            {"results": other_results, "task": other},
        )

        await_project, await_task = _make_in_progress_project(
            "await",
            blocked_on="AWAIT:external-signal:acceptance parked task",
        )
        _set_idle_stale(WORKER)
        with mock.patch.object(cron.subprocess, "run", side_effect=fake_run):
            await_results = _fire_three(_trigger(await_project, "await-trigger", now), now)
        awaiting = get_task(await_task, config=CFG)
        _check(
            "structured AWAIT hold is never released",
            await_results == ["skipped:no_ready_task"] * 3
            and awaiting.get("status") == "in_progress"
            and awaiting.get("blocked_on") == "AWAIT:external-signal:acceptance parked task",
            {"results": await_results, "task": awaiting},
        )

        no_ready_project, no_ready_task = _make_in_progress_project("no-ready")
        _set_idle_stale(WORKER)
        with mock.patch("fleet_orchestrator.orch_schema.project_cycle_in_flight", return_value={"active_count": 0}), \
             mock.patch.object(cron.subprocess, "run", side_effect=fake_run):
            no_ready_results = _fire_three(_trigger(no_ready_project, "no-ready-trigger", now), now)
        no_ready = get_task(no_ready_task, config=CFG)
        _check("no_ready_task starvation branch also releases", no_ready_results == ["skipped:no_ready_task"] * 3 and no_ready.get("status") == "pending", no_ready)
    finally:
        _cleanup()

    if FAILURES:
        print(f"\nFAIL -- {len(FAILURES)}: {FAILURES}")
        return 1
    print("\nPASS -- recurring trigger starvation releases only stale idle non-AWAIT blockers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
