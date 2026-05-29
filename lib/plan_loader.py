from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from .config import OrchConfig, get_neo4j_driver
from .orch_schema import (
    add_dependency,
    assign_task_to_phase,
    create_phase,
    create_project,
    create_task,
)


PROJECT_RE = re.compile(r"^# Project:\s*(?P<id>.+?)\s+[—-]\s+(?P<name>.+?)\s*$")
PHASE_RE = re.compile(r"^## Phase:\s*(?P<id>.+?)\s+[—-]\s+(?P<name>.+?)\s*(?P<meta>(?:\[[^\]]+\]\s*)*)$")
TASK_RE = re.compile(r"^### Task:\s*(?P<id>.+?)\s+[—-]\s+(?P<desc>.+?)\s*(?P<meta>(?:\[[^\]]+\]\s*)*)$")
META_RE = re.compile(r"\[([^\]]+)\]")


def _parse_meta(meta_blob: str) -> Dict[str, Any]:
    meta: Dict[str, Any] = {}
    for raw in META_RE.findall(meta_blob or ""):
        if ":" not in raw:
            continue
        key, value = raw.split(":", 1)
        key = key.strip()
        value = value.strip()
        if key in {"order", "priority"}:
            try:
                meta[key] = int(value)
            except ValueError:
                meta[key] = value
        elif key in {"tags", "depends"}:
            meta[key] = [part.strip() for part in value.split(",") if part.strip()]
        else:
            meta[key] = value
    return meta


def _append_body_line(task: Optional[Dict[str, Any]], line: str) -> None:
    if task is None:
        return
    stripped = line.strip()
    if stripped.startswith("- "):
        task["body"].append(stripped[2:].strip())
    elif task["body"] and (line.startswith("  ") or line.startswith("\t")):
        task["body"][-1] = f"{task['body'][-1]} {stripped}"


def _existing_project_state(project_id: str, cfg: OrchConfig) -> Dict[str, Any]:
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        phases = {
            row["phase_id"]
            for row in session.run("""
                MATCH (:OrchProject {id: $project_id})-[:HAS_PHASE]->(ph:OrchPhase)
                RETURN ph.id AS phase_id
            """, project_id=project_id)
        }
        tasks = {
            row["task_id"]: row["phase_id"]
            for row in session.run("""
                MATCH (:OrchProject {id: $project_id})-[:HAS_PHASE]->(ph:OrchPhase)-[:HAS_TASK]->(t:OrchTask)
                RETURN t.id AS task_id, ph.id AS phase_id
            """, project_id=project_id)
        }
    return {"phase_ids": phases, "task_phase": tasks}


def _set_task_metadata(task: Dict[str, Any], cfg: OrchConfig) -> None:
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        session.run("""
            MATCH (t:OrchTask {id: $task_id})
            SET t.owner = $owner,
                t.capability_tags = $tags,
                t.updated_at = datetime()
        """,
            task_id=task["id"],
            owner=task.get("owner", ""),
            tags=task.get("tags", []),
        )


def _parse_plan(md: str) -> Dict[str, Any]:
    project: Optional[Dict[str, Any]] = None
    current_phase: Optional[Dict[str, Any]] = None
    current_task: Optional[Dict[str, Any]] = None
    phases: List[Dict[str, Any]] = []
    errors: List[str] = []
    description_lines: List[str] = []
    user_stop_conditions: List[str] = []
    in_code_block = False
    in_user_stop_conditions = False

    for line_no, raw_line in enumerate(md.splitlines(), start=1):
        line = raw_line.rstrip()
        stripped = line.strip()

        if line.startswith("```"):
            in_code_block = not in_code_block
            continue

        if in_code_block:
            continue

        project_match = PROJECT_RE.match(line)
        if project_match:
            in_user_stop_conditions = False
            if project is not None:
                errors.append(f"line {line_no}: duplicate project heading ignored")
                current_phase = None
                current_task = None
                continue
            project = {
                "id": project_match.group("id").strip(),
                "name": project_match.group("name").strip(),
            }
            continue

        phase_match = PHASE_RE.match(line)
        if phase_match:
            in_user_stop_conditions = False
            if project is None:
                errors.append(f"line {line_no}: phase declared before project")
                continue
            current_phase = {
                "id": phase_match.group("id").strip(),
                "name": phase_match.group("name").strip(),
                "order": _parse_meta(phase_match.group("meta")).get("order", 0),
                "tasks": [],
            }
            phases.append(current_phase)
            current_task = None
            continue

        task_match = TASK_RE.match(line)
        if task_match:
            in_user_stop_conditions = False
            if current_phase is None:
                errors.append(f"line {line_no}: task declared before phase")
                continue
            meta = _parse_meta(task_match.group("meta"))
            current_task = {
                "id": task_match.group("id").strip(),
                "description": task_match.group("desc").strip(),
                "priority": int(meta.get("priority", 50)),
                "owner": meta.get("owner", ""),
                "tags": meta.get("tags", []),
                "depends": meta.get("depends", []),
                "body": [],
            }
            current_phase["tasks"].append(current_task)
            continue

        if stripped == "## User Stop Conditions":
            if project is None:
                errors.append(f"line {line_no}: user stop conditions declared before project")
                continue
            current_phase = None
            current_task = None
            in_user_stop_conditions = True
            continue

        if in_user_stop_conditions and stripped.startswith("- "):
            user_stop_conditions.append(stripped[2:].strip())
            continue

        if line.startswith("## "):
            in_user_stop_conditions = False
            current_phase = None
            current_task = None
            continue

        if line.startswith("#"):
            in_user_stop_conditions = False
            current_task = None

        if project is not None and current_phase is None and line.startswith(">"):
            description_lines.append(line[1:].strip())
            continue

        _append_body_line(current_task, line)

    if project is None:
        errors.append("missing project heading")

    if project is not None:
        project["description"] = " ".join(part for part in description_lines if part).strip()
        project["user_stop_conditions"] = user_stop_conditions

    for phase in phases:
        for task in phase["tasks"]:
            if task["body"]:
                task["description"] = f"{task['description']} {' '.join(task['body'])}".strip()
            del task["body"]

    return {"project": project, "phases": phases, "errors": errors}


def load_plan_from_text(md: str, source_path: str, source_kind: str,
                        ingested_by: str,
                        config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    parsed = _parse_plan(md)
    project = parsed["project"]
    errors = list(parsed["errors"])
    if project is None:
        return {
            "project_id": None,
            "phases_created": 0,
            "tasks_created": 0,
            "tasks_updated": 0,
            "errors": errors,
            "stale_tasks": [],
        }

    cfg = config or OrchConfig()
    existing = _existing_project_state(project["id"], cfg)
    existing_phase_ids: Set[str] = set(existing["phase_ids"])
    existing_task_phase: Dict[str, str] = dict(existing["task_phase"])
    parsed_task_ids: Set[str] = set()

    ingested_at = datetime.now(timezone.utc).isoformat()
    source_sha256 = hashlib.sha256(md.encode("utf-8")).hexdigest()

    create_project(
        project_id=project["id"],
        name=project["name"],
        description=project.get("description", ""),
        user_stop_conditions=project.get("user_stop_conditions", []),
        source_path=source_path,
        source_sha256=source_sha256,
        source_kind=source_kind,
        ingested_at=ingested_at,
        ingested_by=ingested_by,
        config=cfg,
    )

    phases_created = 0
    tasks_created = 0
    tasks_updated = 0
    dependency_pairs: List[tuple[str, str]] = []

    for phase in parsed["phases"]:
        create_phase(
            project_id=project["id"],
            phase_id=phase["id"],
            name=phase["name"],
            order=phase.get("order", 0),
            config=cfg,
        )
        if phase["id"] not in existing_phase_ids:
            phases_created += 1

        for task in phase["tasks"]:
            parsed_task_ids.add(task["id"])
            create_task(
                phase_id=phase["id"],
                task_id=task["id"],
                description=task["description"],
                priority=task.get("priority", 50),
                owner=task.get("owner", ""),
                capability_tags=task.get("tags", []),
                file_blast_radius=[],
                estimated_tokens=50_000,
                config=cfg,
            )
            assign_task_to_phase(task["id"], phase["id"], config=cfg)
            _set_task_metadata(task, cfg)

            if task["id"] in existing_task_phase:
                tasks_updated += 1
            else:
                tasks_created += 1

            for depends_on in task.get("depends", []):
                dependency_pairs.append((task["id"], depends_on))

    for task_id, depends_on in dependency_pairs:
        add_dependency(task_id, depends_on, config=cfg)

    stale_tasks = sorted(task_id for task_id in existing_task_phase if task_id not in parsed_task_ids)

    return {
        "project_id": project["id"],
        "phases_created": phases_created,
        "tasks_created": tasks_created,
        "tasks_updated": tasks_updated,
        "errors": errors,
        "stale_tasks": stale_tasks,
    }
