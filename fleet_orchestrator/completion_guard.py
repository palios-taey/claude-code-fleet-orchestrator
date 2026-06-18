"""Completion write guards for supervised autonomous peer work."""
from __future__ import annotations

from typing import Any, Optional

from .config import OrchConfig, get_neo4j_driver


_AUTONOMOUS_PEER_SUFFIXES = ("-codex", "-gemini", "-grok", "-claude")


def _autonomous_peer_supervisor(session_id: str) -> Optional[str]:
    node = str(session_id or "").strip()
    for suffix in _AUTONOMOUS_PEER_SUFFIXES:
        if node.endswith(suffix):
            return node[: -len(suffix)]
    return None


def _task_project_supervisor(task_id: str, config: OrchConfig) -> Optional[str]:
    driver = get_neo4j_driver(config)
    with driver.session(database=config.neo4j_db) as session:
        record = session.run(
            """
            MATCH (p:OrchProject)-[:HAS_PHASE]->(:OrchPhase)-[:HAS_TASK]->(t:OrchTask {id: $task_id})
            RETURN p.supervisor AS supervisor
            """,
            task_id=task_id,
        ).single()
    return str(record["supervisor"]) if record and record["supervisor"] else None


def peer_self_completion_rejection(task_id: str,
                                   task_before: dict[str, Any],
                                   sender: str,
                                   status: str,
                                   *,
                                   config: OrchConfig) -> Optional[dict[str, Any]]:
    """Return a 409 payload when a supervised peer tries to close its own task."""
    if str(status or "").strip().lower() != "completed":
        return None
    peer = str(sender or "").strip()
    if not peer:
        return None
    supervisor = _autonomous_peer_supervisor(peer)
    if not supervisor:
        return None

    dispatched_to = str(task_before.get("dispatched_to") or "").strip()
    owner = str(task_before.get("owner") or "").strip()
    if peer not in {dispatched_to, owner}:
        return None

    project_supervisor = _task_project_supervisor(task_id, config)
    if project_supervisor != supervisor:
        return None

    return {
        "ok": False,
        "error": (
            "supervised autonomous peers must report completion with record_outcome('done'); "
            "the supervisor closes the task after audit"
        ),
        "task_id": task_id,
        "status": task_before.get("status"),
        "supervisor": project_supervisor,
        "next_step": f"record_outcome('{peer}', 'done', '<short outcome summary>')",
    }
