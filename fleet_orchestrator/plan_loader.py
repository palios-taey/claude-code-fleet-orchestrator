from __future__ import annotations

import hashlib
import os
import re
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Set

from .config import OrchConfig, get_neo4j_driver
from .feature_flags import gate_template_enabled
from .orch_template import GATE_PHASE_ID, apply_gate_template
from .orch_schema import (
    _has_control_chars,
    add_dependency,
    assign_task_to_phase,
    create_phase,
    create_project,
    create_task,
    resolve_ref_path,
)

# Stored phase/task node ids are scoped to their project: <project_id>::<bare_id>. This makes task
# identity project-local so two unrelated plans can both use a generic id (audit, scaffold, ...) without
# colliding into one shared node (the global-id fusion bug, audit 2026-06-06 B1-B4).
TASK_ID_SEP = "::"
# Allowed characters in a DECLARED project/phase/task id (ids appear in /api/.../{id} URLs).
_ID_OK = re.compile(r"\A[A-Za-z0-9._-]+\Z")
TRUE_ENV_VALUES = {"1", "true", "yes", "on"}
PLAN_MODELING_CONTRACT_RULES = (
    "`owner` is the single session that executes the task, not who is accountable for the outcome; "
    "the stop-engine checks the owner's liveness heartbeat to decide if work is in-flight.",
    "Decompose multi-actor or multi-phase work into one task per executor, gated by `depends` "
    "(rca[grok] -> fix[codex] -> gate[conductor] -> deploy[conductor]); never one task spanning several actors.",
    "A supervisor-owned `in_progress` task is correct only while the supervisor is actively executing it; "
    "the supervisor gates by executing its own gate tasks.",
    "Dispatch (`taey-task dispatch` / `taey-plan assign`) binds the worker; bare `taey-notify` does not.",
)
SUPERVISOR_OWNER_IDS = {"conductor", "weaver", "tutor", "infra", "treasurer", "taeys-hands", "hunter"}
PEER_ROLE_IDS = {"codex", "grok", "gemini"}
SESSION_ROLE_IDS = SUPERVISOR_OWNER_IDS | PEER_ROLE_IDS
PLAN_DELIVERABLE_TOKENS = {
    "audit",
    "build",
    "close",
    "deploy",
    "dispatch",
    "fix",
    "gate",
    "handoff",
    "implement",
    "merge",
    "rca",
    "remediate",
    "review",
    "run",
    "ship",
    "verify",
}


class PlanIdError(ValueError):
    """A declared project/phase/task id is invalid — it contains the reserved project-scope separator
    '::' or a disallowed character. Declared ids are auto-scoped to <project>::<id>, so they must be
    plain. (Honoring a declared '::' would be a namespace-injection escape: a plan declaring
    'victimproj::audit' could MERGE onto another project's node — R2 audit blocker.)"""
    pass


class PlanTerminalStatusError(ValueError):
    pass


def plan_modeling_contract() -> Dict[str, Any]:
    return {
        "rules": list(PLAN_MODELING_CONTRACT_RULES),
    }


def validate_project_id(project_id: str) -> str:
    """A project id must be plain (it becomes the namespace prefix + appears in URLs)."""
    if not project_id or TASK_ID_SEP in project_id or not _ID_OK.match(project_id):
        raise PlanIdError(
            f"project id '{project_id}' must be plain (no '{TASK_ID_SEP}', chars A-Za-z0-9._-)."
        )
    return project_id


def scope_declared_id(project_id: str, raw_id: str) -> str:
    """THE single chokepoint for a caller-influenced DECLARED phase/task id: scope it to
    <project>::<raw>, rejecting a declared '::' (namespace injection) or invalid charset. Every path
    that creates a node from a caller id (plan ingest AND the /phases route) MUST route through this."""
    if not raw_id:
        return raw_id
    if TASK_ID_SEP in raw_id or not _ID_OK.match(raw_id):
        raise PlanIdError(
            f"declared id '{raw_id}' is invalid — declared phase/task ids are auto-scoped to "
            f"'{project_id}{TASK_ID_SEP}<id>' and must be plain (no '{TASK_ID_SEP}', chars A-Za-z0-9._-)."
        )
    return f"{project_id}{TASK_ID_SEP}{raw_id}"


META_RE = re.compile(r"\[([^\]]+)\]")
HEADER_SEPARATOR_RE = re.compile(r"\s+[—-]\s+")
_PLAN_LINE_BYTE_CAP = 4096
_META_BLOB_BYTE_CAP = 512
_WHOLE_FILE_REF_LINE_CAP = 200
_REF_FORMAT_HINT = "expected 'path' or 'path:Lstart-Lend'"
_REF_RANGE_RE = re.compile(r"\A\s*[Ll]?(\d+)\s*-\s*[Ll]?(\d+)\s*\Z")


def _parse_ref_result(raw_value: str) -> tuple[Optional[Dict[str, Any]], Optional[str]]:
    value = (raw_value or "").strip()
    if not value:
        return None, f"empty ref; {_REF_FORMAT_HINT}"
    if _has_control_chars(value):
        return None, f"ref contains control characters; {_REF_FORMAT_HINT}"

    path = value
    l_start = 1
    l_end = _WHOLE_FILE_REF_LINE_CAP

    if ":" in value:
        path_part, line_part = value.rsplit(":", 1)
        range_match = _REF_RANGE_RE.match(line_part)
        if range_match:
            path = path_part.strip()
            l_start = int(range_match.group(1))
            l_end = int(range_match.group(2))
        elif "-" in line_part or line_part.strip().isdigit() or not line_part.strip():
            return None, f"bad line range; {_REF_FORMAT_HINT}"

    path = path.strip()
    if not path:
        return None, f"missing path; {_REF_FORMAT_HINT}"
    if l_start <= 0:
        return None, f"line start must be >= 1; {_REF_FORMAT_HINT}"
    if l_end < l_start:
        return None, f"line end must be >= line start; {_REF_FORMAT_HINT}"
    return {
        "path": path,
        "l_start": l_start,
        "l_end": l_end,
    }, None


def _parse_ref(raw_value: str) -> Optional[Dict[str, Any]]:
    ref, _error = _parse_ref_result(raw_value)
    return ref


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
            ref, ref_error = _parse_ref_result(value)
            if ref is None:
                meta.setdefault("_ref_errors", []).append(
                    f"invalid ref '{value}': {ref_error or _REF_FORMAT_HINT}"
                )
                continue
            meta.setdefault("refs", []).append(ref)
        elif key in {"tags", "depends"}:
            # ACCUMULATE across repeated brackets (like `ref` above), don't overwrite.
            # A task may carry multiple `[depends: x]` tags AND/OR comma lists
            # (`[depends: a,b]`); the prior `meta[key] = [...]` kept only the LAST
            # bracket, silently dropping the others -> multi-dependency tasks were
            # under-gated (surfaced as ready while an unmet dep remained).
            meta.setdefault(key, []).extend(
                part.strip() for part in value.split(",") if part.strip()
            )
        else:
            meta[key] = value
    return meta


def _bool_meta(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in TRUE_ENV_VALUES


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
                t.recurring = $recurring,
                t.updated_at = datetime()
        """,
            task_id=task["id"],
            owner=task.get("owner", ""),
            tags=task.get("tags", []),
            recurring=bool(task.get("recurring", False)),
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


def _word_tokens(text: str) -> Set[str]:
    return set(re.sub(r"[^a-z0-9]+", " ", str(text or "").lower()).split())


def _session_roles_in_text(text: str) -> Set[str]:
    tokens = _word_tokens(text)
    roles = {role for role in SESSION_ROLE_IDS if role in tokens}
    if "taeys" in tokens and "hands" in tokens:
        roles.add("taeys-hands")
    return roles


def _deliverables_in_text(text: str) -> Set[str]:
    return PLAN_DELIVERABLE_TOKENS & _word_tokens(text)


def _looks_like_multi_actor_task(task: Dict[str, Any]) -> bool:
    text = str(task.get("description") or "")
    roles = _session_roles_in_text(text)
    deliverables = _deliverables_in_text(text)
    has_joiner = " + " in text or ";" in text or " and " in f" {text.lower()} "
    return bool(
        len(roles) >= 2
        and len(deliverables) >= 2
        and (has_joiner or len(deliverables) >= 3)
    )


def _plan_modeling_warnings(parsed: Dict[str, Any]) -> List[str]:
    warnings: List[str] = []
    for phase in parsed.get("phases", []):
        for task in phase.get("tasks", []):
            owner = str(task.get("owner") or "").strip().lower()
            if not owner:
                continue
            task_id = str(task.get("id") or "?")
            description = str(task.get("description") or "")
            roles = _session_roles_in_text(description)
            if _looks_like_multi_actor_task(task):
                warnings.append(
                    f"task {task_id} looks like multiple steps across actors; "
                    "decompose into one-task-per-executor gated by depends"
                )
            if owner in SUPERVISOR_OWNER_IDS and (roles & PEER_ROLE_IDS):
                warnings.append(
                    f"task {task_id} is owned by supervisor {owner} but description implies peer execution; "
                    "owner should be the executing session"
                )
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
                "template": meta.get("template") or meta.get("gate_template"),
                "tags": meta.get("tags", []),
            }
            for bad_ref in meta.get("_ref_errors", []):
                errors.append(f"line {line_no}: {bad_ref}")
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
                errors.append(f"line {line_no}: {bad_ref}")
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
            if "status" in meta:
                raise PlanTerminalStatusError(
                    f"line {line_no}: task status metadata is not accepted during plan ingest; "
                    "create tasks as pending and complete them through the evidence-gated task API"
                )
            current_task = {
                "id": task_match["id"],
                "description": task_match["name"],
                "priority": int(meta.get("priority", 50)),
                "owner": meta.get("owner", ""),
                "tags": meta.get("tags", []),
                "depends": meta.get("depends", []),
                "refs": meta.get("refs", []),
                "recurring": _bool_meta(meta.get("recurring")) or "recurring" in {
                    str(tag).strip().lower() for tag in meta.get("tags", [])
                },
                "body": [],
            }
            for bad_ref in meta.get("_ref_errors", []):
                errors.append(f"line {line_no}: {bad_ref}")
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


def _gate_template_enabled() -> bool:
    return gate_template_enabled()


def _gate_template_requested(project: Dict[str, Any]) -> bool:
    template = str(project.get("template") or "").strip().lower()
    tags = {str(tag).strip().lower() for tag in project.get("tags") or []}
    return template in {GATE_PHASE_ID, "forced-subrole", "forced-subrole-gate"} or GATE_PHASE_ID in tags


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
    modeling_contract = plan_modeling_contract()
    modeling_warnings = _plan_modeling_warnings(parsed)
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
            "plan_modeling_contract": modeling_contract,
            "plan_modeling_warnings": modeling_warnings,
            "stale_tasks": [],
        }
    if _gate_template_enabled() and _gate_template_requested(project):
        parsed = apply_gate_template(parsed)
        project = parsed["project"]
        warnings = list(parsed.get("warnings", warnings))
        modeling_warnings = _plan_modeling_warnings(parsed)

    # Project-scope every phase/task id (and intra-plan depends) to <project_id>::<bare_id> BEFORE any
    # existence check or write, so identity is project-local: two plans may reuse a generic id (audit,
    # scaffold, ...) without fusing into one shared node (audit 2026-06-06 B1-B4).
    _pid = validate_project_id(project["id"])

    def _ns_decl(x: str) -> str:
        # DECLARED phase/task id -> the shared chokepoint (force-scope; reject declared '::' + bad charset).
        return scope_declared_id(_pid, x)

    def _ns_dep(x: str) -> str:
        # depends: idempotent — honor a '::' already present (a DELIBERATE cross-project dependency on
        # 'otherproj::task'); a plain id scopes to this project. (This is the ONLY place '::' is honored.)
        return x if (not x or TASK_ID_SEP in x) else f"{_pid}{TASK_ID_SEP}{x}"

    for _ph in parsed["phases"]:
        _ph["id"] = _ns_decl(_ph["id"])
        for _t in _ph["tasks"]:
            _t["id"] = _ns_decl(_t["id"])
            _t["depends"] = [_ns_dep(d) for d in _t.get("depends", [])]

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
        "plan_modeling_contract": modeling_contract,
        "plan_modeling_warnings": modeling_warnings,
        "stale_tasks": stale_tasks,
    }
