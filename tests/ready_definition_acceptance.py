"""Ship-gate e2e — F18 canonical dependency-satisfied predicate is behavior-preserving.

The refactor moves the shared dependency predicate into one Cypher fragment.
This acceptance builds a fixture and compares the current implementation with
the pre-refactor inline queries for:
  - _ZERO_DEP_READY_CYPHER single-task readiness
  - get_ready_tasks broad pending-ready listing
  - get_session_next_ready owner/project-scoped top ready
  - get_project_ready_tasks project-scoped ready list

Env: ORCH_NEO4J_URI, ORCH_NEO4J_DB, ORCH_REDIS_HOST/PORT, ORCH_DASHBOARD_URL.
"""
from __future__ import annotations

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet_orchestrator.config import OrchConfig, get_neo4j_driver  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    _ZERO_DEP_READY_CYPHER,
    add_dependency,
    create_phase,
    create_project,
    create_task,
    get_project_ready_tasks,
    get_ready_tasks,
    get_session_next_ready,
    init_schema,
    update_task_status,
)

CFG = OrchConfig()
PREFIX = f"readydef-ci-{uuid.uuid4().hex[:8]}"
OWNER = f"{PREFIX}-owner"
OTHER_OWNER = f"{PREFIX}-other"
ACTIVE_PROJECT = f"{PREFIX}-active"
STOPPED_PROJECT = f"{PREFIX}-stopped"
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _driver():
    return get_neo4j_driver(CFG)


def _cleanup() -> None:
    with _driver().session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=PREFIX)


def _task(project: str, name: str) -> str:
    return f"{project}::{name}"


def _setup() -> None:
    create_project(project_id=ACTIVE_PROJECT, name=ACTIVE_PROJECT, supervisor=OWNER, priority=1, config=CFG)
    create_phase(project_id=ACTIVE_PROJECT, phase_id=f"{ACTIVE_PROJECT}::phase", name="active", config=CFG)
    create_project(project_id=STOPPED_PROJECT, name=STOPPED_PROJECT, supervisor=OWNER, priority=2, config=CFG)
    create_phase(project_id=STOPPED_PROJECT, phase_id=f"{STOPPED_PROJECT}::phase", name="stopped", config=CFG)
    with _driver().session(database=CFG.neo4j_db) as session:
        session.run("MATCH (p:OrchProject {id: $id}) SET p.status = 'stopped'", id=STOPPED_PROJECT)

    specs = [
        (ACTIVE_PROJECT, "zero", OWNER, 10),
        (ACTIVE_PROJECT, "dep_done", OWNER, 20),
        (ACTIVE_PROJECT, "dep_open", OWNER, 30),
        (ACTIVE_PROJECT, "blocked", OWNER, 5),
        (ACTIVE_PROJECT, "other_owner", OTHER_OWNER, 1),
        (ACTIVE_PROJECT, "completed_dep", OWNER, 99),
        (ACTIVE_PROJECT, "open_dep", OWNER, 100),
        (STOPPED_PROJECT, "stopped_ready", OWNER, 1),
    ]
    for project, name, owner, priority in specs:
        create_task(
            phase_id=f"{project}::phase",
            task_id=_task(project, name),
            description=name,
            priority=priority,
            owner=owner,
            wake_owner_if_ready=False,
            config=CFG,
        )

    update_task_status(
        _task(ACTIVE_PROJECT, "completed_dep"),
        "completed",
        completion_evidence={"production_observation": "ready definition fixture completed dependency"},
        config=CFG,
    )
    add_dependency(_task(ACTIVE_PROJECT, "dep_done"), _task(ACTIVE_PROJECT, "completed_dep"), config=CFG)
    add_dependency(_task(ACTIVE_PROJECT, "dep_open"), _task(ACTIVE_PROJECT, "open_dep"), config=CFG)
    with _driver().session(database=CFG.neo4j_db) as session:
        session.run(
            "MATCH (t:OrchTask {id: $id}) SET t.blocked_on = 'human-gate'",
            id=_task(ACTIVE_PROJECT, "blocked"),
        )


def _legacy_zero(task_id: str) -> dict | None:
    with _driver().session(database=CFG.neo4j_db) as session:
        row = session.run(
            """
            MATCH (t:OrchTask {id: $task_id})
            WHERE coalesce(t.owner, '') <> ''
              AND NOT EXISTS {
                  MATCH (t)-[:DEPENDS_ON]->(:OrchTask)
              }
            RETURN t.id AS task_id,
                   t.owner AS owner,
                   t.description AS description
            """,
            task_id=task_id,
        ).single()
    return dict(row) if row else None


def _current_zero(task_id: str) -> dict | None:
    with _driver().session(database=CFG.neo4j_db) as session:
        row = session.run(_ZERO_DEP_READY_CYPHER, task_id=task_id).single()
    return dict(row) if row else None


def _legacy_ready_ids() -> list[str]:
    with _driver().session(database=CFG.neo4j_db) as session:
        rows = session.run(
            """
            MATCH (t:OrchTask {status: 'pending'})
            WHERE t.id STARTS WITH $prefix
              AND NOT EXISTS {
                  MATCH (t)-[:DEPENDS_ON]->(dep:OrchTask)
                  WHERE dep.status <> 'completed'
              }
            RETURN t.id AS id
            ORDER BY coalesce(t.priority, 999999999) ASC
            """,
            prefix=PREFIX,
        )
        return [row["id"] for row in rows]


def _current_ready_ids() -> list[str]:
    return [row["id"] for row in get_ready_tasks(CFG) if str(row["id"]).startswith(PREFIX)]


def _legacy_next(project_id: str | None = None) -> str | None:
    with _driver().session(database=CFG.neo4j_db) as session:
        row = session.run(
            """
            MATCH (proj:OrchProject)-[:HAS_PHASE]->(ph:OrchPhase)-[:HAS_TASK]->(t:OrchTask)
            WHERE t.status = 'pending'
              AND coalesce(t.owner, '') = $sess
              AND coalesce(t.blocked_on, '') = ''
              AND ($project_id IS NULL OR proj.id = $project_id)
              AND NOT EXISTS {
                  MATCH (t)-[:DEPENDS_ON]->(dep:OrchTask)
                  WHERE dep.status <> 'completed'
              }
              AND coalesce(toLower(trim(proj.status)), '') IN ['active', 'in_progress']
            RETURN t.id AS task_id
            ORDER BY toInteger(coalesce(proj.priority, 999999999)) ASC,
                     toInteger(coalesce(t.priority, 999999999)) ASC,
                     t.created_at ASC
            LIMIT 1
            """,
            sess=OWNER,
            project_id=project_id,
        ).single()
    return row["task_id"] if row else None


def _legacy_project_ready_ids() -> list[str]:
    with _driver().session(database=CFG.neo4j_db) as session:
        rows = session.run(
            """
            MATCH (p:OrchProject {id: $project_id})-[:HAS_PHASE]->(ph:OrchPhase)-[:HAS_TASK]->(t:OrchTask)
            WHERE t.status = 'pending'
              AND coalesce(t.owner, '') = $owner
              AND coalesce(t.blocked_on, '') = ''
              AND NOT EXISTS {
                  MATCH (t)-[:DEPENDS_ON]->(dep:OrchTask)
                  WHERE dep.status <> 'completed'
              }
            RETURN t.id AS id
            ORDER BY coalesce(t.priority, 999999999) ASC, t.created_at ASC
            """,
            project_id=ACTIVE_PROJECT,
            owner=OWNER,
        )
        return [row["id"] for row in rows]


def main() -> int:
    init_schema(config=CFG)
    _cleanup()
    try:
        _setup()

        for name in ("zero", "dep_done", "dep_open"):
            task_id = _task(ACTIVE_PROJECT, name)
            _check(f"zero-dep path matches legacy for {name}", _current_zero(task_id) == _legacy_zero(task_id), {
                "current": _current_zero(task_id),
                "legacy": _legacy_zero(task_id),
            })

        _check("get_ready_tasks matches legacy broad ready ids", _current_ready_ids() == _legacy_ready_ids(), {
            "current": _current_ready_ids(),
            "legacy": _legacy_ready_ids(),
        })

        current_next = get_session_next_ready(OWNER, config=CFG)
        current_next_project = get_session_next_ready(OWNER, project_id=ACTIVE_PROJECT, config=CFG)
        _check("get_session_next_ready matches legacy global top", (current_next or {}).get("task_id") == _legacy_next(), {
            "current": current_next,
            "legacy": _legacy_next(),
        })
        _check("get_session_next_ready matches legacy project-scoped top", (current_next_project or {}).get("task_id") == _legacy_next(ACTIVE_PROJECT), {
            "current": current_next_project,
            "legacy": _legacy_next(ACTIVE_PROJECT),
        })

        current_project_ids = [row["id"] for row in get_project_ready_tasks(ACTIVE_PROJECT, owner=OWNER, config=CFG)]
        _check("get_project_ready_tasks matches legacy project ready ids", current_project_ids == _legacy_project_ready_ids(), {
            "current": current_project_ids,
            "legacy": _legacy_project_ready_ids(),
        })
    finally:
        _cleanup()

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - canonical ready dependency predicate preserved legacy behavior.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
