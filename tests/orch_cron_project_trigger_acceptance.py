#!/usr/bin/env python3
"""Acceptance: orch-cron project triggers dispatch next-ready without reset."""
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


PFX = f"{_require_test_namespace()}-cron-project-{uuid.uuid4().hex[:8]}"
os.environ["NOTIFY_KEY_PREFIX"] = PFX
os.environ["ORCH_SESSION_IDS"] = f"{PFX}-treasurer"

from fleet_orchestrator import cli_orch_cron as cron  # noqa: E402
import fleet_orchestrator.dispatch as dispatch_module  # noqa: E402
from fleet_orchestrator.config import OrchConfig, get_neo4j_driver  # noqa: E402
from fleet_orchestrator.notify_state import redis_connect as notify_redis_connect  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    _state_key,
    add_dependency,
    create_phase,
    create_project,
    create_task,
    get_task,
    init_schema,
    project_cycle_in_flight,
    update_task_status,
)


CFG = OrchConfig()
WORKER = f"{PFX}-treasurer"
PROJECT = f"{PFX}-project"
PHASE = f"{PROJECT}::phase"
ENTRY = f"{PROJECT}::step-0"
AWAITING = f"{PROJECT}::awaiting-review"
FOLLOW = f"{PROJECT}::step-1"
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


def _complete(task_id: str) -> None:
    update_task_status(
        task_id,
        "completed",
        completion_evidence={"production_observation": f"{task_id} completed fixture"},
        completed_by=WORKER,
        config=CFG,
    )


def _mark_recurring(*task_ids: str) -> None:
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        for task_id in task_ids:
            session.run("MATCH (t:OrchTask {id: $task_id}) SET t.recurring = true", task_id=task_id)


def _records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _write_registry(path: Path, trigger: dict) -> None:
    path.write_text(json.dumps({"triggers": [trigger]}, indent=2), encoding="utf-8")


def _project_status() -> str:
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        row = session.run(
            "MATCH (p:OrchProject {id:$project_id}) RETURN p.status AS status",
            project_id=PROJECT,
        ).single()
    return str(row["status"]) if row else ""


def _ok_run(args: list[str], **_kwargs: object) -> SimpleNamespace:
    if args and args[:3] == ["git", "rev-parse", "HEAD"]:
        return SimpleNamespace(returncode=0, stdout="acceptance-head\n", stderr="")
    return SimpleNamespace(returncode=0, stdout="", stderr="")


def main() -> int:
    _cleanup()
    tmp = Path(tempfile.mkdtemp(prefix="orch-cron-project-"))
    try:
        init_schema(config=CFG)
        create_project(
            PROJECT,
            "Recurring project cadence",
            supervisor=WORKER,
            ingested_by=WORKER,
            config=CFG,
        )
        create_phase(PROJECT, PHASE, "Phase", config=CFG)
        create_task(PHASE, ENTRY, "entry task", priority=1, owner=WORKER, wake_owner_if_ready=False, config=CFG)
        create_task(PHASE, AWAITING, "awaiting review", priority=10, owner=WORKER, wake_owner_if_ready=False, config=CFG)
        create_task(PHASE, FOLLOW, "follow task", priority=20, owner=WORKER, wake_owner_if_ready=False, config=CFG)
        add_dependency(FOLLOW, ENTRY, config=CFG)
        _mark_recurring(ENTRY, AWAITING, FOLLOW)
        _complete(ENTRY)
        update_task_status(
            AWAITING,
            "in_progress",
            owner=WORKER,
            blocked_on="AWAIT:external-signal:acceptance parked task",
            config=CFG,
        )

        now = datetime.now(ZoneInfo("UTC")).replace(second=0, microsecond=0)
        registry = tmp / "project-registry.json"
        state_file = tmp / "project-state.jsonl"
        trigger = {
            "id": "project-cycle",
            "session": WORKER,
            "supervisor": WORKER,
            "project": PROJECT,
            "description": "Run recurring project",
            "tz": "UTC",
            "minute": now.minute,
            "hours": [now.hour],
            "state_file": str(state_file),
            "enabled": True,
        }

        _write_registry(registry, trigger)
        dry_fires = cron.tick(str(registry), _redis(), dry_run=True, now_override=now)
        _check("project trigger dry-run does not count as fire", dry_fires == 0, dry_fires)
        _check("project trigger dry-run does not reset entry", get_task(ENTRY, config=CFG).get("status") == "completed", get_task(ENTRY, config=CFG))
        _check("project trigger dry-run does not change next task", get_task(FOLLOW, config=CFG).get("status") == "pending", get_task(FOLLOW, config=CFG))
        _check("project trigger dry-run writes no state", _records(state_file) == [], _records(state_file))
        cycle_state = project_cycle_in_flight(PROJECT, config=CFG)
        _check(
            "await-blocked task is counted separately from active cycle work",
            cycle_state.get("active_count") == 0 and cycle_state.get("awaiting_count") == 1,
            cycle_state,
        )

        with mock.patch.object(dispatch_module, "hook_installation_status", return_value=SimpleNamespace(ok=True, detail="hooked")), \
             mock.patch.object(dispatch_module.subprocess, "run", side_effect=_ok_run):
            fires = cron.tick(str(registry), _redis(), now_override=now)

        entry = get_task(ENTRY, config=CFG)
        awaiting = get_task(AWAITING, config=CFG)
        follow = get_task(FOLLOW, config=CFG)
        current_raw = _redis().get(_state_key(WORKER, "current_task"))
        current = json.loads(current_raw) if current_raw else {}
        records = _records(state_file)
        project_status = _project_status()
        _check("project trigger fires once", fires == 1, fires)
        _check("project remains open without reset", project_status in {"active", "in_progress"}, project_status)
        _check("project trigger preserves completed entry", entry.get("status") == "completed", entry)
        _check("project trigger preserves awaiting sibling hold", awaiting.get("blocked_on") == "AWAIT:external-signal:acceptance parked task", awaiting)
        _check("project trigger dispatches next pending task", follow.get("status") == "in_progress" and follow.get("dispatched_to") == WORKER, follow)
        _check("project trigger binds current_task", current.get("task_id") == FOLLOW, current)
        _check(
            "project trigger records project state",
            records
            and records[-1].get("trigger_mode") == "project"
            and records[-1].get("project") == PROJECT
            and records[-1].get("task_id") == FOLLOW
            and records[-1].get("result") == "dispatched",
            records,
        )

        _complete(FOLLOW)
        _redis().delete(_state_key(WORKER, "current_task"))
        trigger["id"] = "project-cycle-next"
        _write_registry(registry, trigger)
        with mock.patch.object(dispatch_module, "hook_installation_status", return_value=SimpleNamespace(ok=True, detail="hooked")), \
             mock.patch.object(dispatch_module.subprocess, "run", side_effect=_ok_run):
            next_fires = cron.tick(str(registry), _redis(), now_override=now)

        entry = get_task(ENTRY, config=CFG)
        follow = get_task(FOLLOW, config=CFG)
        current_raw = _redis().get(_state_key(WORKER, "current_task"))
        current = json.loads(current_raw) if current_raw else {}
        records = _records(state_file)
        _check("project trigger reclaims completed recurring entry after chain drains", next_fires == 1, next_fires)
        _check("completed recurring entry is in_progress", entry.get("status") == "in_progress" and entry.get("dispatched_to") == WORKER, entry)
        _check("project trigger does not reset completed downstream step", follow.get("status") == "completed", follow)
        _check("project trigger binds reclaimed entry", current.get("task_id") == ENTRY, current)
        _check(
            "project trigger records reclaimed recurring task",
            records
            and records[-1].get("trigger_mode") == "project"
            and records[-1].get("project") == PROJECT
            and records[-1].get("task_id") == ENTRY
            and records[-1].get("result") == "dispatched",
            records,
        )

        ambiguous = cron.fire_trigger(
            _redis(),
            {"id": "ambiguous-project", "session": WORKER, "project": PROJECT, "task_id": ENTRY, "enabled": True},
            now,
        )
        _check("project trigger remains mutually exclusive", ambiguous == "skipped:ambiguous_trigger", ambiguous)
    finally:
        _cleanup()

    if FAILURES:
        print(f"\nFAIL -- {len(FAILURES)}: {FAILURES}")
        return 1
    print("\nPASS -- orch-cron project triggers dispatch next-ready without reset.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
