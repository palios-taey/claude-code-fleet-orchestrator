from __future__ import annotations

import hashlib
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from .config import OrchConfig, get_neo4j_driver
from .orch_schema import (
    _has_control_chars,
    add_dependency,
    assign_task_to_phase,
    create_phase,
    create_project,
    create_task,
    resolve_ref_path,
)

META_RE = re.compile(r"\[([^\]]+)\]")
HEADER_SEPARATOR_RE = re.compile(r"\s+[—-]\s+")
_PLAN_LINE_BYTE_CAP = 4096
_META_BLOB_BYTE_CAP = 512


def _parse_ref(raw_value: str) -> Optional[Dict[str, Any]]:
    value = (raw_value or "").strip()
    if not value:
        return None
    try:
        path_part, line_part = value.rsplit(":", 1)
        start_raw, end_raw = line_part.split("-", 1)
        l_start = int(start_raw.strip())
        l_end = int(end_raw.strip())
    except Exception:
        return None
    path = path_part.strip()
    if not path or _has_control_chars(path) or l_start <= 0 or l_end < l_start:
        return None
    return {
        "path": path,
        "l_start": l_start,
        "l_end": l_end,
    }


def _split_header_meta(text: str) -> tuple[str, str, Optional[str]]:
    if "[" not in text:
        return text.rstrip(), "", None
    last_open = text.rfind("[")
    last_close = text.rfind("]")
    if last_open > last_close:
        return text.rstrip(), "", None
    first_open = text.find("[")
    trailing = text[first_open:]
    if len(trailing.encode("utf-8")) > _META_BLOB_BYTE_CAP:
        return text[:first_open].rstrip(), "", f"meta blob exceeds {_META_BLOB_BYTE_CAP} bytes"
    matches = list(META_RE.finditer(text))
    if not matches:
        return text.rstrip(), "", None
    trailing_start: Optional[int] = None
    cursor = len(text)
    for match in reversed(matches):
        between = text[match.end():cursor]
        if between.strip():
            break
        trailing_start = match.start()
        cursor = match.start()
    if trailing_start is None:
        return text.rstrip(), "", None
    meta_blob = text[trailing_start:].strip()
    if len(meta_blob.encode("utf-8")) > _META_BLOB_BYTE_CAP:
        return text[:trailing_start].rstrip(), "", f"meta blob exceeds {_META_BLOB_BYTE_CAP} bytes"
    return text[:trailing_start].rstrip(), meta_blob, None


def _parse_header(line: str, prefix: str) -> Optional[Dict[str, str]]:
    if not line.startswith(prefix):
        return None
    remainder = line[len(prefix):].strip()
    if not remainder:
        return None
    body, meta_blob, meta_error = _split_header_meta(remainder)
    parts = HEADER_SEPARATOR_RE.split(body, maxsplit=1)
    if len(parts) != 2:
        return None
    result = {
        "id": parts[0].strip(),
        "name": parts[1].strip(),
        "meta": meta_blob,
    }
    if meta_error:
        result["meta_error"] = meta_error
    return result


def _parse_meta(meta_blob: str) -> Dict[str, Any]:
    meta: Dict[str, Any] = {}
    if len((meta_blob or "").encode("utf-8")) > _META_BLOB_BYTE_CAP:
        meta["_meta_error"] = f"meta blob exceeds {_META_BLOB_BYTE_CAP} bytes"
        return meta
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
        elif key == "ref":
            ref = _parse_ref(value)
            if ref is None:
                meta.setdefault("_ref_errors", []).append(value)
                continue
            meta.setdefault("refs", []).append(ref)
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
            row["task_id"]: {"phase_id": row["phase_id"], "status": row["status"]}
            for row in session.run("""
                MATCH (:OrchProject {id: $project_id})-[:HAS_PHASE]->(ph:OrchPhase)-[:HAS_TASK]->(t:OrchTask)
                RETURN t.id AS task_id, ph.id AS phase_id, t.status AS status
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


def _release_ingest_holds(task_ids: Set[str], cfg: OrchConfig) -> None:
    if not task_ids:
        return
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        session.run("""
            MATCH (t:OrchTask)
            WHERE t.id IN $task_ids AND t.status = 'ingesting'
            SET t.status = 'pending',
                t.updated_at = datetime()
        """, task_ids=sorted(task_ids))


def _collect_ref_warnings(parsed: Dict[str, Any], source_path: str) -> List[str]:
    warnings: List[str] = []
    buckets: List[tuple[str, str, List[Dict[str, Any]]]] = []
    project = parsed.get("project") or {}
    buckets.append(("project", str(project.get("id") or "?"), project.get("refs", [])))
    for phase in parsed.get("phases", []):
        buckets.append(("phase", str(phase.get("id") or "?"), phase.get("refs", [])))
        for task in phase.get("tasks", []):
            buckets.append(("task", str(task.get("id") or "?"), task.get("refs", [])))
    for kind, node_id, refs in buckets:
        for ref in refs:
            resolved, resolve_warning = resolve_ref_path(str(ref.get("path") or ""), source_path)
            if resolve_warning:
                warnings.append(resolve_warning)
                continue
            if resolved is None or not resolved.exists():
                warnings.append(f"{kind} {node_id}: ref unreadable {ref.get('path')}:{ref.get('l_start')}-{ref.get('l_end')}")
    return warnings


def _parse_plan(md: str) -> Dict[str, Any]:
    project: Optional[Dict[str, Any]] = None
    current_phase: Optional[Dict[str, Any]] = None
    current_task: Optional[Dict[str, Any]] = None
    phases: List[Dict[str, Any]] = []
    errors: List[str] = []
    warnings: List[str] = []
    description_lines: List[str] = []
    user_stop_conditions: List[str] = []
    in_code_block = False
    in_user_stop_conditions = False

    for line_no, raw_line in enumerate(md.splitlines(), start=1):
        if len(raw_line.encode("utf-8")) > _PLAN_LINE_BYTE_CAP:
            warnings.append(f"line {line_no}: skipped overlong line (> {_PLAN_LINE_BYTE_CAP} bytes)")
            continue
        line = raw_line.rstrip()
        stripped = line.strip()

        if line.startswith("```"):
            in_code_block = not in_code_block
            continue

        if in_code_block:
            continue

        project_match = _parse_header(line, "# Project:")
        if project_match:
            in_user_stop_conditions = False
            if project is not None:
                errors.append(f"line {line_no}: duplicate project heading ignored")
                current_phase = None
                current_task = None
                continue
            if project_match.get("meta_error"):
                warnings.append(f"line {line_no}: {project_match['meta_error']}")
                current_phase = None
                current_task = None
                continue
            meta = _parse_meta(project_match["meta"])
            if meta.get("_meta_error"):
                warnings.append(f"line {line_no}: {meta['_meta_error']}")
                current_phase = None
                current_task = None
                continue
            project = {
                "id": project_match["id"],
                "name": project_match["name"],
                "refs": meta.get("refs", []),
            }
            for bad_ref in meta.get("_ref_errors", []):
                errors.append(f"line {line_no}: invalid ref '{bad_ref}'")
            continue

        phase_match = _parse_header(line, "## Phase:")
        if phase_match:
            in_user_stop_conditions = False
            if project is None:
                errors.append(f"line {line_no}: phase declared before project")
                continue
            if phase_match.get("meta_error"):
                warnings.append(f"line {line_no}: {phase_match['meta_error']}")
                current_task = None
                continue
            meta = _parse_meta(phase_match["meta"])
            if meta.get("_meta_error"):
                warnings.append(f"line {line_no}: {meta['_meta_error']}")
                current_task = None
                continue
            current_phase = {
                "id": phase_match["id"],
                "name": phase_match["name"],
                "order": meta.get("order", 0),
                "refs": meta.get("refs", []),
                "tasks": [],
            }
            for bad_ref in meta.get("_ref_errors", []):
                errors.append(f"line {line_no}: invalid ref '{bad_ref}'")
            phases.append(current_phase)
            current_task = None
            continue

        task_match = _parse_header(line, "### Task:")
        if task_match:
            in_user_stop_conditions = False
            if current_phase is None:
                errors.append(f"line {line_no}: task declared before phase")
                continue
            if task_match.get("meta_error"):
                warnings.append(f"line {line_no}: {task_match['meta_error']}")
                continue
            meta = _parse_meta(task_match["meta"])
            if meta.get("_meta_error"):
                warnings.append(f"line {line_no}: {meta['_meta_error']}")
                continue
            current_task = {
                "id": task_match["id"],
                "description": task_match["name"],
                "priority": int(meta.get("priority", 50)),
                "owner": meta.get("owner", ""),
                "tags": meta.get("tags", []),
                "depends": meta.get("depends", []),
                "refs": meta.get("refs", []),
                "body": [],
            }
            for bad_ref in meta.get("_ref_errors", []):
                errors.append(f"line {line_no}: invalid ref '{bad_ref}'")
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

    return {"project": project, "phases": phases, "errors": errors, "warnings": warnings}


def plan_declares_refs(md: str) -> bool:
    parsed = _parse_plan(md)
    project = parsed.get("project") or {}
    if project.get("refs"):
        return True
    for phase in parsed.get("phases", []):
        if phase.get("refs"):
            return True
        for task in phase.get("tasks", []):
            if task.get("refs"):
                return True
    return False


def load_plan_from_text(md: str, source_path: str, source_kind: str,
                        ingested_by: str,
                        supervisor: Optional[str] = None,
                        priority: Optional[int] = None,
                        migration_exempt: bool = False,
                        config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    parsed = _parse_plan(md)
    project = parsed["project"]
    errors = list(parsed["errors"])
    warnings = list(parsed.get("warnings", []))
    if source_path:
        warnings.extend(_collect_ref_warnings(parsed, source_path))
    if project is None:
        return {
            "project_id": None,
            "phases_created": 0,
            "tasks_created": 0,
            "tasks_updated": 0,
            "errors": errors,
            "warnings": warnings,
            "stale_tasks": [],
        }

    cfg = config or OrchConfig()
    existing = _existing_project_state(project["id"], cfg)
    existing_phase_ids: Set[str] = set(existing["phase_ids"])
    existing_tasks: Dict[str, Dict[str, Any]] = dict(existing["task_phase"])
    parsed_task_ids: Set[str] = set()

    ingested_at = datetime.now(timezone.utc).isoformat()
    source_sha256 = hashlib.sha256(md.encode("utf-8")).hexdigest()

    create_project(
        project_id=project["id"],
        name=project["name"],
        description=project.get("description", ""),
        refs=project.get("refs", []),
        user_stop_conditions=project.get("user_stop_conditions", []),
        supervisor=supervisor,
        priority=priority,
        migration_exempt=migration_exempt,
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
    held_task_ids: Set[str] = set()

    for phase in parsed["phases"]:
        create_phase(
            project_id=project["id"],
            phase_id=phase["id"],
            name=phase["name"],
            order=phase.get("order", 0),
            refs=phase.get("refs", []),
            source_path=source_path,
            config=cfg,
        )
        if phase["id"] not in existing_phase_ids:
            phases_created += 1

        for task in phase["tasks"]:
            parsed_task_ids.add(task["id"])
            existing_task = existing_tasks.get(task["id"])
            is_existing_task = existing_task is not None
            create_task(
                phase_id=phase["id"],
                task_id=task["id"],
                description=task["description"],
                priority=task.get("priority", 50),
                owner=task.get("owner", ""),
                refs=task.get("refs", []),
                source_path=source_path,
                capability_tags=task.get("tags", []),
                file_blast_radius=[],
                estimated_tokens=50_000,
                initial_status="pending" if is_existing_task else "ingesting",
                wake_owner_if_ready=False,
                config=cfg,
            )
            assign_task_to_phase(task["id"], phase["id"], config=cfg)
            _set_task_metadata(task, cfg)

            if is_existing_task:
                tasks_updated += 1
                if existing_task.get("status") == "ingesting":
                    held_task_ids.add(task["id"])
            else:
                tasks_created += 1
                held_task_ids.add(task["id"])

            for depends_on in task.get("depends", []):
                dependency_pairs.append((task["id"], depends_on))

    for task_id, depends_on in dependency_pairs:
        if not add_dependency(task_id, depends_on, config=cfg):
            held_task_ids.discard(task_id)
            errors.append(
                f"task '{task_id}' depends on missing task '{depends_on}' -- dependency NOT created (would be ungated)"
            )

    _release_ingest_holds(held_task_ids, cfg)

    stale_tasks = sorted(task_id for task_id in existing_tasks if task_id not in parsed_task_ids)

    return {
        "project_id": project["id"],
        "phases_created": phases_created,
        "tasks_created": tasks_created,
        "tasks_updated": tasks_updated,
        "errors": errors,
        "warnings": warnings,
        "stale_tasks": stale_tasks,
    }
