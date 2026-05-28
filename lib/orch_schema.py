"""
Neo4j Orchestration Schema

Creates task DAG schema in the neo4j database with Orch-prefixed labels
to isolate from memory infrastructure (ISMA, HMM, Weaviate).

Label convention: OrchProject, OrchPhase, OrchTask, OrchFileOwnership
(memory labels: ISMAExchange, HMMTile, HMMMotif, Message, ChatSession)
"""

import json
import os
import subprocess
import sys
import time
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
]


_ZERO_DEP_READY_CYPHER = """
MATCH (t:OrchTask {id: $task_id})
WHERE coalesce(t.owner, '') <> ''
  AND NOT EXISTS {
      MATCH (t)-[:DEPENDS_ON]->(:OrchTask)
  }
RETURN t.id AS task_id,
       t.owner AS owner,
       t.description AS description
"""


def _fleet_redis_connect():
    for path in (
        "/usr/local/lib/claude-code-fleet-notify",
        "/home/mira/claude-code-fleet-notify",
    ):
        if os.path.isdir(path) and path not in sys.path:
            sys.path.insert(0, path)
    from identity import redis_connect  # type: ignore
    return redis_connect()


def _state_key(node_id: str, suffix: str) -> str:
    prefix = os.environ.get("NOTIFY_KEY_PREFIX", "taey")
    return f"{prefix}:{node_id}:{suffix}"


def _send_wake(owner: str, body: str) -> None:
    cli = "/usr/local/bin/taey-notify"
    if os.path.isfile(cli) and os.access(cli, os.X_OK):
        result = subprocess.run(
            [cli, owner, body, "--from", "orch-create", "--type", "wake", "--priority", "normal"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stderr.strip() or "taey-notify failed")
        return

    redis_client = _fleet_redis_connect()
    payload = json.dumps({
        "from": "orch-create",
        "type": "wake",
        "body": body,
        "priority": "normal",
        "msg_id": f"orch-create-zero-dep-{owner}-{int(time.time())}",
        "timestamp": time.time(),
    })
    redis_client.lpush(_state_key(owner, "inbox"), payload)


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

    try:
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
    finally:
        pass  # Driver is singleton; do not close

    return results


def create_project(project_id: str, name: str, description: str = "",
                   source_path: Optional[str] = None,
                   source_sha256: Optional[str] = None,
                   source_kind: Optional[str] = None,
                   ingested_at: Optional[str] = None,
                   ingested_by: Optional[str] = None,
                   config: Optional[OrchConfig] = None) -> str:
    """Create an OrchProject node."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            result = session.run("""
                MERGE (p:OrchProject {id: $id})
                ON CREATE SET p.created_at = datetime(), p.status = 'active'
                SET p.name = $name,
                    p.description = $description,
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
                source_path=source_path,
                source_sha256=source_sha256,
                source_kind=source_kind,
                ingested_at=ingested_at,
                ingested_by=ingested_by,
            )
            return result.single()["id"]
    finally:
        pass  # Driver is singleton; do not close


def create_phase(project_id: str, phase_id: str, name: str,
                 order: int = 0, config: Optional[OrchConfig] = None) -> str:
    """Create an OrchPhase linked to a project."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            result = session.run("""
                MATCH (p:OrchProject {id: $project_id})
                MERGE (ph:OrchPhase {id: $phase_id})
                ON CREATE SET ph.created_at = datetime(), ph.status = 'pending'
                SET ph.name = $name, ph.order = $order
                MERGE (p)-[:HAS_PHASE]->(ph)
                RETURN ph.id AS id
            """, project_id=project_id, phase_id=phase_id, name=name, order=order)
            return result.single()["id"]
    finally:
        pass  # Driver is singleton; do not close


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
    estimated_tokens: int = 50_000,
    wake_owner_if_ready: bool = True,
    config: Optional[OrchConfig] = None,
) -> str:
    """Create an OrchTask linked to a phase."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            result = session.run("""
                MATCH (ph:OrchPhase {id: $phase_id})
                MERGE (t:OrchTask {id: $task_id})
                ON CREATE SET t.created_at = datetime(),
                              t.status = 'pending',
                              t.owner = $owner
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
                    t.estimated_tokens = $estimated_tokens
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
                estimated_tokens=estimated_tokens,
            )
            created_id = result.single()["id"]
        if wake_owner_if_ready:
            _wake_owner_for_zero_dep_task(created_id, cfg)
        return created_id
    finally:
        pass  # Driver is singleton; do not close


def add_dependency(task_id: str, depends_on_id: str,
                   config: Optional[OrchConfig] = None) -> bool:
    """Create DEPENDS_ON relationship between tasks."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            session.run("""
                MATCH (t:OrchTask {id: $task_id})
                MATCH (dep:OrchTask {id: $depends_on_id})
                MERGE (t)-[:DEPENDS_ON]->(dep)
            """, task_id=task_id, depends_on_id=depends_on_id)
            return True
    finally:
        pass  # Driver is singleton; do not close


def get_ready_tasks(config: Optional[OrchConfig] = None) -> List[Dict[str, Any]]:
    """Get tasks that are pending with all dependencies satisfied."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            result = session.run("""
                MATCH (t:OrchTask {status: 'pending'})
                WHERE NOT EXISTS {
                    MATCH (t)-[:DEPENDS_ON]->(dep:OrchTask)
                    WHERE dep.status <> 'completed'
                }
                RETURN t.id AS id, t.description AS description,
                       t.priority AS priority, t.owner AS owner,
                       t.capability_tags AS capability_tags,
                       t.file_blast_radius AS file_blast_radius,
                       t.estimated_tokens AS estimated_tokens
                ORDER BY t.priority DESC
            """)
            return [dict(r) for r in result]
    finally:
        pass  # Driver is singleton; do not close


def update_task_status(task_id: str, status: str, owner: str = "",
                       result: Optional[str] = None,
                       blocked_on: Optional[str] = None,
                       config: Optional[OrchConfig] = None) -> bool:
    """Update task status, owner, and optional result."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    blocked_on_value = "__KEEP__" if blocked_on is None else blocked_on
    try:
        with driver.session(database=cfg.neo4j_db) as session:
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
                        t.updated_at = datetime()
                    RETURN t.id AS id
                """, task_id=task_id, status=status, owner=owner, blocked_on=blocked_on_value)
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
                        t.updated_at = datetime()
                    RETURN t.id AS id
                """, task_id=task_id, status=status, owner=owner, result=result,
                     blocked_on=blocked_on_value)
            return rec.single() is not None
    finally:
        pass  # Driver is singleton; do not close


def get_task(task_id: str,
             config: Optional[OrchConfig] = None) -> Optional[Dict[str, Any]]:
    """Return one OrchTask node as a plain dict."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            result = session.run("""
                MATCH (t:OrchTask {id: $task_id})
                RETURN t
            """, task_id=task_id).single()
            if not result:
                return None
            return _normalize_map(dict(result["t"]))
    finally:
        pass  # Driver is singleton; do not close


def check_phase_complete(phase_id: str,
                         config: Optional[OrchConfig] = None) -> bool:
    """Check if all tasks in a phase are completed. If so, mark phase completed."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    try:
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
    finally:
        pass  # Driver is singleton; do not close


def get_task_phase(task_id: str,
                   config: Optional[OrchConfig] = None) -> Optional[str]:
    """Get the phase ID for a given task."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            result = session.run("""
                MATCH (ph:OrchPhase)-[:HAS_TASK]->(t:OrchTask {id: $task_id})
                RETURN ph.id AS phase_id
            """, task_id=task_id)
            rec = result.single()
            return rec["phase_id"] if rec else None
    finally:
        pass  # Driver is singleton; do not close


def assign_task_to_phase(task_id: str, phase_id: str,
                         config: Optional[OrchConfig] = None) -> bool:
    """Ensure a task belongs to exactly one phase, re-parenting when needed."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    try:
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
    finally:
        pass  # Driver is singleton; do not close


def _normalize_value(value: Any) -> Any:
    iso = getattr(value, "iso_format", None)
    if callable(iso):
        return iso()
    return value


def _normalize_map(values: Dict[str, Any]) -> Dict[str, Any]:
    return {key: _normalize_value(value) for key, value in values.items()}


def get_project_summary(project_id: str,
                        config: Optional[OrchConfig] = None) -> Optional[Dict[str, Any]]:
    """Return a project with its phases and per-phase task status counts."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            result = session.run("""
                MATCH (p:OrchProject {id: $project_id})
                OPTIONAL MATCH (p)-[:HAS_PHASE]->(ph:OrchPhase)
                OPTIONAL MATCH (ph)-[:HAS_TASK]->(t:OrchTask)
                WITH p, ph,
                     count(t) AS total_tasks,
                     sum(CASE WHEN t.status = 'pending' THEN 1 ELSE 0 END) AS pending,
                     sum(CASE WHEN t.status = 'in_progress' THEN 1 ELSE 0 END) AS in_progress,
                     sum(CASE WHEN t.status = 'completed' THEN 1 ELSE 0 END) AS completed,
                     sum(CASE WHEN t.status = 'failed' THEN 1 ELSE 0 END) AS failed
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
                            }
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
                phases.append({
                    "phase": phase,
                    "task_counts": dict(item["task_counts"]),
                })

            return {
                "project": _normalize_map(dict(record["p"])),
                "phases": phases,
            }
    finally:
        pass  # Driver is singleton; do not close


def get_session_current_work(session_id: str,
                             config: Optional[OrchConfig] = None) -> Optional[Dict[str, Any]]:
    """Return the highest-priority in-progress task for a session with project context."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    try:
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
                ORDER BY t.priority DESC, ph.order ASC, t.created_at ASC
                LIMIT 1
            """, session_id=session_id)
            record = result.single()
            return dict(record) if record else None
    finally:
        pass  # Driver is singleton; do not close


def get_session_next_ready(session_id: str, exclude_task_id: Optional[str] = None,
                           config: Optional[OrchConfig] = None) -> Optional[Dict[str, Any]]:
    """Return the top ready task for a session, excluding a specific task if requested."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            result = session.run("""
                MATCH (t:OrchTask)
                WHERE t.status = 'pending'
                  AND (t.owner = $sess OR t.owner = '' OR t.owner IS NULL)
                  AND ($exclude_task_id IS NULL OR t.id <> $exclude_task_id)
                  AND NOT EXISTS {
                      MATCH (t)-[:DEPENDS_ON]->(dep:OrchTask)
                      WHERE dep.status <> 'completed'
                  }
                OPTIONAL MATCH (ph:OrchPhase)-[:HAS_TASK]->(t)
                OPTIONAL MATCH (proj:OrchProject)-[:HAS_PHASE]->(ph)
                RETURN t.id AS task_id, t.description AS description,
                       t.priority AS priority, t.owner AS owner,
                       t.blocked_on AS blocked_on,
                       ph.id AS phase_id, ph.name AS phase_name,
                       proj.id AS project_id, proj.name AS project_name
                ORDER BY (CASE WHEN t.owner = $sess THEN 1 ELSE 0 END) DESC,
                         t.priority DESC
                LIMIT 1
            """, sess=session_id, exclude_task_id=exclude_task_id).single()
            return dict(result) if result else None
    finally:
        pass  # Driver is singleton; do not close


def ensure_default_project(config: Optional[OrchConfig] = None) -> str:
    """Ensure a default project and phase exist for ad-hoc tasks. Returns phase_id."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            session.run("""
                MERGE (p:OrchProject {id: 'default'})
                ON CREATE SET p.name = 'Default Project',
                              p.description = 'Auto-created for ad-hoc tasks',
                              p.created_at = datetime(), p.status = 'active'
                MERGE (ph:OrchPhase {id: 'default-main'})
                ON CREATE SET ph.name = 'Main', ph.order = 0, ph.status = 'active'
                MERGE (p)-[:HAS_PHASE]->(ph)
            """)
        return "default-main"
    finally:
        pass  # Driver is singleton; do not close


def get_agent_tasks(agent_id: str, config: Optional[OrchConfig] = None) -> List[Dict[str, Any]]:
    """Get tasks owned by an agent."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            result = session.run("""
                MATCH (t:OrchTask)
                WHERE t.owner = $agent_id
                  AND t.status IN ['pending', 'in_progress']
                RETURN t.id AS id, t.description AS description,
                       t.status AS status, t.priority AS priority
                ORDER BY t.priority DESC
                LIMIT 10
            """, agent_id=agent_id)
            return [dict(r) for r in result]
    finally:
        pass  # Driver is singleton; do not close


def create_question(question_id: str, text: str, context: str = "",
                    task_id: str = "", asked_by: str = "",
                    config: Optional[OrchConfig] = None) -> str:
    """Create an OrchQuestion node linked to a task."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            # Link to task if provided
            task_clause = ""
            if task_id:
                task_clause = """
                WITH q
                MATCH (t:OrchTask {id: $task_id})
                MERGE (q)-[:CONCERNS_TASK]->(t)
                """
            
            result = session.run(f"""
                MERGE (q:OrchQuestion {{id: $id}})
                SET q.text = $text,
                    q.context = $context,
                    q.task_id = $task_id,
                    q.asked_by = $asked_by,
                    q.status = 'open',
                    q.created_at = datetime()
                {task_clause}
                RETURN q.id AS id
            """, id=question_id, text=text, context=context, 
                task_id=task_id, asked_by=asked_by)
            return result.single()["id"]
    finally:
        pass


def answer_question(question_id: str, answer: str, answered_by: str,
                    config: Optional[OrchConfig] = None) -> bool:
    """Provide an answer to an open question."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    try:
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
    finally:
        pass


def get_open_questions(config: Optional[OrchConfig] = None) -> List[Dict[str, Any]]:
    """Get all questions with status 'open'."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            result = session.run("""
                MATCH (q:OrchQuestion {status: 'open'})
                RETURN q.id AS id, q.text AS text, q.context AS context,
                       q.task_id AS task_id, q.asked_by AS asked_by,
                       q.created_at AS created_at
                ORDER BY q.created_at ASC
            """)
            return [dict(r) for r in result]
    finally:
        pass
