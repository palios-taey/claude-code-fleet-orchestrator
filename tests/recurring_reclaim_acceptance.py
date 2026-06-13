#!/usr/bin/env python3
"""Acceptance: dispatch may re-claim completed recurring tasks only.

Recurring §13 cycle tasks reuse the same task id across discovery cycles. A
completed task may be claimed again only when the plan marked it recurring;
ordinary completed tasks and dependency-blocked recurring tasks still fail.
"""
from __future__ import annotations

import importlib
import os
import re
import stat
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet_orchestrator.config import OrchConfig, get_neo4j_driver  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    add_dependency,
    create_phase,
    create_project,
    create_task,
    get_task,
    init_schema,
    update_task_status,
)
from fleet_orchestrator.plan_loader import load_plan_from_text  # noqa: E402


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
PFX = f"{_require_test_namespace()}-recur-{uuid.uuid4().hex[:8]}"
PROJECT = f"{PFX}-project"
PHASE = f"{PROJECT}::phase"
WORKER = f"{PFX}-codex"
SUP = f"{PFX}-sup"
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _notify_stub(tmp: str) -> str:
    path = os.path.join(tmp, "taey-notify-ok")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("#!/bin/sh\nexit 0\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return path


def _cleanup() -> None:
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=PFX)
    try:
        importlib.import_module("fleet_orchestrator.dispatch")._redis_connect().delete(
            importlib.import_module("fleet_orchestrator.dispatch")._state_key(WORKER, "current_task")
        )
    except Exception:
        pass


def _mark_recurring(task_id: str) -> None:
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run("MATCH (t:OrchTask {id: $task_id}) SET t.recurring = true", task_id=task_id)


def _completed(task_id: str, observation: str = "cycle completed") -> None:
    update_task_status(
        task_id,
        "completed",
        completion_evidence={"production_observation": observation},
        completed_by=WORKER,
        config=CFG,
    )


def main() -> int:
    D = importlib.import_module("fleet_orchestrator.dispatch")
    tmp = tempfile.mkdtemp(prefix="reclaim-")
    os.environ["ORCH_NOTIFY_CLI"] = _notify_stub(tmp)
    _cleanup()
    try:
        init_schema(config=CFG)

        md = f"""# Project: {PROJECT} - Recurring Project

## Phase: phase - Phase

### Task: recur - Repeating cycle task [owner: {SUP}] [recurring: true]
### Task: tagrecur - Tag-marked repeating task [owner: {SUP}] [tags: recurring]
"""
        result = load_plan_from_text(
            md,
            source_path="",
            source_kind="markdown",
            ingested_by=SUP,
            supervisor=SUP,
            config=CFG,
        )
        _check("plan ingest accepts recurring markers", not result.get("errors"), result)
        recurring_task = f"{PROJECT}::recur"
        tagged_task = f"{PROJECT}::tagrecur"
        _completed(recurring_task, "first recurring cycle completed")
        _completed(tagged_task, "first tag recurring cycle completed")

        D.dispatch(WORKER, recurring_task, "next recurring cycle", supervisor=SUP)
        recur = get_task(recurring_task, config=CFG)
        _check("completed recurring task re-claims as in_progress", recur.get("status") == "in_progress", recur)
        _check("recurring re-claim increments counter", int(recur.get("reclaim_count") or 0) == 1, recur)

        D.dispatch(WORKER, tagged_task, "next tag recurring cycle", supervisor=SUP)
        tagged = get_task(tagged_task, config=CFG)
        _check("recurring tag also marks task re-claimable", tagged.get("status") == "in_progress", tagged)

        create_project(project_id=f"{PFX}-plain", name="plain", config=CFG)
        create_phase(project_id=f"{PFX}-plain", phase_id=f"{PFX}-plain::phase", name="phase", config=CFG)
        plain = f"{PFX}-plain::done"
        create_task(phase_id=f"{PFX}-plain::phase", task_id=plain, description="one shot", owner=SUP, wake_owner_if_ready=False, config=CFG)
        _completed(plain, "one-shot completed")
        try:
            D.dispatch(WORKER, plain, "bad one-shot re-claim", supervisor=SUP)
            _check("completed non-recurring task rejects re-claim", False, get_task(plain, config=CFG))
        except D.OrchTaskNotReady as exc:
            _check("completed non-recurring task rejects re-claim", "status=completed" in str(exc), str(exc))

        blocked = f"{PFX}-plain::blocked-recur"
        dep = f"{PFX}-plain::dep"
        create_task(phase_id=f"{PFX}-plain::phase", task_id=dep, description="dep", owner=SUP, wake_owner_if_ready=False, config=CFG)
        create_task(phase_id=f"{PFX}-plain::phase", task_id=blocked, description="blocked recur", owner=SUP, wake_owner_if_ready=False, config=CFG)
        _mark_recurring(blocked)
        _completed(blocked, "prior cycle completed")
        add_dependency(blocked, dep, config=CFG)
        try:
            D.dispatch(WORKER, blocked, "bad blocked re-claim", supervisor=SUP)
            _check("recurring completed task with unmet deps rejects re-claim", False, get_task(blocked, config=CFG))
        except D.OrchTaskNotReady as exc:
            _check("recurring completed task with unmet deps rejects re-claim", "incomplete_deps=1" in str(exc), str(exc))
    finally:
        os.environ.pop("ORCH_NOTIFY_CLI", None)
        _cleanup()

    if FAILURES:
        print(f"\nFAIL — {len(FAILURES)}: {FAILURES}")
        return 1
    print("\nPASS — recurring completed tasks re-claim; one-shot and dep-blocked tasks do not.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
