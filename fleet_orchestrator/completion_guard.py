"""Completion write guards for supervised autonomous peer work."""
from __future__ import annotations

from typing import Any, Optional

from .config import OrchConfig, get_neo4j_driver
from .current_task_binding import current_task_matches


_AUTONOMOUS_PEER_SUFFIXES = ("-codex", "-gemini", "-grok", "-claude")
_PEER_TERMINAL_OUTCOMES = {
    "completed": "done",
    "failed": "error",
    "interrupted": "interrupted",
}


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


def peer_execution_binding_rejection(task_id: str,
                                     task_before: dict[str, Any],
                                     sender: str,
                                     status: str) -> Optional[dict[str, Any]]:
    """Reject supervised peer execution writes that are not backed by current_task."""
    peer = str(sender or "").strip()
    if not peer:
        return None
    supervisor = _autonomous_peer_supervisor(peer)
    if not supervisor:
        return None

    normalized_status = str(status or "").strip().lower()
    if normalized_status not in {"in_progress", "completed", "failed", "interrupted"}:
        return None

    owner = str(task_before.get("owner") or "").strip()
    dispatched_to = str(task_before.get("dispatched_to") or "").strip()
    bound_to_task = current_task_matches(peer, task_id)
    controls_delegated_execution = (
        owner == peer
        and bool(dispatched_to)
        and dispatched_to != peer
    )

    if normalized_status == "in_progress":
        if owner == peer:
            return None
        if dispatched_to == peer and bound_to_task:
            return None
        return {
            "ok": False,
            "error": (
                "unbound peer execution rejected: a peer may start only a task it owns "
                "or a task explicitly dispatched to it with a live current_task binding"
            ),
            "task_id": task_id,
            "status": task_before.get("status"),
            "owner": owner,
            "dispatched_to": dispatched_to,
            "sender": peer,
            "next_step": (
                f"Use `taey-task dispatch {task_id} {peer}` from the supervisor, "
                f"or inspect `taey-task status {task_id}` before retrying."
            ),
        }

    if controls_delegated_execution:
        return None

    if not bound_to_task:
        return {
            "ok": False,
            "error": (
                "unbound peer outcome rejected: the sender does not hold the task's "
                "current_task binding"
            ),
            "task_id": task_id,
            "status": task_before.get("status"),
            "owner": owner,
            "dispatched_to": dispatched_to,
            "sender": peer,
            "next_step": (
                f"If this is dispatched peer work, call "
                "`taey-task outcome <done|error|interrupted> --details '<summary>'`; "
                "otherwise the supervisor must close the task after audit."
            ),
        }

    return None


def peer_self_completion_rejection(task_id: str,
                                   task_before: dict[str, Any],
                                   sender: str,
                                   status: str,
                                   *,
                                   config: OrchConfig) -> Optional[dict[str, Any]]:
    """Return a 409 payload when a supervised peer tries to close its own task."""
    normalized_status = str(status or "").strip().lower()
    outcome_command = _PEER_TERMINAL_OUTCOMES.get(normalized_status)
    if outcome_command is None:
        return None
    peer = str(sender or "").strip()
    if not peer:
        return None
    supervisor = _autonomous_peer_supervisor(peer)
    if not supervisor:
        return None

    dispatched_to = str(task_before.get("dispatched_to") or "").strip()
    owner = str(task_before.get("owner") or "").strip()
    is_executor = peer == dispatched_to or (not dispatched_to and peer == owner)
    if not is_executor:
        return None

    project_supervisor = _task_project_supervisor(task_id, config)
    if project_supervisor != supervisor:
        return None

    return {
        "ok": False,
        "error": (
            f"supervised autonomous peers must report terminal outcomes with `taey-task outcome {outcome_command}`; "
            "the supervisor closes the task after audit"
        ),
        "task_id": task_id,
        "status": task_before.get("status"),
        "supervisor": project_supervisor,
        "next_step": f"taey-task outcome {outcome_command} --details '<short outcome summary>'",
    }


def completion_producer_for_status_update(task_id: str,
                                          task_before: dict[str, Any],
                                          sender: str,
                                          status: str,
                                          *,
                                          owner: str,
                                          completion_evidence: Optional[dict[str, Any]],
                                          config: OrchConfig) -> str:
    """Return the producer identity passed into completion evidence verification."""
    actor = str(sender or "").strip()
    owner_value = str(owner or task_before.get("owner") or "").strip()
    default = actor or owner_value
    if str(status or "").strip().lower() != "completed":
        return default
    if not actor or not owner_value or actor == owner_value:
        return default
    if not isinstance(completion_evidence, dict) or "supervisor_verification" not in completion_evidence:
        return default
    if completion_evidence.get("commit_sha") or completion_evidence.get("loop_proof"):
        return default

    project_supervisor = _task_project_supervisor(task_id, config)
    if project_supervisor == actor:
        return owner_value
    return default
