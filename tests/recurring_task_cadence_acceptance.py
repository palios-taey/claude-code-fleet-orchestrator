#!/usr/bin/env python3
"""Acceptance: orch-cron can cadence-dispatch recurring OrchTasks.

Wake-prompt cron entries only notify a session. Markdown ``[recurring: true]``
tasks also need a cadence driver that reclaims the completed task through the
normal dispatch/evidence lifecycle.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import uuid
from datetime import datetime
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


PFX = f"{_require_test_namespace()}-cron-recur-{uuid.uuid4().hex[:8]}"
os.environ["NOTIFY_KEY_PREFIX"] = PFX
os.environ["ORCH_SESSION_IDS"] = f"{PFX}-treasurer"

from fleet_orchestrator import cli_orch_cron as cron  # noqa: E402
import fleet_orchestrator.dispatch as dispatch_module  # noqa: E402
from fleet_orchestrator.config import OrchConfig, get_neo4j_driver  # noqa: E402
from fleet_orchestrator.notify_state import redis_connect as notify_redis_connect  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    _state_key,
    create_phase,
    create_project,
    create_task,
    get_task,
    init_schema,
    update_task_status,
)
from fleet_orchestrator.plan_loader import load_plan_from_text  # noqa: E402


CFG = OrchConfig()
PROJECT = f"{PFX}-project"
PHASE = f"{PROJECT}::phase"
WORKER = f"{PFX}-treasurer"
TASK = f"{PROJECT}::cycle"
PLAIN = f"{PROJECT}::plain"
FAILURES: list[str] = []


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


def _complete(task_id: str, observation: str) -> None:
    update_task_status(
        task_id,
        "completed",
        completion_evidence={"production_observation": observation},
        completed_by=WORKER,
        config=CFG,
    )


def _write_registry(path: Path, trigger: dict) -> None:
    path.write_text(json.dumps({"triggers": [trigger]}, indent=2), encoding="utf-8")


def _ok_run(args: list[str], **_kwargs: object) -> SimpleNamespace:
    if args and args[:3] == ["git", "rev-parse", "HEAD"]:
        return SimpleNamespace(returncode=0, stdout="acceptance-head\n", stderr="")
    return SimpleNamespace(returncode=0, stdout="", stderr="")


def main() -> int:
    _cleanup()
    tmp = Path(tempfile.mkdtemp(prefix="recurring-task-cadence-"))
    try:
        init_schema(config=CFG)
        md = f"""# Project: {PROJECT} - Recurring cadence

## Phase: phase - Phase

### Task: cycle - Careers recurring cycle [owner: {WORKER}] [recurring: true]
"""
        result = load_plan_from_text(
            md,
            source_path="",
            source_kind="markdown",
            ingested_by=WORKER,
            supervisor=WORKER,
            config=CFG,
        )
        _check("plan ingest accepted recurring task", not result.get("errors"), result)
        _complete(TASK, "first cycle completed with evidence")

        now = datetime.now(ZoneInfo("UTC")).replace(second=0, microsecond=0)
        registry = tmp / "recurring.json"
        state_file = tmp / "state.jsonl"
        _write_registry(registry, {
            "id": "careers-cycle",
            "session": WORKER,
            "supervisor": WORKER,
            "task_id": TASK,
            "description": "Run careers recurring cycle",
            "tz": "UTC",
            "minute": now.minute,
            "hours": [now.hour],
            "state_file": str(state_file),
            "enabled": True,
        })

        with mock.patch.object(dispatch_module, "hook_installation_status", return_value=SimpleNamespace(ok=True, detail="hooked")), \
             mock.patch.object(dispatch_module.subprocess, "run", side_effect=_ok_run):
            fires = cron.tick(str(registry), _redis(), now_override=now)

        task = get_task(TASK, config=CFG)
        current_raw = _redis().get(_state_key(WORKER, "current_task"))
        current = json.loads(current_raw) if current_raw else {}
        records = [json.loads(line) for line in state_file.read_text(encoding="utf-8").splitlines()]
        _check("cron task trigger fired once", fires == 1, fires)
        _check("completed recurring task was reclaimed", task.get("status") == "in_progress", task)
        _check("dispatch recorded concrete worker", task.get("dispatched_to") == WORKER, task)
        _check("dispatch bound current_task", current.get("task_id") == TASK, current)
        _check("state file records task trigger", records and records[-1].get("trigger_mode") == "task" and records[-1].get("task_id") == TASK, records)

        _redis().delete(_state_key(WORKER, "current_task"))
        create_project(project_id=f"{PFX}-plain-project", name="plain", config=CFG)
        create_phase(project_id=f"{PFX}-plain-project", phase_id=f"{PFX}-plain-project::phase", name="phase", config=CFG)
        create_task(
            phase_id=f"{PFX}-plain-project::phase",
            task_id=PLAIN,
            description="plain completed task",
            owner=WORKER,
            wake_owner_if_ready=False,
            config=CFG,
        )
        _complete(PLAIN, "one-shot done")
        _write_registry(registry, {
            "id": "plain-cycle",
            "session": WORKER,
            "supervisor": WORKER,
            "task_id": PLAIN,
            "description": "Should not reclaim",
            "tz": "UTC",
            "minute": now.minute,
            "hours": [now.hour],
            "enabled": True,
        })
        with mock.patch.object(dispatch_module, "hook_installation_status", return_value=SimpleNamespace(ok=True, detail="hooked")), \
             mock.patch.object(dispatch_module.subprocess, "run", side_effect=_ok_run):
            skipped = cron.tick(str(registry), _redis(), now_override=now)
        plain = get_task(PLAIN, config=CFG)
        _check("completed non-recurring task is not cadence-reclaimed", skipped == 0 and plain.get("status") == "completed", plain)
    finally:
        _cleanup()

    if FAILURES:
        print(f"\nFAIL -- {len(FAILURES)}: {FAILURES}")
        return 1
    print("\nPASS -- orch-cron task triggers cadence-dispatch completed recurring OrchTasks only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
