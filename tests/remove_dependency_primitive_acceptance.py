#!/usr/bin/env python3
"""Acceptance: manual dependency edges can be removed without erasing plan gates."""
from __future__ import annotations

import contextlib
import io
import os
import sys
import uuid
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("ORCH_REDIS_HOST", "127.0.0.1")
os.environ.setdefault("ORCH_REDIS_PORT", "6379")

from fastapi.testclient import TestClient  # noqa: E402
from fleet_orchestrator import cli_taey_task  # noqa: E402
from fleet_orchestrator.config import OrchConfig, get_neo4j_driver  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    add_dependency,
    create_phase,
    create_project,
    create_task,
    init_schema,
)
from fleet_orchestrator.plan_loader import load_plan_from_text  # noqa: E402
from fleet_orchestrator.tasks_api import app  # noqa: E402

PFX = f"remove-dep-{uuid.uuid4().hex[:8]}"
SUPERVISOR = f"{PFX}-sup"
WORKER = f"{PFX}-worker"
PROJECT = f"{PFX}-project"
PHASE = f"{PROJECT}::phase"
os.environ["ORCH_SESSION_IDS"] = f"{SUPERVISOR},{WORKER}"
CFG = OrchConfig()
FAILURES: list[str] = []


def _check(label: str, cond: bool, detail: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {detail}"))
    if not cond:
        FAILURES.append(label)


def _task_id(bare: str) -> str:
    return f"{PROJECT}::{bare}"


def _cleanup() -> None:
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=PFX)


def _dep_rows(task_id: str) -> list[dict[str, object]]:
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        rows = session.run(
            """
            MATCH (:OrchTask {id: $task_id})-[r:DEPENDS_ON]->(dep:OrchTask)
            RETURN dep.id AS id,
                   coalesce(r.plan_dependency, false) AS plan_dependency,
                   coalesce(r.manual_dependency, false) AS manual_dependency,
                   r.dependency_source AS dependency_source
            ORDER BY dep.id
            """,
            task_id=task_id,
        ).data()
    return rows


def _create_base_tasks() -> None:
    create_project(PROJECT, "Remove dependency primitive", supervisor=SUPERVISOR, config=CFG)
    create_phase(PROJECT, PHASE, "Phase", config=CFG)
    for bare in ("a", "b", "c", "d"):
        create_task(
            PHASE,
            _task_id(bare),
            f"task {bare}",
            owner=WORKER,
            wake_owner_if_ready=False,
            config=CFG,
        )


def _load_plan() -> dict[str, object]:
    md = f"""# Project: {PROJECT} - Remove Dependency Plan
> plan-managed edge fixture

## Phase: phase - Phase

### Task: a - downstream task [owner: {WORKER}] [depends: b]

### Task: b - plan dependency [owner: {WORKER}]
"""
    return load_plan_from_text(
        md,
        source_path=f"/tmp/{PROJECT}.md",
        source_kind="markdown",
        ingested_by=SUPERVISOR,
        supervisor=SUPERVISOR,
        config=CFG,
    )


def main() -> int:
    init_schema(config=CFG)
    _cleanup()
    client = TestClient(app)
    try:
        _create_base_tasks()
        _check("add_dependency creates manual edge", add_dependency(_task_id("a"), _task_id("b"), config=CFG), _dep_rows(_task_id("a")))
        response = client.delete(f"/api/tasks/{_task_id('a')}/dependencies/{_task_id('b')}")
        _check("DELETE manual dependency returns ok", response.status_code == 200 and response.json().get("ok"), response.text)
        _check("manual dependency edge is gone", _dep_rows(_task_id("a")) == [], _dep_rows(_task_id("a")))

        _cleanup()
        result = _load_plan()
        _check("plan ingest creates plan-managed edge", result.get("errors") == [], result)
        rows = _dep_rows(_task_id("a"))
        _check("plan edge has plan provenance", rows and rows[0].get("plan_dependency") is True, rows)
        response = client.delete(f"/api/tasks/{_task_id('a')}/dependencies/{_task_id('b')}")
        _check("DELETE plan-only dependency is rejected", response.status_code == 409, response.text)
        _check("plan-only dependency remains", len(_dep_rows(_task_id("a"))) == 1, _dep_rows(_task_id("a")))

        _check("manual overlay on plan edge is accepted", add_dependency(_task_id("a"), _task_id("b"), config=CFG), _dep_rows(_task_id("a")))
        response = client.delete(f"/api/tasks/{_task_id('a')}/dependencies/{_task_id('b')}")
        rows = _dep_rows(_task_id("a"))
        _check("DELETE plan+manual edge returns ok", response.status_code == 200 and response.json().get("ok"), response.text)
        _check(
            "plan+manual delete preserves plan dependency only",
            len(rows) == 1 and rows[0].get("plan_dependency") is True and rows[0].get("manual_dependency") is False,
            rows,
        )

        create_task(
            PHASE,
            _task_id("c"),
            "manual cli dependency",
            owner=WORKER,
            wake_owner_if_ready=False,
            config=CFG,
        )
        _check("add_dependency creates second manual edge", add_dependency(_task_id("a"), _task_id("c"), config=CFG), _dep_rows(_task_id("a")))
        original_api_call = cli_taey_task.api_call
        try:
            cli_taey_task.api_call = lambda method, endpoint, data=None: client.request(method, endpoint, json=data).json()
            out = io.StringIO()
            with contextlib.redirect_stdout(out):
                cli_taey_task.cmd_remove_dependency(SimpleNamespace(task_id=_task_id("a"), depends_on_id=_task_id("c")))
            _check("CLI prints remove-dependency success", "OK: removed dependency" in out.getvalue(), out.getvalue())
            _check("CLI removed manual dependency", all(row.get("id") != _task_id("c") for row in _dep_rows(_task_id("a"))), _dep_rows(_task_id("a")))
        finally:
            cli_taey_task.api_call = original_api_call
    finally:
        _cleanup()

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - remove-dependency preserves plan gates and removes manual edges.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
