#!/usr/bin/env python3
"""Acceptance: re-ingest reconciles DEPENDS_ON edges to the current plan.

Regression guard for #182: A plan originally had A depends-on B. After B was
renamed to C, re-ingest MERGE-added A->C but left the orphan A->B edge, so A
stayed blocked by stale dependency state. The invariant is stricter: for tasks
in the ingested plan, outgoing DEPENDS_ON edges must equal the current plan.
"""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
for candidate in (Path(ROOT) / ".env", Path.home() / "claude-code-fleet-orchestrator/.env"):
    if "ORCH_DOTENV" not in os.environ and candidate.is_file():
        os.environ["ORCH_DOTENV"] = str(candidate)
        break

PFX = f"reingest-deps-{uuid.uuid4().hex[:8]}"
SUPERVISOR = f"{PFX}-supervisor"
WORKER = f"{PFX}-worker"
PROJECT = f"{PFX}-project"
os.environ["ORCH_SESSION_IDS"] = f"{SUPERVISOR},{WORKER}"
from fleet_orchestrator.test_isolation import assert_acceptance_redis_isolated  # noqa: E402

assert_acceptance_redis_isolated()

from fleet_orchestrator.config import OrchConfig, get_neo4j_driver  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    add_dependency,
    create_task,
    get_session_next_ready,
    init_schema,
    update_task_status,
)
from fleet_orchestrator.plan_loader import load_plan_from_text  # noqa: E402

CFG = OrchConfig()
FAILURES: list[str] = []


def _check(label: str, cond: bool, detail: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {detail}"))
    if not cond:
        FAILURES.append(label)


def _task_id(bare: str) -> str:
    return f"{PROJECT}::{bare}"


def _plan(dep_id: str, depends_on: str) -> str:
    return f"""# Project: {PROJECT} - Reingest Dependency Reconcile
> verify dependency edge set reconciliation

## Phase: work - Work [order: 1]

### Task: {dep_id} - dependency task [owner: {WORKER}] [priority: 20]

### Task: a - downstream task [owner: {WORKER}] [priority: 10] [depends: {depends_on}]
"""


def _ingest(dep_id: str, depends_on: str) -> dict[str, object]:
    return load_plan_from_text(
        _plan(dep_id, depends_on),
        source_path=f"/tmp/{PROJECT}.md",
        source_kind="markdown",
        ingested_by="reingest-dependency-acceptance",
        supervisor=SUPERVISOR,
        priority=10,
        config=CFG,
    )


def _deps(task_id: str) -> list[str]:
    driver = get_neo4j_driver(CFG)
    with driver.session(database=CFG.neo4j_db) as session:
        row = session.run(
            """
            MATCH (t:OrchTask {id: $task_id})
            OPTIONAL MATCH (t)-[:DEPENDS_ON]->(dep:OrchTask)
            RETURN collect(dep.id) AS deps
            """,
            task_id=task_id,
        ).single()
    return sorted(dep_id for dep_id in (row["deps"] if row else []) if dep_id)


def _set_status(task_id: str, status: str) -> None:
    driver = get_neo4j_driver(CFG)
    with driver.session(database=CFG.neo4j_db) as session:
        session.run(
            "MATCH (t:OrchTask {id: $task_id}) SET t.status = $status, t.updated_at = datetime()",
            task_id=task_id,
            status=status,
        )


def _cleanup() -> None:
    driver = get_neo4j_driver(CFG)
    with driver.session(database=CFG.neo4j_db) as session:
        session.run(
            "MATCH (n) WHERE (n:OrchProject OR n:OrchPhase OR n:OrchTask) "
            "AND n.id STARTS WITH $prefix DETACH DELETE n",
            prefix=PFX,
        )


def main() -> int:
    init_schema(config=CFG)
    _cleanup()
    try:
        first = _ingest("b", "b")
        _check("initial ingest has no loader errors", first.get("errors") == [], first)
        _check("A initially depends only on B", _deps(_task_id("a")) == [_task_id("b")], _deps(_task_id("a")))
        manual_dep = _task_id("manual")
        create_task(
            phase_id=_task_id("work"),
            task_id=manual_dep,
            description="manual ad-hoc dependency",
            owner=WORKER,
            wake_owner_if_ready=False,
            config=CFG,
        )
        _check("manual dependency primitive creates edge", add_dependency(_task_id("a"), manual_dep, config=CFG), _deps(_task_id("a")))

        _set_status(_task_id("b"), "interrupted")
        second = _ingest("c", "c")
        _check("rename re-ingest has no loader errors", second.get("errors") == [], second)
        _check("rename re-ingest reports old B stale", _task_id("b") in second.get("stale_tasks", []), second)
        _check("A now depends on current plan C plus manual dep", _deps(_task_id("a")) == sorted([manual_dep, _task_id("c")]), _deps(_task_id("a")))

        update_task_status(
            _task_id("c"),
            "completed",
            completion_evidence={"production_observation": "reingest dependency reconcile acceptance"},
            config=CFG,
        )
        update_task_status(
            manual_dep,
            "completed",
            completion_evidence={"production_observation": "manual dependency preserved through reingest"},
            config=CFG,
        )
        ready = get_session_next_ready(WORKER, config=CFG)
        _check("A becomes dispatch-ready when C completes", ready and ready.get("task_id") == _task_id("a"), ready)

        if FAILURES:
            print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
            return 1
        print("\nPASS - re-ingest reconciles dependency edges after task rename.")
        return 0
    finally:
        _cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
