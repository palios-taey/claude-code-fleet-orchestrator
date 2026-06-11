#!/usr/bin/env python3
"""Stage A migration priority convention acceptance.

The migration must write valid non-negative/positive priorities that sort under
the runtime convention used everywhere else: lower number is earlier.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.config import OrchConfig, get_neo4j_driver  # noqa: E402
from lib.orch_schema import init_schema  # noqa: E402
from migrations.v1_3_0_stage_a.v1_3_0_stage_a_migrate import (  # noqa: E402
    _epoch_priority,
    apply_migration,
)

CFG = OrchConfig()
PREFIX = f"migpri-ci-{uuid.uuid4().hex[:8]}"
FAILURES: list[str] = []


def _check(label: str, condition: bool, extra: str = "") -> None:
    print(("PASS " if condition else "FAIL ") + label + ("" if condition else f" -> {extra}"))
    if not condition:
        FAILURES.append(label)


def _cleanup() -> None:
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=PREFIX)


def _seed_legacy_project(project_id: str, created_at: dt.datetime) -> None:
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run(
            """
            CREATE (p:OrchProject {
                id: $project_id,
                name: $project_id,
                created_at: datetime($created_at),
                status: 'active',
                supervisor: $supervisor
            })
            """,
            project_id=project_id,
            created_at=created_at.isoformat(),
            supervisor=f"{PREFIX}-supervisor",
        )


def _project(project_id: str) -> dict:
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        record = session.run(
            "MATCH (p:OrchProject {id: $project_id}) RETURN p",
            project_id=project_id,
        ).single()
    return dict(record["p"]) if record else {}


def _ordered_fixture_project_ids() -> list[str]:
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        return [
            record["id"]
            for record in session.run(
                """
                MATCH (p:OrchProject)
                WHERE p.id IN $ids
                RETURN p.id AS id
                ORDER BY coalesce(p.priority, 999999999) ASC, p.created_at ASC
                """,
                ids=[f"{PREFIX}-old", f"{PREFIX}-new"],
            )
        ]


def main() -> int:
    init_schema(config=CFG)
    _cleanup()
    old_id = f"{PREFIX}-old"
    new_id = f"{PREFIX}-new"
    old_created = dt.datetime(2024, 1, 1, tzinfo=dt.timezone.utc)
    new_created = dt.datetime(2024, 1, 2, tzinfo=dt.timezone.utc)

    try:
        _check("epoch priority is positive", _epoch_priority(old_created) > 0, str(_epoch_priority(old_created)))
        _check(
            "older created_at sorts earlier by lower priority",
            _epoch_priority(old_created) < _epoch_priority(new_created),
            f"old={_epoch_priority(old_created)} new={_epoch_priority(new_created)}",
        )

        _seed_legacy_project(old_id, old_created)
        _seed_legacy_project(new_id, new_created)
        result = apply_migration(CFG.neo4j_uri)
        _check("migration touched at least the two fixture projects", result["projects_touched"] >= 2, str(result))

        old_project = _project(old_id)
        new_project = _project(new_id)
        old_priority = int(old_project.get("priority"))
        new_priority = int(new_project.get("priority"))
        old_history = json.loads(old_project.get("priority_history") or "[]")

        _check("old migrated priority is positive", old_priority > 0, str(old_priority))
        _check("new migrated priority is positive", new_priority > 0, str(new_priority))
        _check(
            "migrated priority preserves lower-number-earlier ordering",
            old_priority < new_priority,
            f"old={old_priority} new={new_priority}",
        )
        _check(
            "priority_history matches positive migrated priority",
            bool(old_history) and old_history[0].get("priority_after") == old_priority,
            str(old_history),
        )

        ordered = _ordered_fixture_project_ids()
        _check("runtime priority reader sees older migrated project first", ordered == [old_id, new_id], str(ordered))
    finally:
        _cleanup()

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - migration priority convention is positive and reader-aligned")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
