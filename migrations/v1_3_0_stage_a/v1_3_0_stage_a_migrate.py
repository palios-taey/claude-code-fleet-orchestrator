#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from typing import Any, Dict, List

from neo4j import GraphDatabase


DEFAULT_SOURCE_URI = os.environ.get("ORCH_NEO4J_URI", "bolt://10.0.0.163:7689")
DEFAULT_TARGET_URI = os.environ.get("STAGE_A_TEST_NEO4J_URI", "bolt://127.0.0.1:7691")


def _driver(uri: str):
    return GraphDatabase.driver(uri, auth=None)


def _normalize(value: Any) -> Any:
    iso = getattr(value, "iso_format", None)
    if callable(iso):
        return iso()
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize(item) for key, item in value.items()}
    return value


def _epoch_priority(created_at: Any) -> int:
    if not created_at:
        return 0
    if isinstance(created_at, str):
        created_at = dt.datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    if created_at.tzinfo is None:
        created_at = created_at.replace(tzinfo=dt.timezone.utc)
    return -int(created_at.timestamp())


def _normalize_conditions(raw: Any) -> str:
    if raw in (None, ""):
        return "[]"
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            parsed = []
    else:
        parsed = raw
    normalized = []
    for item in parsed or []:
        if isinstance(item, str):
            normalized.append({
                "id": f"migrated-{abs(hash(item))}",
                "label": item,
                "version": 1,
                "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "created_by": "migration_v1_3_0_stage_a",
                "deprecated_at": None,
                "replaces_id": None,
            })
        elif isinstance(item, dict):
            normalized.append(item)
    return json.dumps(normalized, separators=(",", ":"), sort_keys=True)


def reset_target(uri: str) -> None:
    with _driver(uri).session(database="neo4j") as session:
        session.run("MATCH (n) DETACH DELETE n")


def _copy_nodes(source_uri: str, target_uri: str, label: str) -> int:
    count = 0
    with _driver(source_uri).session(database="neo4j") as src, _driver(target_uri).session(database="neo4j") as dst:
        rows = src.run(f"MATCH (n:{label}) RETURN n").data()
        for row in rows:
            props = _normalize(dict(row["n"]))
            node_id = props.pop("id")
            sets = ", ".join(f"n.`{key}` = ${key}" for key in props)
            query = f"MERGE (n:{label} {{id: $id}})"
            if sets:
                query += f" SET {sets}"
            dst.run(query, id=node_id, **props)
            count += 1
    return count


def _copy_relationships(source_uri: str, target_uri: str) -> int:
    count = 0
    rel_specs = [
        ("OrchProject", "HAS_PHASE", "OrchPhase", "project_id", "phase_id"),
        ("OrchPhase", "HAS_TASK", "OrchTask", "phase_id", "task_id"),
        ("OrchTask", "DEPENDS_ON", "OrchTask", "task_id", "depends_on_id"),
    ]
    with _driver(source_uri).session(database="neo4j") as src, _driver(target_uri).session(database="neo4j") as dst:
        for left, rel, right, left_id, right_id in rel_specs:
            rows = src.run(
                f"MATCH (a:{left})-[:{rel}]->(b:{right}) RETURN a.id AS {left_id}, b.id AS {right_id}"
            ).data()
            for row in rows:
                dst.run(
                    f"""
                    MATCH (a:{left} {{id: ${left_id}}})
                    MATCH (b:{right} {{id: ${right_id}}})
                    MERGE (a)-[:{rel}]->(b)
                    """,
                    **row,
                )
                count += 1
    return count


def seed_target(source_uri: str, target_uri: str) -> Dict[str, int]:
    reset_target(target_uri)
    return {
        "projects": _copy_nodes(source_uri, target_uri, "OrchProject"),
        "phases": _copy_nodes(source_uri, target_uri, "OrchPhase"),
        "tasks": _copy_nodes(source_uri, target_uri, "OrchTask"),
        "relationships": _copy_relationships(source_uri, target_uri),
    }


def apply_migration(target_uri: str, dry_run: bool = False) -> Dict[str, Any]:
    touched_projects = 0
    touched_tasks = 0
    seeded_launch_project = False
    with _driver(target_uri).session(database="neo4j") as session:
        projects = session.run("MATCH (p:OrchProject) RETURN p").data()
        for row in projects:
            props = dict(row["p"])
            needs = (
                props.get("migration_exempt") is None
                or props.get("supervisor") is None
                or props.get("priority") is None
                or props.get("priority_history") is None
                or props.get("stop_reason_history") is None
                or props.get("stop_reason_current") is None
                or props.get("in_progress_heartbeat_at") is None
                or not isinstance(props.get("user_stop_conditions"), str)
            )
            if not needs:
                continue
            touched_projects += 1
            if dry_run:
                continue
            priority = _epoch_priority(_normalize(props.get("created_at")))
            session.run(
                """
                MATCH (p:OrchProject {id: $id})
                SET p.migration_exempt = true,
                    p.supervisor = coalesce(p.supervisor, 'unassigned'),
                    p.priority = coalesce(p.priority, $priority),
                    p.status = coalesce(p.status, 'active'),
                    p.stop_reason_current = coalesce(p.stop_reason_current, ''),
                    p.stop_reason_history = coalesce(p.stop_reason_history, '[]'),
                    p.priority_history = coalesce(p.priority_history, $priority_history),
                    p.user_stop_conditions = $user_stop_conditions,
                    p.in_progress_heartbeat_at = coalesce(p.in_progress_heartbeat_at, ''),
                    p.updated_at = datetime()
                """,
                id=props["id"],
                priority=priority,
                priority_history=json.dumps([{
                    "priority_before": None,
                    "priority_after": priority,
                    "set_by": "migration_v1_3_0_stage_a",
                    "set_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    "source_surface": "migration",
                    "reason": "legacy project grandfathered out of v1.3.0 walks",
                }], separators=(",", ":"), sort_keys=True),
                user_stop_conditions=_normalize_conditions(props.get("user_stop_conditions")),
            )
        tasks = session.run("MATCH (t:OrchTask) RETURN t.id AS id, t.forced_continuation_count AS forced_continuation_count").data()
        for row in tasks:
            if row.get("forced_continuation_count") is not None:
                continue
            touched_tasks += 1
            if dry_run:
                continue
            session.run("MATCH (t:OrchTask {id: $id}) SET t.forced_continuation_count = 0", id=row["id"])
        launch = session.run("MATCH (p:OrchProject {id: 'orch-v1-3-0-launch'}) RETURN p.id AS id").single()
        if not launch:
            seeded_launch_project = True
            if not dry_run:
                session.run(
                    """
                    MERGE (p:OrchProject {id: 'orch-v1-3-0-launch'})
                    SET p.name = 'orchestrator v1.3.0 launch',
                        p.description = 'Seed project for v1.3.0 stop-reason engine rollout',
                        p.created_at = datetime(),
                        p.status = 'active',
                        p.supervisor = 'conductor',
                        p.priority = 1,
                        p.migration_exempt = false,
                        p.stop_reason_current = '',
                        p.stop_reason_history = '[]',
                        p.priority_history = $priority_history,
                        p.user_stop_conditions = $conditions,
                        p.in_progress_heartbeat_at = ''
                    MERGE (ph:OrchPhase {id: 'orch-v1-3-0-launch-main'})
                    SET ph.name = 'Main', ph.order = 0, ph.status = 'active', ph.created_at = datetime()
                    MERGE (p)-[:HAS_PHASE]->(ph)
                    """,
                    priority_history=json.dumps([{
                        "priority_before": None,
                        "priority_after": 1,
                        "set_by": "migration_v1_3_0_stage_a",
                        "set_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "source_surface": "migration",
                        "reason": "v1.3.0 launch seed",
                    }], separators=(",", ":"), sort_keys=True),
                    conditions=json.dumps([{
                        "id": "launch-stop-all-ready",
                        "label": "stop_when_all_ready_tasks_dispatched",
                        "version": 1,
                        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                        "created_by": "migration_v1_3_0_stage_a",
                        "deprecated_at": None,
                        "replaces_id": None,
                    }], separators=(",", ":"), sort_keys=True),
                )
    return {
        "target_uri": target_uri,
        "dry_run": dry_run,
        "projects_touched": touched_projects,
        "tasks_touched": touched_tasks,
        "seeded_launch_project": seeded_launch_project,
        "errors": [],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-uri", default=DEFAULT_SOURCE_URI)
    parser.add_argument("--target-uri", default=DEFAULT_TARGET_URI)
    parser.add_argument("--seed-from-source", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.seed_from_source:
        print(json.dumps({"seeded": seed_target(args.source_uri, args.target_uri), "source_uri": args.source_uri, "target_uri": args.target_uri}, sort_keys=True))
    print(json.dumps(apply_migration(args.target_uri, dry_run=args.dry_run), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
