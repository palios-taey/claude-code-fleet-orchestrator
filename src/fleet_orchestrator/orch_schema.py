"""
Neo4j Orchestration Schema

Creates task DAG schema in the neo4j database with Orch-prefixed labels
to isolate from memory infrastructure (ISMA, HMM, Weaviate).

Label convention: OrchProject, OrchPhase, OrchTask, OrchFileOwnership
(memory labels: ISMAExchange, HMMTile, HMMMotif, Message, ChatSession)
"""

import copy
import datetime as dt
import json
import os
import subprocess
import sys
import time
import uuid
from typing import Any, Dict, List, Optional

from .config import OrchConfig, get_neo4j_driver


SCHEMA_CONSTRAINTS = [
    "CREATE CONSTRAINT orch_task_id IF NOT EXISTS FOR (t:OrchTask) REQUIRE t.id IS UNIQUE",
    "CREATE CONSTRAINT orch_project_id IF NOT EXISTS FOR (p:OrchProject) REQUIRE p.id IS UNIQUE",
    "CREATE CONSTRAINT orch_phase_id IF NOT EXISTS FOR (ph:OrchPhase) REQUIRE ph.id IS UNIQUE",
    "CREATE CONSTRAINT orch_question_id IF NOT EXISTS FOR (q:OrchQuestion) REQUIRE q.id IS UNIQUE",
]

SCHEMA_INDEXES = [
    "CREATE INDEX orch_task_status IF NOT EXISTS FOR (t:OrchTask) ON (t.status)",
    "CREATE INDEX orch_task_owner IF NOT EXISTS FOR (t:OrchTask) ON (t.owner)",
    "CREATE INDEX orch_file_path IF NOT EXISTS FOR (f:OrchFileOwnership) ON (f.path)",
    "CREATE INDEX orch_question_status IF NOT EXISTS FOR (q:OrchQuestion) ON (q.status)",
    "CREATE INDEX orch_project_supervisor IF NOT EXISTS FOR (p:OrchProject) ON (p.supervisor)",
    "CREATE INDEX orch_project_priority IF NOT EXISTS FOR (p:OrchProject) ON (p.priority)",
    "CREATE INDEX orch_project_supervisor_status_priority IF NOT EXISTS FOR (p:OrchProject) ON (p.supervisor, p.status, p.priority)",
]


class ProjectNotFoundError(ValueError):
    pass


class ReadyWorkConflictError(ValueError):
    pass


class ConditionValidationError(ValueError):
    pass


class PriorityAuditError(ValueError):
    pass


class PauseValidationError(ValueError):
    pass


class TaskTransitionError(ValueError):
    pass


class TaskWriteError(ValueError):
    pass


_PAUSE_SOURCES = {"ui", "cli", "api", "user_command_explicit"}
_TASK_TERMINAL_STATUSES = {"completed", "failed", "interrupted"}
_TASK_ALLOWED_TRANSITIONS = {
    "pending": {"pending", "in_progress", "completed", "failed", "interrupted"},
    "in_progress": {"in_progress", "completed", "failed", "interrupted"},
    "completed": {"completed"},
    "failed": {"failed"},
    "interrupted": {"interrupted"},
}


_DECLARED_DEPS_EXPR = """
CASE
    WHEN t.declared_dependencies IS NULL OR size(t.declared_dependencies) = 0
    THEN size(deps)
    ELSE size(t.declared_dependencies)
END
"""


def _ready_task_clause(task_alias: str = "t", deps_alias: str = "deps",
                       passthrough: Optional[List[str]] = None) -> str:
    passthrough_vars = list(passthrough or [])
    passthrough_prefix = ", ".join(passthrough_vars)
    if passthrough_prefix:
        passthrough_prefix += ", "
    return f"""
WITH {passthrough_prefix}{task_alias}, {deps_alias}, CASE
    WHEN {task_alias}.declared_dependencies IS NULL OR size({task_alias}.declared_dependencies) = 0
    THEN size({deps_alias})
    ELSE size({task_alias}.declared_dependencies)
END AS declared_dep_count
WHERE size({deps_alias}) = declared_dep_count
  AND coalesce({task_alias}.status, 'pending') = 'pending'
  AND ALL(dep IN {deps_alias} WHERE coalesce(dep.status, 'pending') = 'completed')
"""


def _zero_declared_dependency_clause(task_alias: str = "t", deps_alias: str = "deps") -> str:
    return f"""
WITH {task_alias}, {deps_alias}, CASE
    WHEN {task_alias}.declared_dependencies IS NULL THEN []
    ELSE {task_alias}.declared_dependencies
END AS declared_dependencies
WHERE size(declared_dependencies) = 0
  AND size({deps_alias}) = 0
"""


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _json_encode(value: Any) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def validate_task_transition(current_status: str, next_status: str,
                             commit_sha: str = "",
                             production_observation: str = "") -> None:
    current = (current_status or "pending").strip()
    target = (next_status or "").strip()
    allowed = _TASK_ALLOWED_TRANSITIONS.get(current)
    if not target or allowed is None or target not in allowed:
        raise TaskTransitionError(
            f"invalid status transition {current}->{target}; allowed transitions from {current}: "
            + ", ".join(sorted(_TASK_ALLOWED_TRANSITIONS.get(current, [])))
        )
    if target == "completed":
        if not (commit_sha or "").strip() or not (production_observation or "").strip():
            raise TaskTransitionError(
                "completed transition requires close-out evidence: provide commit_sha and production_observation"
            )


def _normalize_closeout_value(value: Optional[str]) -> Optional[str]:
    if value is None:
        return None
    normalized = value.strip()
    if not normalized or normalized == "__KEEP__":
        return None
    return normalized


def _effective_closeout_value(existing_value: Optional[str], incoming_value: Optional[str]) -> Optional[str]:
    normalized_incoming = _normalize_closeout_value(incoming_value)
    if incoming_value is None:
        return _normalize_closeout_value(existing_value)
    if isinstance(incoming_value, str) and incoming_value.strip() == "__KEEP__":
        return _normalize_closeout_value(existing_value)
    return normalized_incoming


def _decode_json_field(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return copy.deepcopy(default)
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            return copy.deepcopy(default)
    return raw


def _normalize_owner_session(owner: str) -> str:
    owner = (owner or "").strip()
    for suffix in ("-codex", "-gemini", "-grok"):
        if owner.endswith(suffix):
            return owner[: -len(suffix)]
    return owner


def _condition_view(condition: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "condition_id": condition["id"],
        "version": int(condition["version"]),
        "label": condition["label"],
    }


def _build_condition(label: str, created_by: str, *, condition_id: Optional[str] = None,
                     version: int = 1, deprecated_at: Optional[str] = None,
                     replaces_id: Optional[str] = None,
                     created_at: Optional[str] = None) -> Dict[str, Any]:
    cond_id = condition_id or uuid.uuid4().hex
    return {
        "id": cond_id,
        "label": label,
        "version": int(version),
        "created_at": created_at or _utc_now_iso(),
        "created_by": created_by or "unknown",
        "deprecated_at": deprecated_at,
        "replaces_id": replaces_id,
    }


def _normalize_user_stop_conditions(conditions: Optional[List[Any]], created_by: str) -> List[Dict[str, Any]]:
    normalized: List[Dict[str, Any]] = []
    for item in conditions or []:
        if isinstance(item, str):
            normalized.append(_build_condition(item, created_by))
            continue
        if isinstance(item, dict):
            label = str(item.get("label") or "").strip()
            if not label:
                continue
            normalized.append(_build_condition(
                label,
                str(item.get("created_by") or created_by or "unknown"),
                condition_id=str(item.get("id") or item.get("condition_id") or uuid.uuid4().hex),
                version=int(item.get("version", 1)),
                deprecated_at=item.get("deprecated_at"),
                replaces_id=item.get("replaces_id"),
                created_at=item.get("created_at"),
            ))
    return normalized


def _active_conditions(conditions: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [cond for cond in conditions if not cond.get("deprecated_at")]


def _decode_project_node(node: Dict[str, Any]) -> Dict[str, Any]:
    project = _normalize_map(node)
    project["user_stop_conditions"] = _normalize_user_stop_conditions(
        _decode_json_field(project.get("user_stop_conditions"), []),
        created_by="decoded",
    )
    project["stop_reason_current"] = _decode_json_field(project.get("stop_reason_current"), None)
    project["stop_reason_history"] = _decode_json_field(project.get("stop_reason_history"), [])
    project["priority_history"] = _decode_json_field(project.get("priority_history"), [])
    stop_state = _project_stop_reason_state(project)
    project["stop_reason_orphaned"] = stop_state["orphaned"]
    return project


def _condition_lookup(conditions: List[Dict[str, Any]], condition_id: str,
                      version: Optional[int] = None) -> Optional[Dict[str, Any]]:
    matches = [cond for cond in conditions if cond.get("id") == condition_id]
    if version is not None:
        matches = [cond for cond in matches if int(cond.get("version", 0)) == int(version)]
    if not matches:
        return None
    matches.sort(key=lambda item: int(item.get("version", 0)), reverse=True)
    return matches[0]


def _project_stop_reason_state(project: Dict[str, Any]) -> Dict[str, Any]:
    conditions = list(project.get("user_stop_conditions") or [])
    current = project.get("stop_reason_current")
    active_conditions = _active_conditions(conditions)
    deprecated_only = bool(conditions) and not active_conditions
    if not current:
        return {
            "valid": False,
            "orphaned": False,
            "deprecated_only": deprecated_only,
            "condition": None,
        }

    matched = _condition_lookup(
        active_conditions,
        str(current.get("condition_id") or ""),
        int(current.get("condition_version", 0) or 0),
    )
    if matched:
        return {
            "valid": True,
            "orphaned": False,
            "deprecated_only": deprecated_only,
            "condition": matched,
        }
    return {
        "valid": False,
        "orphaned": True,
        "deprecated_only": deprecated_only,
        "condition": None,
    }


def _append_history(existing: List[Dict[str, Any]], entry: Dict[str, Any]) -> str:
    payload = list(existing)
    payload.append(entry)
    return _json_encode(payload)


def _next_project_priority(supervisor: str, config: Optional[OrchConfig] = None) -> int:
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        record = session.run("""
            MATCH (p:OrchProject)
            WHERE coalesce(p.supervisor, '') = $supervisor
            RETURN max(coalesce(p.priority, 0)) AS max_priority
        """, supervisor=supervisor).single()
    current = record["max_priority"] if record and record["max_priority"] is not None else 0
    return int(current) + 1


def _project_record(project_id: str, config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        record = session.run("""
            MATCH (p:OrchProject {id: $project_id})
            RETURN p
        """, project_id=project_id).single()
    if not record:
        raise ProjectNotFoundError(f"Project {project_id} not found")
    return _decode_project_node(dict(record["p"]))


_ZERO_DEP_READY_CYPHER = """
MATCH (t:OrchTask {id: $task_id})
WHERE coalesce(t.owner, '') <> ''
  AND coalesce(t.status, 'pending') = 'pending'
OPTIONAL MATCH (t)-[:DEPENDS_ON]->(dep:OrchTask)
WITH t, collect(dep) AS deps
""" + _zero_declared_dependency_clause() + """
RETURN t.id AS task_id,
       t.owner AS owner,
       t.description AS description
"""


def _fleet_redis_connect():
    for path in (
        "/usr/local/lib/claude-code-fleet-notify",
        "/path/to/repo",  # lint-allow: fleet-notify ships as a separate runtime dependency with a canonical checkout path on Mira hosts
    ):
        # KEEP: fleet-notify's ``identity`` module is an external runtime
        # dependency, so we must discover its install root before importing.
        if os.path.isdir(path) and path not in sys.path:
            sys.path.insert(0, path)
    from identity import redis_connect  # type: ignore
    return redis_connect()


def _state_key(node_id: str, suffix: str) -> str:
    prefix = os.environ.get("NOTIFY_KEY_PREFIX", "taey")
    return f"{prefix}:{node_id}:{suffix}"


def _send_wake(owner: str, body: str) -> None:
    cli = "/usr/local/bin/taey-notify"
    if not (os.path.isfile(cli) and os.access(cli, os.X_OK)):
        raise RuntimeError(f"taey-notify missing or not executable: {cli}")
    result = subprocess.run(
        [cli, owner, body, "--from", "orch-create", "--type", "wake", "--priority", "normal"],
        capture_output=True,
        text=True,
        check=True,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "taey-notify failed")


def _wake_owner_for_zero_dep_task(task_id: str, cfg: OrchConfig) -> None:
    from .plan_readiness import _dedup_wake

    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        record = session.run(_ZERO_DEP_READY_CYPHER, task_id=task_id).single()

    if not record:
        return

    owner = record["owner"]
    redis_client = _fleet_redis_connect()
    if not redis_client.get(_state_key(owner, "idle")):
        return
    if redis_client.get(_state_key(owner, "current_task")):
        return
    if not _dedup_wake(redis_client, task_id):
        return

    body = (
        f"WAKE: task={record['task_id']} "
        f"(\"{(record.get('description') or '')[:80]}\") has zero dependencies "
        f"and is ready now. Pick it up with `taey-plan next` or dispatch a worker."
    )
    _send_wake(owner, body)


def init_schema(config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    """Create orchestration schema (constraints + indexes). Idempotent."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    results = {"constraints": [], "indexes": [], "errors": []}

    with driver.session(database=cfg.neo4j_db) as session:
        for stmt in SCHEMA_CONSTRAINTS:
            try:
                session.run(stmt)
                results["constraints"].append(stmt.split("FOR")[0].strip())
            except Exception as e:
                results["errors"].append(f"{stmt[:60]}: {e}")

        for stmt in SCHEMA_INDEXES:
            try:
                session.run(stmt)
                results["indexes"].append(stmt.split("FOR")[0].strip())
            except Exception as e:
                results["errors"].append(f"{stmt[:60]}: {e}")

    return results


def create_project(project_id: str, name: str, description: str = "",
                   source_path: Optional[str] = None,
                   source_sha256: Optional[str] = None,
                   source_kind: Optional[str] = None,
                   ingested_at: Optional[str] = None,
                   ingested_by: Optional[str] = None,
                   user_stop_conditions: Optional[List[Any]] = None,
                   supervisor: Optional[str] = None,
                   priority: Optional[int] = None,
                   migration_exempt: bool = False,
                   config: Optional[OrchConfig] = None) -> str:
    """Create an OrchProject node."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    created_by = ingested_by or "unknown"
    supervisor_value = supervisor or created_by or "unassigned"
    if (not migration_exempt) and supervisor_value == "unassigned":
        raise ValueError("supervisor must be non-empty and not 'unassigned' unless migration_exempt=true")
    priority_value = int(priority if priority is not None else _next_project_priority(supervisor_value, cfg))
    conditions_value = _normalize_user_stop_conditions(user_stop_conditions, created_by)
    priority_history = [{
        "priority_before": None,
        "priority_after": priority_value,
        "set_by": created_by,
        "set_at": _utc_now_iso(),
        "source_surface": "api" if source_kind == "api" else "system",
        "reason": "project created",
    }]
    with driver.session(database=cfg.neo4j_db) as session:
        result = session.run("""
                MERGE (p:OrchProject {id: $id})
                ON CREATE SET p.created_at = datetime(), p.status = 'active'
                SET p.name = $name,
                    p.description = $description,
                    p.supervisor = $supervisor,
                    p.priority = $priority,
                    p.user_stop_conditions = CASE
                        WHEN $user_stop_conditions IS NULL THEN coalesce(p.user_stop_conditions, '[]')
                        ELSE $user_stop_conditions
                    END,
                    p.stop_reason_current = coalesce(p.stop_reason_current, ''),
                    p.stop_reason_history = coalesce(p.stop_reason_history, '[]'),
                    p.priority_history = CASE
                        WHEN p.priority_history IS NULL OR p.priority_history = '' THEN $priority_history
                        ELSE p.priority_history
                    END,
                    p.migration_exempt = coalesce(p.migration_exempt, $migration_exempt),
                    p.in_progress_heartbeat_at = coalesce(p.in_progress_heartbeat_at, ''),
                    p.source_path = CASE
                        WHEN $source_path IS NULL OR $source_path = '' THEN p.source_path
                        ELSE $source_path
                    END,
                    p.source_sha256 = CASE
                        WHEN $source_sha256 IS NULL OR $source_sha256 = '' THEN p.source_sha256
                        ELSE $source_sha256
                    END,
                    p.source_kind = CASE
                        WHEN $source_kind IS NULL OR $source_kind = '' THEN p.source_kind
                        ELSE $source_kind
                    END,
                    p.ingested_at = CASE
                        WHEN $ingested_at IS NULL OR $ingested_at = '' THEN p.ingested_at
                        ELSE datetime($ingested_at)
                    END,
                    p.ingested_by = CASE
                        WHEN $ingested_by IS NULL OR $ingested_by = '' THEN p.ingested_by
                        ELSE $ingested_by
                    END
                RETURN p.id AS id
            """,
                id=project_id,
                name=name,
                description=description,
                supervisor=supervisor_value,
                priority=priority_value,
                source_path=source_path,
                source_sha256=source_sha256,
                source_kind=source_kind,
                ingested_at=ingested_at,
                ingested_by=ingested_by,
                user_stop_conditions=_json_encode(conditions_value),
                priority_history=_json_encode(priority_history),
                migration_exempt=bool(migration_exempt),
            )
        record = result.single()
        if not record:
            raise ProjectNotFoundError(f"Unable to create or update project {project_id}")
        return record["id"]


def create_phase(project_id: str, phase_id: str, name: str,
                 order: int = 0, config: Optional[OrchConfig] = None) -> str:
    """Create an OrchPhase linked to a project."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        result = session.run("""
                MATCH (p:OrchProject {id: $project_id})
                MERGE (ph:OrchPhase {id: $phase_id})
                ON CREATE SET ph.created_at = datetime(), ph.status = 'pending'
                SET ph.name = $name, ph.order = $order
                MERGE (p)-[:HAS_PHASE]->(ph)
                RETURN ph.id AS id
            """, project_id=project_id, phase_id=phase_id, name=name, order=order)
        record = result.single()
        if not record:
            raise ProjectNotFoundError(f"Project {project_id} not found for phase {phase_id}")
        return record["id"]


def create_task(
    phase_id: str,
    task_id: str,
    description: str,
    priority: int = 50,
    owner: str = "",
    created_by: str = "",
    task_type: str = "standard",
    capability_tags: Optional[List[str]] = None,
    file_blast_radius: Optional[List[str]] = None,
    declared_dependencies: Optional[List[str]] = None,
    estimated_tokens: int = 50_000,
    heartbeat_exempt_secs: Optional[int] = None,
    wake_owner_if_ready: bool = True,
    config: Optional[OrchConfig] = None,
) -> str:
    """Create an OrchTask linked to a phase."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        result = session.run("""
                MATCH (ph:OrchPhase {id: $phase_id})
                MERGE (t:OrchTask {id: $task_id})
                ON CREATE SET t.created_at = datetime(),
                              t.status = 'pending',
                              t.owner = $owner,
                              t.forced_continuation_count = 0
                SET t.description = $description,
                    t.priority = $priority,
                    t.owner = $owner,
                    t.created_by = CASE
                        WHEN $created_by = '' THEN t.created_by
                        ELSE $created_by
                    END,
                    t.task_type = CASE
                        WHEN $task_type = '' THEN t.task_type
                        ELSE $task_type
                    END,
                    t.capability_tags = $capability_tags,
                    t.file_blast_radius = $file_blast_radius,
                    t.declared_dependencies = CASE
                        WHEN $declared_dependencies IS NULL THEN t.declared_dependencies
                        ELSE $declared_dependencies
                    END,
                    t.estimated_tokens = $estimated_tokens,
                    t.heartbeat_exempt_secs = $heartbeat_exempt_secs
                MERGE (ph)-[:HAS_TASK]->(t)
                RETURN t.id AS id
            """,
                task_id=task_id,
                phase_id=phase_id,
                description=description,
                priority=priority,
                owner=owner,
                created_by=created_by,
                task_type=task_type,
                capability_tags=capability_tags or [],
                file_blast_radius=file_blast_radius or [],
                declared_dependencies=(
                    None if declared_dependencies is None else sorted(set(declared_dependencies))
                ),
                estimated_tokens=estimated_tokens,
                heartbeat_exempt_secs=heartbeat_exempt_secs,
            )
        record = result.single()
        if not record:
            raise TaskWriteError(f"Phase {phase_id} not found for task {task_id}")
        created_id = record["id"]
    if wake_owner_if_ready:
        _wake_owner_for_zero_dep_task(created_id, cfg)
    return created_id


def add_dependency(task_id: str, depends_on_id: str,
                   config: Optional[OrchConfig] = None) -> bool:
    """Create DEPENDS_ON relationship between tasks."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        record = session.run("""
                MATCH (t:OrchTask {id: $task_id})
                SET t.declared_dependencies = CASE
                    WHEN t.declared_dependencies IS NULL THEN [$depends_on_id]
                    WHEN $depends_on_id IN t.declared_dependencies THEN t.declared_dependencies
                    ELSE t.declared_dependencies + $depends_on_id
                END
                WITH t
                OPTIONAL MATCH (dep:OrchTask {id: $depends_on_id})
                FOREACH (_ IN CASE WHEN dep IS NULL THEN [] ELSE [1] END |
                    MERGE (t)-[:DEPENDS_ON]->(dep)
                )
                RETURN t.id AS task_id, dep IS NOT NULL AS dependency_exists
            """, task_id=task_id, depends_on_id=depends_on_id).single()
        if record is None:
            raise ValueError(f"Task {task_id} not found")
        return bool(record["dependency_exists"])


def get_ready_tasks(config: Optional[OrchConfig] = None) -> List[Dict[str, Any]]:
    """Get tasks that are pending with all dependencies satisfied."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        result = session.run("""
                MATCH (t:OrchTask)
                WHERE coalesce(t.status, 'pending') = 'pending'
                OPTIONAL MATCH (t)-[:DEPENDS_ON]->(dep:OrchTask)
                WITH t, collect(dep) AS deps
                """ + _ready_task_clause() + """
                RETURN t.id AS id, t.description AS description,
                       t.priority AS priority, t.owner AS owner,
                       t.capability_tags AS capability_tags,
                       t.file_blast_radius AS file_blast_radius,
                       t.estimated_tokens AS estimated_tokens
                ORDER BY coalesce(t.priority, 999999999) ASC
            """)
        return [dict(r) for r in result]


def update_task_status(task_id: str, status: str, owner: str = "",
                       result: Optional[str] = None,
                       blocked_on: Optional[str] = None,
                       commit_sha: Optional[str] = None,
                       production_observation: Optional[str] = None,
                       evidence_note: Optional[str] = None,
                       config: Optional[OrchConfig] = None) -> bool:
    """Update task status, owner, and optional result."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    blocked_on_value = "__KEEP__" if blocked_on is None else blocked_on
    commit_sha_value = "__KEEP__" if commit_sha is None else commit_sha.strip()
    production_observation_value = "__KEEP__" if production_observation is None else production_observation.strip()
    evidence_note_value = "__KEEP__" if evidence_note is None else evidence_note.strip()
    with driver.session(database=cfg.neo4j_db) as session:
        current_record = session.run(
                """
                MATCH (t:OrchTask {id: $task_id})
                RETURN coalesce(t.status, 'pending') AS status,
                       t.closeout_commit_sha AS closeout_commit_sha,
                       t.closeout_production_observation AS closeout_production_observation
                """,
                task_id=task_id,
            ).single()
        if current_record is None:
            return False

        validate_task_transition(
            str(current_record["status"] or "pending"),
            status,
            commit_sha=_effective_closeout_value(
                current_record.get("closeout_commit_sha"),
                commit_sha_value,
            ) or "",
            production_observation=_effective_closeout_value(
                current_record.get("closeout_production_observation"),
                production_observation_value,
            ) or "",
        )

        if result is None:
            rec = session.run("""
                    MATCH (t:OrchTask {id: $task_id})
                    SET t.status = $status, t.owner = $owner,
                        t.blocked_on = CASE
                            WHEN $status <> 'in_progress' THEN NULL
                            WHEN $blocked_on = '__KEEP__' THEN t.blocked_on
                            WHEN $blocked_on = '' THEN NULL
                            ELSE $blocked_on
                        END,
                        t.forced_continuation_count = CASE
                            WHEN t.status <> $status THEN 0
                            WHEN coalesce(t.blocked_on, '') <> coalesce(
                                CASE
                                    WHEN $status <> 'in_progress' THEN NULL
                                    WHEN $blocked_on = '__KEEP__' THEN t.blocked_on
                                    WHEN $blocked_on = '' THEN NULL
                                    ELSE $blocked_on
                                END,
                                ''
                            ) THEN 0
                            ELSE coalesce(t.forced_continuation_count, 0)
                        END,
                        t.closeout_commit_sha = CASE
                            WHEN $commit_sha = '__KEEP__' THEN t.closeout_commit_sha
                            WHEN $commit_sha = '' THEN NULL
                            ELSE $commit_sha
                        END,
                        t.closeout_production_observation = CASE
                            WHEN $production_observation = '__KEEP__' THEN t.closeout_production_observation
                            WHEN $production_observation = '' THEN NULL
                            ELSE $production_observation
                        END,
                        t.closeout_evidence_note = CASE
                            WHEN $evidence_note = '__KEEP__' THEN t.closeout_evidence_note
                            WHEN $evidence_note = '' THEN NULL
                            ELSE $evidence_note
                        END,
                        t.updated_at = datetime()
                    RETURN t.id AS id
                """,
                    task_id=task_id,
                    status=status,
                    owner=owner,
                    blocked_on=blocked_on_value,
                    commit_sha=commit_sha_value,
                    production_observation=production_observation_value,
                    evidence_note=evidence_note_value,
            )
        else:
            rec = session.run("""
                    MATCH (t:OrchTask {id: $task_id})
                    SET t.status = $status, t.owner = $owner,
                        t.result = $result,
                        t.blocked_on = CASE
                            WHEN $status <> 'in_progress' THEN NULL
                            WHEN $blocked_on = '__KEEP__' THEN t.blocked_on
                            WHEN $blocked_on = '' THEN NULL
                            ELSE $blocked_on
                        END,
                        t.forced_continuation_count = CASE
                            WHEN t.status <> $status THEN 0
                            WHEN coalesce(t.blocked_on, '') <> coalesce(
                                CASE
                                    WHEN $status <> 'in_progress' THEN NULL
                                    WHEN $blocked_on = '__KEEP__' THEN t.blocked_on
                                    WHEN $blocked_on = '' THEN NULL
                                    ELSE $blocked_on
                                END,
                                ''
                            ) THEN 0
                            ELSE coalesce(t.forced_continuation_count, 0)
                        END,
                        t.closeout_commit_sha = CASE
                            WHEN $commit_sha = '__KEEP__' THEN t.closeout_commit_sha
                            WHEN $commit_sha = '' THEN NULL
                            ELSE $commit_sha
                        END,
                        t.closeout_production_observation = CASE
                            WHEN $production_observation = '__KEEP__' THEN t.closeout_production_observation
                            WHEN $production_observation = '' THEN NULL
                            ELSE $production_observation
                        END,
                        t.closeout_evidence_note = CASE
                            WHEN $evidence_note = '__KEEP__' THEN t.closeout_evidence_note
                            WHEN $evidence_note = '' THEN NULL
                            ELSE $evidence_note
                        END,
                        t.updated_at = datetime()
                    RETURN t.id AS id
                """,
                    task_id=task_id,
                    status=status,
                    owner=owner,
                    result=result,
                    blocked_on=blocked_on_value,
                    commit_sha=commit_sha_value,
                    production_observation=production_observation_value,
                    evidence_note=evidence_note_value,
            )
        rec_record = rec.single()
        if rec_record is None:
            return False
        session.run("""
                MATCH (p:OrchProject)-[:HAS_PHASE]->(:OrchPhase)-[:HAS_TASK]->(t:OrchTask {id: $task_id})
                SET p.in_progress_heartbeat_at = CASE
                        WHEN $status = 'in_progress' THEN datetime()
                        WHEN $status IN ['completed', 'failed', 'interrupted'] THEN ''
                        ELSE p.in_progress_heartbeat_at
                    END,
                    p.status = CASE
                        WHEN $status = 'in_progress' THEN 'in_progress'
                        WHEN $status IN ['completed', 'failed', 'interrupted']
                             AND p.status = 'in_progress'
                             AND NOT EXISTS {
                                 MATCH (p)-[:HAS_PHASE]->(:OrchPhase)-[:HAS_TASK]->(other:OrchTask)
                                 WHERE other.id <> $task_id
                                   AND coalesce(other.status, 'pending') = 'in_progress'
                             } THEN 'active'
                        ELSE p.status
                    END,
                    p.updated_at = datetime()
        """, task_id=task_id, status=status)
        return True


def get_task(task_id: str,
             config: Optional[OrchConfig] = None) -> Optional[Dict[str, Any]]:
    """Return one OrchTask node as a plain dict."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        result = session.run("""
                MATCH (t:OrchTask {id: $task_id})
                RETURN t
            """, task_id=task_id).single()
        if not result:
            return None
        task = _normalize_map(dict(result["t"]))
        task["forced_continuation_count"] = int(task.get("forced_continuation_count", 0) or 0)
        return task


def check_phase_complete(phase_id: str,
                         config: Optional[OrchConfig] = None) -> bool:
    """Check if all tasks in a phase are completed. If so, mark phase completed."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        result = session.run("""
                MATCH (ph:OrchPhase {id: $phase_id})-[:HAS_TASK]->(t:OrchTask)
                WITH ph,
                     count(t) AS total,
                     sum(CASE WHEN t.status = 'completed' THEN 1 ELSE 0 END) AS done
                WHERE total > 0 AND total = done AND ph.status <> 'completed'
                SET ph.status = 'completed', ph.completed_at = datetime()
                RETURN ph.id AS id
            """, phase_id=phase_id)
        rec = result.single()
        return rec is not None


def get_task_phase(task_id: str,
                   config: Optional[OrchConfig] = None) -> Optional[str]:
    """Get the phase ID for a given task."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        result = session.run("""
                MATCH (ph:OrchPhase)-[:HAS_TASK]->(t:OrchTask {id: $task_id})
                RETURN ph.id AS phase_id
            """, task_id=task_id)
        rec = result.single()
        return rec["phase_id"] if rec else None


def assign_task_to_phase(task_id: str, phase_id: str,
                         config: Optional[OrchConfig] = None) -> bool:
    """Ensure a task belongs to exactly one phase, re-parenting when needed."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        result = session.run("""
                MATCH (t:OrchTask {id: $task_id})
                MATCH (ph:OrchPhase {id: $phase_id})
                OPTIONAL MATCH (:OrchPhase)-[rel:HAS_TASK]->(t)
                DELETE rel
                MERGE (ph)-[:HAS_TASK]->(t)
                RETURN t.id AS task_id
            """, task_id=task_id, phase_id=phase_id)
        return result.single() is not None


def _normalize_value(value: Any) -> Any:
    iso = getattr(value, "iso_format", None)
    if callable(iso):
        return iso()
    if isinstance(value, list):
        return [_normalize_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize_value(item) for key, item in value.items()}
    return value


def _normalize_map(values: Dict[str, Any]) -> Dict[str, Any]:
    return {key: _normalize_value(value) for key, value in values.items()}


def get_project_summary(project_id: str,
                        config: Optional[OrchConfig] = None) -> Optional[Dict[str, Any]]:
    """Return a project with its phases, tasks, and per-phase task status counts."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        result = session.run("""
                MATCH (p:OrchProject {id: $project_id})
                OPTIONAL MATCH (p)-[:HAS_PHASE]->(ph:OrchPhase)
                OPTIONAL MATCH (ph)-[:HAS_TASK]->(t:OrchTask)
                WITH p, ph, t
                ORDER BY coalesce(t.priority, 999999999) ASC, t.created_at ASC
                WITH p, ph,
                     count(t) AS total_tasks,
                     sum(CASE WHEN coalesce(t.status, 'pending') = 'pending' THEN 1 ELSE 0 END) AS pending,
                     sum(CASE WHEN coalesce(t.status, 'pending') = 'in_progress' THEN 1 ELSE 0 END) AS in_progress,
                     sum(CASE WHEN coalesce(t.status, 'pending') = 'completed' THEN 1 ELSE 0 END) AS completed,
                     sum(CASE WHEN coalesce(t.status, 'pending') = 'failed' THEN 1 ELSE 0 END) AS failed,
                     collect(
                         CASE
                             WHEN t IS NULL THEN NULL
                             ELSE {
                                 id: t.id,
                                 description: t.description,
                                 status: t.status,
                                 owner: t.owner,
                                 priority: t.priority,
                                 blocked_on: t.blocked_on
                             }
                         END
                     ) AS tasks
                ORDER BY ph.order ASC, ph.name ASC
                RETURN p, collect(
                    CASE
                        WHEN ph IS NULL THEN NULL
                        ELSE {
                            phase: ph,
                            task_counts: {
                                total: total_tasks,
                                pending: pending,
                                in_progress: in_progress,
                                completed: completed,
                                failed: failed
                            },
                            tasks: tasks
                        }
                    END
                ) AS phases
            """, project_id=project_id)
        record = result.single()
        if not record:
            return None

        phases = []
        for item in record["phases"]:
            if item is None:
                continue
            phase = _normalize_map(dict(item["phase"]))
            tasks = []
            for task in item["tasks"]:
                if task is None:
                    continue
                tasks.append(_normalize_map(dict(task)))
            phases.append({
                "phase": phase,
                "task_counts": dict(item["task_counts"]),
                "tasks": tasks,
            })

        return {
            "project": _decode_project_node(dict(record["p"])),
            "phases": phases,
        }


def get_session_current_work(session_id: str,
                             config: Optional[OrchConfig] = None) -> Optional[Dict[str, Any]]:
    """Return the highest-priority in-progress task for a session with project context."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        result = session.run("""
                MATCH (p:OrchProject)-[:HAS_PHASE]->(ph:OrchPhase)-[:HAS_TASK]->(t:OrchTask)
                WHERE t.owner = $session_id AND t.status = 'in_progress'
                RETURN p.id AS project_id,
                       p.name AS project_name,
                       ph.id AS phase_id,
                       ph.name AS phase_name,
                       t.id AS top_task_id,
                       t.description AS top_task_desc
                ORDER BY coalesce(p.priority, 999999999) ASC, coalesce(t.priority, 999999999) ASC, ph.order ASC, t.created_at ASC
                LIMIT 1
            """, session_id=session_id)
        record = result.single()
        return dict(record) if record else None


def get_session_next_ready(session_id: str, exclude_task_id: Optional[str] = None,
                           project_id: Optional[str] = None,
                           config: Optional[OrchConfig] = None) -> Optional[Dict[str, Any]]:
    """Return the top ready task for a session, excluding a specific task if requested."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        result = session.run("""
                MATCH (proj:OrchProject)-[:HAS_PHASE]->(ph:OrchPhase)-[:HAS_TASK]->(t:OrchTask)
                WHERE coalesce(t.status, 'pending') = 'pending'
                  AND coalesce(t.owner, '') = $sess
                  AND coalesce(t.blocked_on, '') = ''
                  AND ($exclude_task_id IS NULL OR t.id <> $exclude_task_id)
                  AND ($project_id IS NULL OR proj.id = $project_id)
                  AND coalesce(proj.status, 'active') <> 'completed'
                OPTIONAL MATCH (t)-[:DEPENDS_ON]->(dep:OrchTask)
                WITH proj, ph, t, collect(dep) AS deps
                """ + _ready_task_clause(passthrough=["proj", "ph"]) + """
                RETURN t.id AS task_id, t.description AS description,
                       t.priority AS priority, t.owner AS owner,
                       t.blocked_on AS blocked_on,
                       proj.status AS project_status,
                       proj.user_stop_conditions AS project_user_stop_conditions,
                       proj.stop_reason_current AS project_stop_reason_current,
                       ph.id AS phase_id, ph.name AS phase_name,
                       proj.id AS project_id, proj.name AS project_name
                ORDER BY coalesce(proj.priority, 999999999) ASC,
                         coalesce(t.priority, 999999999) ASC,
                         t.created_at ASC
            """, sess=session_id, exclude_task_id=exclude_task_id, project_id=project_id)
        for record in result:
            candidate = dict(record)
            project = {
                "status": candidate.get("project_status"),
                "user_stop_conditions": _decode_json_field(candidate.get("project_user_stop_conditions"), []),
                "stop_reason_current": _decode_json_field(candidate.get("project_stop_reason_current"), None),
            }
            stop_state = _project_stop_reason_state(project)
            if candidate.get("project_status") == "stopped" and stop_state["valid"]:
                continue
            candidate.pop("project_status", None)
            candidate.pop("project_user_stop_conditions", None)
            candidate.pop("project_stop_reason_current", None)
            return candidate
        return None


def get_project_user_stop_conditions(project_id: str,
                                     config: Optional[OrchConfig] = None) -> Optional[List[Dict[str, Any]]]:
    """Return the project's configured versioned user-stop conditions."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        record = session.run("""
                MATCH (p:OrchProject {id: $project_id})
                RETURN p.user_stop_conditions AS user_stop_conditions
            """, project_id=project_id).single()
        if not record:
            return None
        return _normalize_user_stop_conditions(
            _decode_json_field(record["user_stop_conditions"], []),
            created_by="decoded",
        )


def set_project_user_stop_conditions(project_id: str, user_stop_conditions: List[Any],
                                     config: Optional[OrchConfig] = None,
                                     created_by: str = "unknown") -> List[Dict[str, Any]]:
    """Persist the project's configured versioned user-stop conditions."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    normalized = _normalize_user_stop_conditions(user_stop_conditions, created_by)
    with driver.session(database=cfg.neo4j_db) as session:
        record = session.run("""
                MATCH (p:OrchProject {id: $project_id})
                SET p.user_stop_conditions = $user_stop_conditions,
                    p.updated_at = datetime()
                RETURN p.user_stop_conditions AS user_stop_conditions
            """, project_id=project_id, user_stop_conditions=_json_encode(normalized)).single()
        if not record:
            raise ValueError(f"Project {project_id} not found")
        return _normalize_user_stop_conditions(
            _decode_json_field(record["user_stop_conditions"], []),
            created_by="decoded",
        )


def get_task_project(task_id: str,
                     config: Optional[OrchConfig] = None) -> Optional[Dict[str, Any]]:
    """Return the project context for a task."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        record = session.run("""
                MATCH (proj:OrchProject)-[:HAS_PHASE]->(:OrchPhase)-[:HAS_TASK]->(t:OrchTask {id: $task_id})
                RETURN proj.id AS project_id, proj.name AS project_name,
                       proj.user_stop_conditions AS user_stop_conditions
            """, task_id=task_id).single()
        if not record:
            return None
        result = dict(record)
        conditions = _normalize_user_stop_conditions(
            _decode_json_field(result.get("user_stop_conditions"), []),
            created_by="decoded",
        )
        result["user_stop_conditions"] = [cond["label"] for cond in _active_conditions(conditions)]
        return result


def ensure_default_project(config: Optional[OrchConfig] = None) -> str:
    """Ensure a default project and phase exist for ad-hoc tasks. Returns phase_id."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    default_priority = _next_project_priority("conductor", cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        session.run("""
                MERGE (p:OrchProject {id: 'default'})
                ON CREATE SET p.name = 'Default Project',
                              p.description = 'Auto-created for ad-hoc tasks',
                              p.created_at = datetime(), p.status = 'active'
                SET p.supervisor = coalesce(p.supervisor, 'conductor'),
                    p.priority = coalesce(p.priority, $priority),
                    p.migration_exempt = coalesce(p.migration_exempt, false),
                    p.user_stop_conditions = coalesce(p.user_stop_conditions, '[]'),
                    p.stop_reason_current = coalesce(p.stop_reason_current, ''),
                    p.stop_reason_history = coalesce(p.stop_reason_history, '[]'),
                    p.priority_history = coalesce(
                        p.priority_history,
                        $priority_history
                    )
                MERGE (ph:OrchPhase {id: 'default-main'})
                ON CREATE SET ph.name = 'Main', ph.order = 0, ph.status = 'active'
                MERGE (p)-[:HAS_PHASE]->(ph)
        """,
            priority=default_priority,
            priority_history=_json_encode([{
                "priority_before": None,
                "priority_after": default_priority,
                "set_by": "system",
                "set_at": _utc_now_iso(),
                "source_surface": "system",
                "reason": "default project bootstrap",
            }]),
        )
    return "default-main"


def get_session_supervised_projects(session_id: str,
                                    config: Optional[OrchConfig] = None) -> List[Dict[str, Any]]:
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        result = session.run("""
            MATCH (p:OrchProject)
            WHERE coalesce(p.supervisor, '') = $session_id
              AND coalesce(p.migration_exempt, false) = false
            RETURN p
            ORDER BY coalesce(p.priority, 999999999) ASC, p.created_at ASC
        """, session_id=session_id)
        return [_decode_project_node(dict(record["p"])) for record in result]
def get_project_ready_tasks(project_id: str, owner: Optional[str] = None,
                            config: Optional[OrchConfig] = None) -> List[Dict[str, Any]]:
    cfg = config or OrchConfig()
    project = _project_record(project_id, cfg)
    stop_state = _project_stop_reason_state(project)
    if project.get("status") == "completed":
        return []
    if project.get("status") == "stopped" and stop_state["valid"]:
        return []
    if stop_state["valid"]:
        return []
    owner_value = owner if owner is not None else str(project.get("supervisor") or "")
    if not owner_value:
        return []
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        result = session.run("""
            MATCH (p:OrchProject {id: $project_id})-[:HAS_PHASE]->(ph:OrchPhase)-[:HAS_TASK]->(t:OrchTask)
            WHERE coalesce(t.status, 'pending') = 'pending'
              AND coalesce(t.owner, '') = $owner
              AND coalesce(t.blocked_on, '') = ''
            OPTIONAL MATCH (t)-[:DEPENDS_ON]->(dep:OrchTask)
            WITH ph, t, collect(dep) AS deps
            """ + _ready_task_clause(passthrough=["ph"]) + """
            RETURN t.id AS id,
                   t.description AS description,
                   t.priority AS priority,
                   t.owner AS owner,
                   t.forced_continuation_count AS forced_continuation_count,
                   t.task_type AS task_type,
                   t.required_credentials AS required_credentials,
                   t.credentials_available AS credentials_available,
                   t.permissions_available AS permissions_available,
                   ph.id AS phase_id,
                   ph.name AS phase_name
            ORDER BY coalesce(t.priority, 999999999) ASC, t.created_at ASC
        """, project_id=project_id, owner=owner_value)
        ready = []
        for record in result:
            task = _normalize_map(dict(record))
            task["forced_continuation_count"] = int(task.get("forced_continuation_count", 0) or 0)
            required = task.get("required_credentials")
            if required:
                if not bool(task.get("credentials_available")) and not bool(task.get("permissions_available")):
                    continue
            ready.append(task)
        return ready
def ready_work(project_id: str, session_id: Optional[str] = None,
               config: Optional[OrchConfig] = None) -> List[Dict[str, Any]]:
    return get_project_ready_tasks(project_id, owner=session_id, config=config)


def set_project_stop_reason(project_id: str, condition_id: str, condition_version: int,
                            detail: str, set_by: str,
                            config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    cfg = config or OrchConfig()
    project = _project_record(project_id, cfg)
    if ready_work(project_id, session_id=str(project.get("supervisor") or ""), config=cfg):
        raise ReadyWorkConflictError(f"ready_work exists for project {project_id}")
    conditions = list(project.get("user_stop_conditions") or [])
    condition = _condition_lookup(conditions, condition_id, condition_version)
    if not condition:
        raise ConditionValidationError("condition_id/version not found")
    if condition.get("deprecated_at"):
        raise ConditionValidationError("condition_id is deprecated")
    entry = {
        "condition_id": condition["id"],
        "condition_version": int(condition["version"]),
        "label_snapshot": condition["label"],
        "detail": detail or "",
        "set_at": _utc_now_iso(),
        "set_by": set_by,
    }
    history_entry = {
        "action": "set",
        "condition_id": condition["id"],
        "condition_version": int(condition["version"]),
        "label_snapshot": condition["label"],
        "detail": detail or "",
        "set_by": set_by,
        "set_at": entry["set_at"],
    }
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        record = session.run("""
            MATCH (p:OrchProject {id: $project_id})
            SET p.stop_reason_current = $stop_reason_current,
                p.stop_reason_history = $stop_reason_history,
                p.status = 'stopped',
                p.updated_at = datetime()
            RETURN p.stop_reason_current AS stop_reason_current
        """,
            project_id=project_id,
            stop_reason_current=_json_encode(entry),
            stop_reason_history=_append_history(list(project.get("stop_reason_history") or []), history_entry),
        ).single()
        return _decode_json_field(record["stop_reason_current"], {})
def clear_project_stop_reason(project_id: str, cleared_by: str,
                              config: Optional[OrchConfig] = None) -> bool:
    cfg = config or OrchConfig()
    project = _project_record(project_id, cfg)
    current = project.get("stop_reason_current") or {}
    history_entry = {
        "action": "clear",
        "condition_id": current.get("condition_id"),
        "condition_version": current.get("condition_version"),
        "label_snapshot": current.get("label_snapshot"),
        "detail": current.get("detail", ""),
        "cleared_by": cleared_by,
        "cleared_at": _utc_now_iso(),
    }
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        record = session.run("""
            MATCH (p:OrchProject {id: $project_id})
            SET p.stop_reason_current = '',
                p.stop_reason_history = $stop_reason_history,
                p.status = CASE
                    WHEN p.status = 'stopped' THEN 'active'
                    ELSE p.status
                END,
                p.updated_at = datetime()
            RETURN p.id AS id
        """,
            project_id=project_id,
            stop_reason_history=_append_history(list(project.get("stop_reason_history") or []), history_entry),
        ).single()
        return record is not None
def update_project_priority(project_id: str, new_priority: int, set_by: str,
                            source_surface: str, reason: str,
                            config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    if not set_by or not source_surface or not reason.strip():
        raise PriorityAuditError("set_by, source_surface, and reason are required")
    cfg = config or OrchConfig()
    project = _project_record(project_id, cfg)
    old_priority = project.get("priority")
    history_entry = {
        "priority_before": old_priority,
        "priority_after": int(new_priority),
        "set_by": set_by,
        "set_at": _utc_now_iso(),
        "source_surface": source_surface,
        "reason": reason,
    }
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        record = session.run("""
            MATCH (p:OrchProject {id: $project_id})
            SET p.priority = $new_priority,
                p.priority_history = $priority_history,
                p.updated_at = datetime()
            RETURN p.priority AS priority, p.priority_history AS priority_history
        """,
            project_id=project_id,
            new_priority=int(new_priority),
            priority_history=_append_history(list(project.get("priority_history") or []), history_entry),
        ).single()
        return {
            "priority": int(record["priority"]),
            "priority_history": _decode_json_field(record["priority_history"], []),
        }
def add_project_condition(project_id: str, label: str, created_by: str,
                          config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    cfg = config or OrchConfig()
    project = _project_record(project_id, cfg)
    conditions = list(project.get("user_stop_conditions") or [])
    condition = _build_condition(label.strip(), created_by)
    conditions.append(condition)
    set_project_user_stop_conditions(project_id, conditions, config=cfg, created_by=created_by)
    return condition


def edit_project_condition(project_id: str, condition_id: str, label: str, edited_by: str,
                           config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    cfg = config or OrchConfig()
    project = _project_record(project_id, cfg)
    conditions = list(project.get("user_stop_conditions") or [])
    active = _condition_lookup(_active_conditions(conditions), condition_id)
    if not active:
        raise ConditionValidationError("active condition not found")
    updated_conditions: List[Dict[str, Any]] = []
    now_iso = _utc_now_iso()
    for condition in conditions:
        if condition["id"] == condition_id and int(condition["version"]) == int(active["version"]):
            mutated = dict(condition)
            mutated["deprecated_at"] = now_iso
            updated_conditions.append(mutated)
        else:
            updated_conditions.append(condition)
    new_condition = _build_condition(
        label.strip(),
        edited_by,
        condition_id=condition_id,
        version=int(active["version"]) + 1,
        replaces_id=condition_id,
    )
    updated_conditions.append(new_condition)
    set_project_user_stop_conditions(project_id, updated_conditions, config=cfg, created_by=edited_by)
    return new_condition


def preflight_supervisor_orphan_check(config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        result = session.run("""
            MATCH (p:OrchProject)
            WHERE coalesce(p.status, 'active') = 'active'
              AND coalesce(p.migration_exempt, false) = false
              AND (p.supervisor IS NULL OR p.supervisor = '' OR p.supervisor = 'unassigned' OR p.supervisor = 'unknown')
            RETURN p.id AS project_id, p.name AS name, p.status AS status, p.supervisor AS supervisor
            ORDER BY p.id
        """)
        rows = [dict(record) for record in result]
        return {"ok": len(rows) == 0, "count": len(rows), "rows": rows}
def get_session_stop_status(session_id: str,
                            config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    cfg = config or OrchConfig()
    projects = get_session_supervised_projects(session_id, config=cfg)
    if not projects:
        return {"projects": [], "decision": {"can_stop": True}}
    project_statuses: List[Dict[str, Any]] = []
    decision: Dict[str, Any] = {"can_stop": True}
    for project in projects:
        stop_state = _project_stop_reason_state(project)
        ready_tasks = ready_work(project["id"], session_id=session_id, config=cfg)
        available_conditions = [_condition_view(cond) for cond in _active_conditions(project.get("user_stop_conditions", []))]
        stop_reason = project.get("stop_reason_current")
        project_statuses.append({
            "project_id": project["id"],
            "priority": project.get("priority"),
            "status": project.get("status"),
            "stop_reason": stop_reason,
            "stop_reason_orphaned": stop_state["orphaned"],
            "available_conditions": available_conditions,
            "ready_task_count": len(ready_tasks),
        })
        if ready_tasks:
            decision = {
                "can_stop": False,
                "wake_type": "WAKE_WITH_QUEUE",
                "wake_reason": f"ready_work:{project['id']}",
            }
            break
        if project.get("status") == "completed":
            continue
        if stop_state["valid"]:
            continue
        if stop_state["deprecated_only"]:
            continue
        if project.get("status") == "in_progress":
            continue
        decision = {
            "can_stop": False,
            "wake_type": "WAKE_REASON_REQUIRED",
            "wake_reason": f"stop_reason_required:{project['id']}",
        }
        break
    return {"projects": project_statuses, "decision": decision}


def set_session_pause(session_id: str, pause_source: str, pause_reason: str,
                      pause_expires_at: Optional[str] = None,
                      paused_by: Optional[str] = None,
                      config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    if pause_source not in _PAUSE_SOURCES:
        raise PauseValidationError("pause_source invalid")
    cfg = config or OrchConfig()
    from .config import get_redis_sync
    r = get_redis_sync(cfg)
    meta = {
        "paused_by": paused_by or session_id,
        "paused_at": _utc_now_iso(),
        "pause_source": pause_source,
        "pause_reason": pause_reason or "",
        "pause_expires_at": pause_expires_at,
    }
    prefix = os.environ.get("NOTIFY_KEY_PREFIX", "taey")
    r.set(f"{prefix}:{session_id}:pause", "1")
    r.set(f"{prefix}:{session_id}:pause_meta", _json_encode(meta))
    return meta


def clear_session_pause(session_id: str, cleared_by: str,
                        config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    cfg = config or OrchConfig()
    from .config import get_redis_sync
    r = get_redis_sync(cfg)
    prefix = os.environ.get("NOTIFY_KEY_PREFIX", "taey")
    meta_raw = r.get(f"{prefix}:{session_id}:pause_meta")
    meta = _decode_json_field(meta_raw, {})
    meta["cleared_by"] = cleared_by
    meta["cleared_at"] = _utc_now_iso()
    r.delete(f"{prefix}:{session_id}:pause")
    r.delete(f"{prefix}:{session_id}:pause_meta")
    return meta


def get_agent_tasks(agent_id: str, config: Optional[OrchConfig] = None) -> List[Dict[str, Any]]:
    """Get tasks owned by an agent."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        result = session.run("""
                MATCH (t:OrchTask)
                WHERE t.owner = $agent_id
                  AND t.status IN ['pending', 'in_progress']
                RETURN t.id AS id, t.description AS description,
                       t.status AS status, t.priority AS priority
                ORDER BY coalesce(t.priority, 999999999) ASC
                LIMIT 10
            """, agent_id=agent_id)
        return [dict(r) for r in result]


def create_question(question_id: str, text: str, context: str = "",
                    task_id: str = "", asked_by: str = "",
                    config: Optional[OrchConfig] = None) -> str:
    """Create an OrchQuestion node linked to a task."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        result = session.run("""
            MERGE (q:OrchQuestion {id: $id})
            SET q.text = $text,
                q.context = $context,
                q.task_id = $task_id,
                q.asked_by = $asked_by,
                q.status = 'open',
                q.created_at = datetime()
            WITH q
            OPTIONAL MATCH (t:OrchTask {id: $task_id})
            FOREACH (_ IN CASE WHEN $task_id <> '' AND t IS NOT NULL THEN [1] ELSE [] END |
                MERGE (q)-[:CONCERNS_TASK]->(t)
            )
            RETURN q.id AS id,
                   CASE
                       WHEN $task_id = '' THEN true
                       ELSE t IS NOT NULL
                   END AS task_exists
        """, id=question_id, text=text, context=context,
            task_id=task_id, asked_by=asked_by)
        record = result.single()
        if not record:
            raise TaskWriteError(f"Unable to create question {question_id}")
        if task_id and not record["task_exists"]:
            raise TaskWriteError(f"Task {task_id} not found for question {question_id}")
        return record["id"]
def answer_question(question_id: str, answer: str, answered_by: str,
                    config: Optional[OrchConfig] = None) -> bool:
    """Provide an answer to an open question."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        result = session.run("""
            MATCH (q:OrchQuestion {id: $id})
            SET q.answer = $answer,
                q.answered_by = $answered_by,
                q.status = 'answered',
                q.answered_at = datetime()
            RETURN q.id AS id
        """, id=question_id, answer=answer, answered_by=answered_by)
        return result.single() is not None
def get_open_questions(config: Optional[OrchConfig] = None) -> List[Dict[str, Any]]:
    """Get all questions with status 'open'."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        result = session.run("""
            MATCH (q:OrchQuestion {status: 'open'})
            RETURN q.id AS id, q.text AS text, q.context AS context,
                   q.task_id AS task_id, q.asked_by AS asked_by,
                   q.created_at AS created_at
            ORDER BY q.created_at ASC
        """)
        return [dict(r) for r in result]
