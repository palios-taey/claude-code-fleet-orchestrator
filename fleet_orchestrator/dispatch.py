"""Supervisor→worker dispatch primitive.

Assembles the mandatory wake packet, writes it to the worker's Redis inbox, AND records
``taey:<worker>:current_task`` so the worker's Stop hook can include
the task summary in its supervisor-notify when the worker finishes.

This is the orchestration-layer counterpart to fleet-notify's universal
Stop+notify. fleet-notify owns "what happens when a worker stops"; this
module owns "what gets recorded when a worker is given work" so the Stop
hook has something to report.

Public API:
    dispatch(worker, task_id, description, supervisor=None,
             prompt_body=None, priority="normal",
             is_bugfix=False, force=False) -> None

The ``supervisor`` argument is informational — it's stamped on the
current_task payload so the Stop hook knows who to address even when
the suffix-strip rule wouldn't reach them (e.g., multi-level trees).
For single-level use, leave it None and the Stop hook uses
``<worker-name>-<cli>`` suffix-strip → supervisor.

The dispatcher's identity is stored separately in the current_task payload
for clobber protection; ``force=True`` is the explicit escape hatch when a
supervisor intends to replace another dispatcher's live worker binding.

Usage from a Python supervisor session::

    from fleet_orchestrator.dispatch import dispatch

    dispatch(
        worker="worker-codex",
        task_id="example-task",
        description="Run the assigned worker task",
        prompt_body="Run the deployment's worker workflow.",
    )

CLI equivalent::

    orch-dispatch worker-codex example-task \\
        --description "Run the assigned worker task" \\
        --prompt-file /tmp/worker.prompt

Once dispatched, the worker receives a rendered wake packet via the released
``claude-code-fleet-notify`` daemon (idle=1 + inbox>0 → tmux inject). The packet
contains rules, refs, identity, operating context, and the original dispatch body.
This path does not depend on the optional wake-packet endpoint or session hooks.
When the worker stops, its Stop hook reads current_task and notifies
the supervisor with the task id + description + duration in the
peer_idle body.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional

from .config import OrchConfig, _parse_product_owner_map, _parse_session_ids, get_neo4j_session, notify_cli
from .causal_ledger import (
    UNKNOWN as CAUSAL_UNKNOWN,
    append_event as append_causal_event,
    build_actor_attestation,
    extract_reported_commit_sha,
)
from .context_assembler import (
    assemble as assemble_wake_packet,
    attach_proof_capsule as attach_wake_proof_capsule,
    build_packet as build_wake_packet,
    select_context as select_wake_context,
    size_report as wake_size_report,
    task_ref_receipt,
)
from .kb_context import KnowledgeBaseContextError
from .notify_state import redis_connect as _notify_redis_connect
from .notify_state import state_key as _notify_state_key
from .decision_receipt import maybe_emit_receipt as maybe_emit_decision_receipt
from .handoff_validation import mark_superseded_for_task
from .hook_installation import hook_installation_status
from .memory_tier import get_memory
from .orch_schema import completed_task_satisfies_dependents_cypher
from .rules_tier import get_rules
from .session_topology import control_principal_for_session, session_family
from .worker_liveness import register_worker_task_liveness
from .current_task_binding import (
    clear_matching_current_task,
    clear_session_current_task,
    decode_current_task,
    is_live_binding_status,
    task_status as _binding_task_status,
)
from .world_manifest import publish_world_manifest_v0

logger = logging.getLogger(__name__)

# Bounded Redis WATCH retry (grok ws2-state WATCH-livelock note): a hot current_task key
# must not let an optimistic-lock loop spin forever. ~8 attempts with linear backoff caps
# the worst case at well under a second, after which we give up rather than livelock.
_WATCH_MAX_ATTEMPTS = 8
_WATCH_BACKOFF_S = 0.02
_TERMINAL_TASK_STATUSES = {"completed", "failed", "interrupted"}
_DEPENDENCY_SATISFIED_CYPHER = completed_task_satisfies_dependents_cypher("dep")
_DEPENDENCIES_READY_CYPHER = f"""
NOT EXISTS {{
    MATCH (t)-[:DEPENDS_ON]->(dep:OrchTask)
    WHERE NOT {_DEPENDENCY_SATISFIED_CYPHER}
}}
"""


class BugLockActive(Exception):
    """Dispatch blocked because the target product is under an active bug lock."""


class OrchTaskNotReady(Exception):
    """Dispatch blocked because the OrchTask is not ready at claim time."""


class WorkerBusy(Exception):
    """Dispatch blocked because another live dispatcher already owns the worker slot."""


class ControlSeatTarget(WorkerBusy):
    """Dispatch blocked because a worker command targeted another control seat."""


class HooksNotInstalled(Exception):
    """Dispatch blocked because the target session has no managed notify hooks."""


class ChangesRequestedError(Exception):
    """Changes-requested rework could not be resolved to a concrete peer/task."""


def _base_session_name(worker: str) -> str:
    return session_family(worker, _parse_session_ids())


def _cli_for_worker(worker: str) -> str:
    for suffix, cli in (
        ("-codex", "codex"),
        ("-gemini", "gemini"),
        ("-grok", "grok"),
        ("-claude", "claude"),
    ):
        if worker.endswith(suffix):
            return cli
    return "claude"


def _resolve_product_id(worker: str) -> Optional[str]:
    return _parse_product_owner_map().get(_base_session_name(worker))


def _redis_connect():
    """Connect to Redis via the fleet-notify identity module."""
    return _notify_redis_connect()


def _state_key(node_id: str, suffix: str) -> str:
    return _notify_state_key(node_id, suffix)


def _decode_current_task(raw: Optional[str]) -> Optional[dict[str, Any]]:
    return decode_current_task(raw)


def _causal_event_id(row: Optional[dict[str, Any]]) -> str:
    event = (row or {}).get("event") if isinstance(row, dict) else {}
    if not isinstance(event, dict):
        return ""
    return str(event.get("event_id") or "")


def _session_roots_from_env() -> dict[str, str]:
    raw = os.environ.get("ORCH_SESSION_ROOTS", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        parsed = None
    if isinstance(parsed, dict):
        return {str(k).strip(): str(v).strip() for k, v in parsed.items() if str(k).strip() and str(v).strip()}
    roots: dict[str, str] = {}
    for part in raw.split(","):
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        if key.strip() and value.strip():
            roots[key.strip()] = value.strip()
    return roots


def _worker_work_snapshot(worker: str) -> dict[str, str]:
    roots = _session_roots_from_env()
    worktree = roots.get(worker) or roots.get(_base_session_name(worker))
    if not worktree:
        return {}
    snapshot = {"worktree": worktree, "repo": CAUSAL_UNKNOWN, "branch": CAUSAL_UNKNOWN}
    try:
        branch = subprocess.run(
            ["git", "-C", worktree, "branch", "--show-current"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
        if branch.returncode == 0 and branch.stdout.strip():
            snapshot["branch"] = branch.stdout.strip()
    except Exception:
        pass
    try:
        remote = subprocess.run(
            ["git", "-C", worktree, "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
        if remote.returncode == 0 and remote.stdout.strip():
            snapshot["repo"] = remote.stdout.strip()
    except Exception:
        pass
    return snapshot


def _attach_causal_metadata_to_current_task(
    r: Any,
    worker: str,
    task_id: str,
    binding_nonce: Optional[float],
    metadata: dict[str, Any],
) -> bool:
    key = _state_key(worker, "current_task")
    if not callable(getattr(r, "set", None)):
        return False
    try:
        current_task = _decode_current_task(r.get(key))
        if not isinstance(current_task, dict) or current_task.get("task_id") != task_id:
            return False
        if binding_nonce is not None and current_task.get("started_at") != binding_nonce:
            return False
        causal = current_task.get("causal") if isinstance(current_task.get("causal"), dict) else {}
        causal.update(metadata)
        current_task["causal"] = causal
        r.set(key, json.dumps(current_task))
        return True
    except Exception as exc:
        logger.warning("causal current_task metadata attach skipped worker=%s task=%s: %r", worker, task_id, exc)
        return False


def _causal_parent_events(current_task: Optional[dict[str, Any]]) -> list[str]:
    causal = (current_task or {}).get("causal") if isinstance(current_task, dict) else {}
    if not isinstance(causal, dict):
        return []
    parents = [
        causal.get("wake_delivered_event_id"),
        causal.get("wake_packet_event_id"),
        causal.get("dispatch_event_id"),
    ]
    return [str(parent) for parent in parents if isinstance(parent, str) and parent.strip()]


def _append_worker_outcome_causal_event(
    worker: str,
    outcome: str,
    details: Optional[str],
    current_task: Optional[dict[str, Any]],
    payload: dict[str, Any],
) -> Optional[str]:
    current_task_id = str((current_task or {}).get("task_id") or "").strip()
    if not current_task_id:
        return None
    causal = (current_task or {}).get("causal") if isinstance(current_task, dict) else {}
    if not isinstance(causal, dict):
        causal = {}
    reported_commit = extract_reported_commit_sha(details)
    row = append_causal_event(
        "worker_outcome_recorded",
        subject={"worker": worker, "task_id": current_task_id},
        parents=_causal_parent_events(current_task),
        actor_attestation_id=str(causal.get("attestation_id") or CAUSAL_UNKNOWN),
        packet_id=str(causal.get("packet_id") or CAUSAL_UNKNOWN),
        packet_provenance_hash=str(causal.get("packet_provenance_hash") or CAUSAL_UNKNOWN),
        payload={
            "outcome": outcome,
            "details": str(details or "")[:2000],
            "last_outcome": dict(payload),
            "reported_commit_sha": reported_commit or CAUSAL_UNKNOWN,
            "reported_commit_sha_register": "Observed" if reported_commit else CAUSAL_UNKNOWN,
            "identity_rule": "git author is not actor identity",
        },
    )
    return _causal_event_id(row) or None


def _append_dispatch_delivery_failed_causal_event(
    *,
    worker: str,
    task_id: str,
    supervisor: Optional[str],
    dispatch_event_id: str,
    wake_packet_event_id: str,
    attestation_id: Optional[str],
    packet_meta: Optional[dict[str, Any]],
    failure_stage: str,
    binding_nonce: Optional[float],
    rollback: str,
    returncode: Optional[int] = None,
    stdout: Optional[str] = None,
    stderr: Optional[str] = None,
    error: Optional[BaseException] = None,
) -> Optional[str]:
    meta = packet_meta or {}
    payload: dict[str, Any] = {
        "attestation_id": str(attestation_id or CAUSAL_UNKNOWN),
        "outcome": {
            "status": "delivery_failed",
            "failure_stage": failure_stage,
        },
        "rollback": rollback,
        "binding_nonce": binding_nonce,
    }
    if returncode is not None:
        payload["returncode"] = returncode
    if stdout is not None:
        payload["stdout"] = stdout[:2000]
    if stderr is not None:
        payload["stderr"] = stderr[:2000]
    if error is not None:
        payload["error"] = f"{error.__class__.__name__}: {error}"[:2000]
    parents = [wake_packet_event_id] if wake_packet_event_id else ([dispatch_event_id] if dispatch_event_id else [])
    try:
        row = append_causal_event(
            "dispatch_delivery_failed",
            subject={"worker": worker, "task_id": task_id, "supervisor": supervisor or CAUSAL_UNKNOWN},
            parents=parents,
            actor_attestation_id=str(attestation_id or CAUSAL_UNKNOWN),
            packet_id=str(meta.get("packet_id", "")),
            packet_provenance_hash=str(meta.get("provenance_hash", "")),
            payload=payload,
        )
    except Exception as exc:
        logger.warning(
            "dispatch_delivery_failed causal event append failed worker=%s task=%s stage=%s: %r",
            worker,
            task_id,
            failure_stage,
            exc,
        )
        return None
    return _causal_event_id(row) or None


def _current_task_status(task_id: str) -> Optional[str]:
    return _binding_task_status(task_id)


def _busy_current_task_error(worker: str, existing: Optional[dict[str, Any]],
                             dispatcher: Optional[str], task_id: str) -> Optional[WorkerBusy]:
    if not existing:
        return None
    existing_task_id = str(existing.get("task_id") or "").strip()
    incoming_task_id = str(task_id or "").strip()
    existing_dispatcher = str(existing.get("dispatcher") or existing.get("supervisor") or "").strip()
    incoming_dispatcher = str(dispatcher or "").strip()
    if existing_dispatcher == incoming_dispatcher and existing_task_id == incoming_task_id:
        return None
    status = _current_task_status(existing_task_id)
    if status is not None and not is_live_binding_status(status):
        return None
    display_status = status or "unknown"
    dispatcher_label = existing_dispatcher or "unknown"
    task_label = existing_task_id or "unknown-task"
    return WorkerBusy(f"worker busy with {dispatcher_label}:{task_label} ({display_status})")


def _bind_current_task_checked(r: Any, worker: str, current_task: dict[str, Any],
                               set_parent: bool, supervisor: Optional[str],
                               dispatcher: Optional[str],
                               *, force: bool = False) -> None:
    from redis import WatchError

    key = _state_key(worker, "current_task")
    for attempt in range(_WATCH_MAX_ATTEMPTS):
        with r.pipeline() as pipe:
            try:
                pipe.watch(key)
                existing = _decode_current_task(pipe.get(key))
                stale_existing: Optional[dict[str, Any]] = None
                if not force:
                    busy = _busy_current_task_error(
                        worker,
                        existing,
                        dispatcher,
                        str(current_task["task_id"]),
                    )
                    if busy:
                        pipe.unwatch()
                        raise busy
                    existing_task_id = str((existing or {}).get("task_id") or "").strip()
                    existing_status = _current_task_status(existing_task_id) if existing_task_id else None
                    if existing_task_id and existing_status is not None and not is_live_binding_status(existing_status):
                        stale_existing = {"task_id": existing_task_id, "status": existing_status}
                pipe.multi()
                pipe.delete(_state_key(worker, "last_outcome"))
                pipe.delete(_state_key("orch-watch-stuck", f"{worker}:{current_task['task_id']}"))
                pipe.set(key, json.dumps(current_task))
                if set_parent and supervisor:
                    pipe.set(_state_key(worker, "parent"), supervisor)
                pipe.execute()
                if stale_existing:
                    logger.warning(
                        "stale current_task binding cleared during dispatch worker=%s stale_task=%s status=%s new_task=%s",
                        worker,
                        stale_existing["task_id"],
                        stale_existing["status"],
                        current_task["task_id"],
                    )
                return
            except WatchError:
                if attempt == _WATCH_MAX_ATTEMPTS - 1:
                    raise RuntimeError(f"worker busy with changing current_task: {worker}")
                time.sleep(_WATCH_BACKOFF_S * (attempt + 1))
    raise RuntimeError(f"worker busy with changing current_task: {worker}")


def bind_current_task(
    worker: str,
    task_id: str,
    description: str,
    supervisor: Optional[str] = None,
    set_parent: bool = False,
    force: bool = False,
    guard_existing: bool = False,
    dispatcher: Optional[str] = None,
) -> float:
    """Write the canonical dispatch/current-task wire for ``worker``.

    This is the Redis-side half of the dispatch primitive, factored out so
    non-dispatch task flows (for example self-owned ``taey-task`` work) can
    mirror the exact same state shape that the Stop hook and orch-watch
    already understand.

    Returns the ``started_at`` nonce written into ``current_task``. dispatch()
    uses it as a claim-token so a later rollback only reverts THIS exact binding
    (not one a subsequent dispatch for the same worker may have rebound).
    """
    r = _redis_connect()
    current_task = {
        "task_id": task_id,
        "description": description,
        "supervisor": supervisor,
        "started_at": time.time(),
    }
    if dispatcher:
        current_task["dispatcher"] = dispatcher

    if guard_existing:
        _bind_current_task_checked(r, worker, current_task, set_parent, supervisor, dispatcher or supervisor, force=force)
    else:
        pipe = r.pipeline(transaction=True)
        pipe.delete(_state_key(worker, "last_outcome"))
        pipe.delete(_state_key("orch-watch-stuck", f"{worker}:{task_id}"))
        pipe.set(_state_key(worker, "current_task"), json.dumps(current_task))
        if set_parent and supervisor:
            pipe.set(_state_key(worker, "parent"), supervisor)
        pipe.execute()

    # Binding a task means the worker is working it — flip it to in_progress so
    # next-ready stops re-surfacing it.
    # Best-effort: no-op for ad-hoc tasks / already-claimed / dep-blocked.
    _mark_in_progress_best_effort(task_id, worker)
    register_worker_task_liveness(
        worker=worker,
        task_id=task_id,
        description=description,
        supervisor=supervisor,
        started_at=current_task["started_at"],
    )
    return current_task["started_at"]


def _orch_task_exists(task_id: str) -> bool:
    cfg = OrchConfig()
    with get_neo4j_session(cfg) as session:
        record = session.run(
            "MATCH (t:OrchTask {id: $task_id}) RETURN t.id AS id",
            task_id=task_id,
        ).single()
    return record is not None


def _claim_ready_orch_task(task_id: str, worker: str, *,
                           supervisor: Optional[str] = None,
                           force: bool = False) -> None:
    if not _orch_task_exists(task_id):
        return

    cfg = OrchConfig()
    supervisor_owner = control_principal_for_session(
        str(supervisor or "").strip(),
        cfg.session_ids,
    )
    owner = supervisor_owner or control_principal_for_session(
        worker,
        cfg.session_ids,
    )
    with get_neo4j_session(cfg) as session:
        record = session.run(
            f"""
            MATCH (t:OrchTask {{id: $task_id}})
            SET t._claim_lock = true
            WITH t, coalesce(t.status, 'pending') AS prior_status
            WHERE (
                  prior_status = 'pending'
                  OR (prior_status = 'completed' AND coalesce(t.recurring, false) = true)
                  OR ($force = true AND prior_status = 'in_progress')
              )
              AND {_DEPENDENCIES_READY_CYPHER}
            SET t.status = 'in_progress',
                t.owner = $owner,
                t.dispatched_to = $worker,
                t.blocked_on = NULL,
                t.reclaim_count = CASE
                    WHEN prior_status = 'completed' THEN coalesce(t.reclaim_count, 0) + 1
                    ELSE coalesce(t.reclaim_count, 0)
                END,
                t.last_reclaimed_at = CASE
                    WHEN prior_status = 'completed' THEN datetime()
                    ELSE t.last_reclaimed_at
                END,
                t.updated_at = datetime()
            RETURN t.id AS task_id
            """,
            task_id=task_id,
            worker=worker,
            owner=owner,
            force=bool(force),
        ).single()

        if record is not None:
            return

        detail = session.run(
            f"""
            MATCH (t:OrchTask {{id: $task_id}})
            OPTIONAL MATCH (t)-[:DEPENDS_ON]->(dep:OrchTask)
            RETURN coalesce(t.status, 'pending') AS status,
                   count(CASE WHEN dep IS NOT NULL AND NOT {_DEPENDENCY_SATISFIED_CYPHER} THEN 1 END) AS incomplete_deps
            """,
            task_id=task_id,
        ).single()

    if detail is None:
        return

    raise OrchTaskNotReady(
        f"ORCH_TASK_NOT_READY task={task_id} status={detail['status']} "
        f"incomplete_deps={detail['incomplete_deps']}"
    )


def _current_task_binding_candidates(task_id: str) -> set[str]:
    if not _orch_task_exists(task_id):
        return set()
    cfg = OrchConfig()
    with get_neo4j_session(cfg) as session:
        record = session.run(
            """
            MATCH (t:OrchTask {id: $task_id})
            RETURN coalesce(t.status, 'pending') AS status,
                   t.owner AS owner,
                   t.dispatched_to AS dispatched_to,
                   t.worker_liveness_worker AS worker_liveness_worker
            """,
            task_id=task_id,
        ).single()
    if not record:
        return set()
    values = record.data()
    if str(values.get("status") or "").strip().lower() != "in_progress":
        return set()
    return {
        str(candidate).strip()
        for candidate in (
            values.get("owner"),
            values.get("dispatched_to"),
            values.get("worker_liveness_worker"),
        )
        if str(candidate or "").strip()
    }


def _clear_replaced_force_bindings(task_id: str, previous_workers: set[str], worker: str) -> None:
    stale_workers = sorted(previous_workers - {worker})
    if not stale_workers:
        return
    try:
        r = _redis_connect()
    except Exception as exc:
        logger.warning(
            "force dispatch reclaim could not connect to Redis for stale binding clear task=%s workers=%s: %r",
            task_id,
            stale_workers,
            exc,
        )
        return
    for stale_worker in stale_workers:
        clear_matching_current_task(
            stale_worker,
            task_id,
            redis_client=r,
            reason=f"dispatch-force-reclaim:{worker}",
        )


def _binding_is_ours(raw: Optional[str], task_id: str, binding_nonce: Optional[float]) -> bool:
    """True iff a current_task JSON blob is THIS dispatch's binding (task_id +
    started_at nonce). A non-matching/absent/garbage blob means a later dispatch
    rebound the worker (or it was cleared) -- not ours."""
    if not raw:
        return False
    try:
        cur = json.loads(raw)
    except (TypeError, ValueError):
        return False
    if not isinstance(cur, dict) or cur.get("task_id") != task_id:
        return False
    return binding_nonce is None or cur.get("started_at") == binding_nonce


def _rollback_claim_only(worker: str, task_id: str) -> None:
    """Undo the OrchTask claim made before a guarded bind refusal."""
    try:
        if _orch_task_exists(task_id):
            cfg = OrchConfig()
            with get_neo4j_session(cfg) as session:
                session.run(
                    """
                    MATCH (t:OrchTask {id: $task_id})
                    WHERE t.status = 'in_progress'
                      AND (
                          coalesce(t.dispatched_to, '') = $worker
                          OR (coalesce(t.dispatched_to, '') = '' AND coalesce(t.owner, '') = $worker)
                      )
                    SET t.status = 'pending',
                        t.dispatched_to = NULL,
                        t.updated_at = datetime()
                    """,
                    task_id=task_id,
                    worker=worker,
                )
    except Exception as exc:
        logger.warning(
            "dispatch rollback: neo claim-only revert FAILED worker=%s task=%s "
            "(task may linger in_progress as a phantom): %r", worker, task_id, exc)


def _rollback_claim(worker: str, task_id: str, binding_nonce: Optional[float]) -> None:
    """Undo a claim+bind when wake delivery fails, so a failed dispatch leaves
    READY work (pending) rather than a phantom 'live resolver'.

    dispatch() mutates state (claim -> status=in_progress; bind -> Redis
    current_task) BEFORE the wake (taey-notify) is delivered. If the wake fails,
    the task would otherwise stay in_progress -- counted live by
    _LIVE_RESOLVER_STATUSES -- with nothing actually working it, so a supervisor
    blocked_on it stops and the work dead-locks.

    Identity-guarded (grok PR#25 audit V1/V2): ``binding_nonce`` is the claim
    token written by bind_current_task. We revert ONLY if the worker's live
    current_task is still THIS dispatch's binding (same task_id + nonce). If a
    later same-worker dispatch has rebound current_task, this dispatch was
    superseded -- we touch neither the task status nor the (newer) binding.

    Observability-first (V4): the rollback never raises (the caller is already
    raising the dispatch failure) but every internal failure is LOGGED, so a
    cleanup that leaves the phantom is visible rather than silent.
    """
    from redis import WatchError

    key = _state_key(worker, "current_task")

    # 1. Read the live binding and classify it relative to THIS dispatch.
    try:
        r = _redis_connect()
        raw = r.get(key)
    except Exception as exc:
        logger.warning(
            "dispatch rollback: could not read current_task to verify ownership "
            "worker=%s task=%s -- NOT reverting blindly: %r", worker, task_id, exc)
        return
    try:
        cur = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        cur = None
    live_task = cur.get("task_id") if isinstance(cur, dict) else None
    live_nonce = cur.get("started_at") if isinstance(cur, dict) else None
    is_ours = live_task == task_id and (binding_nonce is None or live_nonce == binding_nonce)
    is_reclaim = live_task == task_id and not is_ours  # OUR task, but a NEWER dispatch rebound it

    if is_reclaim:
        # A newer dispatch re-claimed this same task on this worker. It is now
        # legitimately in_progress under someone else's wake -- reverting would
        # clobber that live re-claim (grok V1). Leave status AND binding alone.
        return

    # 2. Revert the orch task to pending. Safe in BOTH remaining cases: it is
    #    task_id + owner + status='in_progress' guarded, so it only ever touches
    #    THIS task's failed claim -- never a different task the worker has since
    #    moved to (grok V2: the worker may be on T2 now; reverting T1 does not
    #    touch T2), and never another worker's claim.
    try:
        if _orch_task_exists(task_id):
            cfg = OrchConfig()
            with get_neo4j_session(cfg) as session:
                session.run(
                    """
                    MATCH (t:OrchTask {id: $task_id})
                    WHERE t.status = 'in_progress'
                      AND (
                          coalesce(t.dispatched_to, '') = $worker
                          OR (coalesce(t.dispatched_to, '') = '' AND coalesce(t.owner, '') = $worker)
                      )
                    SET t.status = 'pending',
                        t.dispatched_to = NULL,
                        t.updated_at = datetime()
                    """,
                    task_id=task_id,
                    worker=worker,
                )
    except Exception as exc:
        logger.warning(
            "dispatch rollback: neo revert to pending FAILED worker=%s task=%s "
            "(task may linger in_progress as a phantom): %r", worker, task_id, exc)

    # 3. Clear the binding ONLY if it is still OURS, atomically. If the worker has
    #    moved to a different task (T2) the binding is T2's -- never delete it
    #    (grok V2). WATCH guards against a rebind racing between step 1 and here.
    if not is_ours:
        return
    try:
        with r.pipeline() as pipe:
            for _attempt in range(_WATCH_MAX_ATTEMPTS):
                try:
                    pipe.watch(key)
                    if not _binding_is_ours(pipe.get(key), task_id, binding_nonce):
                        pipe.unwatch()
                        break
                    pipe.multi()
                    pipe.delete(key)
                    pipe.execute()
                    break
                except WatchError:
                    # Bounded retry + small backoff so a hot current_task key cannot
                    # livelock this loop (grok ws2-state WATCH-livelock note).
                    time.sleep(_WATCH_BACKOFF_S * (_attempt + 1))
                    continue
    except Exception as exc:
        logger.warning(
            "dispatch rollback: could not clear current_task binding worker=%s "
            "task=%s: %r", worker, task_id, exc)


def _mark_in_progress_best_effort(task_id: str, worker: str) -> bool:
    """Flip an OrchTask to in_progress if it is pending + dependency-ready.

    Best-effort and NEVER raises (unlike _claim_ready_orch_task): returns False
    for ad-hoc (non-Orch) tasks, already-claimed tasks, or dependency-blocked
    tasks. Used by bind_current_task so a bound task stops re-surfacing in
    next-ready (the oscillation), without making bind fail on a re-bind.
    """
    if not _orch_task_exists(task_id):
        return False
    cfg = OrchConfig()
    owner = control_principal_for_session(worker, cfg.session_ids)
    with get_neo4j_session(cfg) as session:
        record = session.run(
            f"""
            MATCH (t:OrchTask {{id: $task_id}})
            SET t._claim_lock = true
            WITH t
            WHERE coalesce(t.status, 'pending') = 'pending'
              AND {_DEPENDENCIES_READY_CYPHER}
            SET t.status = 'in_progress',
                t.owner = $owner,
                t.dispatched_to = $worker,
                t.blocked_on = NULL,
                t.updated_at = datetime()
            RETURN t.id AS task_id
            """,
            task_id=task_id,
            worker=worker,
            owner=owner,
        ).single()
    return record is not None


def _default_dispatch_body(task_id: str, description: str) -> str:
    return (
        f"DISPATCH task={task_id}\n"
        f"description: {description}\n"
        f"(Recorded as your current_task in Redis. Your Stop hook will "
        f"notify the supervisor when you finish.)"
    )


def _with_record_outcome_footer(body: str, worker: str) -> str:
    if "taey-task outcome" in body:
        return body
    return body.rstrip() + (
        "\n\n---\nWHEN DONE - report the outcome through the installed CLI so "
        "the supervisor knows the result (otherwise outcome=unknown + current_task "
        "persists as 'previous dispatch unresolved'). One line via bash tool:\n\n"
        "taey-task outcome done --details '<short outcome summary>'\n\n"
        "Replace 'done' with 'error' or 'interrupted' if the task did not complete "
        "cleanly. If the task is genuinely awaiting a structured external signal, "
        "keep it in progress with:\n\n"
        "taey-task hold <task-id> AWAIT:external-signal:<detail>\n\n"
        "The '<short outcome summary>' is your one-line report - what landed, "
        "what's at /tmp/..., what's the verdict."
    )


def _project_hint_from_task_id(task_id: str) -> Optional[str]:
    prefix, sep, _rest = str(task_id or "").partition("::")
    return prefix if sep and prefix else None


def _rules_root_from_env() -> Optional[Path]:
    raw = os.environ.get("ORCH_RULES_ROOT", "").strip()
    return Path(raw).expanduser().resolve(strict=False) if raw else None


def _memory_root_from_env() -> Optional[Path]:
    raw = os.environ.get("ORCH_MEMORY_ROOT", "").strip()
    return Path(raw).expanduser().resolve(strict=False) if raw else None


def _minimal_dispatch_context(worker: str, task_id: str, description: str,
                              supervisor: Optional[str], cli: str) -> dict[str, Any]:
    cfg = OrchConfig()
    control_session = control_principal_for_session(supervisor or worker, cfg.session_ids)
    project = _project_hint_from_task_id(task_id)
    return {
        "overall_refs": [],
        "supervisor_refs": [],
        "project_refs": [],
        "phase_refs": [],
        "task_refs": [],
        "identity": {},
        "supervisor_affordance": {},
        "memory": get_memory(control_session, project=project, memory_root=_memory_root_from_env()),
        "rules": get_rules(control_session, project=project, rules_root=_rules_root_from_env()),
        "rules_meta": {},
        "budget_used": 0,
        "snapshot": {
            "repo_head": "",
            "session_id": worker,
            "cli": cli,
            "requested_task_id": task_id,
            "resolved_work": {
                "source": "in_progress_own",
                "project_id": project,
                "phase_id": None,
                "task_id": task_id,
                "description": description,
                "status": "in_progress",
                "owner": supervisor or "",
                "dispatched_to": worker,
                "task_type": None,
                "blocked_on": None,
            },
        },
    }


def _select_dispatch_context(worker: str, task_id: str, description: str,
                             supervisor: Optional[str], cli: str) -> tuple[dict[str, Any], str]:
    try:
        return select_wake_context(worker, task_id=task_id, cli=cli), ""
    except KnowledgeBaseContextError:
        raise
    except Exception as exc:
        warning = (
            "full task-scoped context selection unavailable; dispatch built the "
            f"mandatory packet from dispatch-local state and available rules: {exc.__class__.__name__}: {exc}"
        )
        logger.warning(
            "dispatch mandatory packet using minimal context worker=%s task=%s error=%s",
            worker,
            task_id,
            exc,
        )
        return _minimal_dispatch_context(worker, task_id, description, supervisor, cli), warning


def _assemble_dispatch_prompt(worker: str, task_id: str, description: str,
                              supervisor: Optional[str], dispatcher: str,
                              dispatch_body: str,
                              causal_event_ids: Optional[list[str]] = None) -> tuple[str, dict[str, Any]]:
    cli = _cli_for_worker(worker)
    context, warning = _select_dispatch_context(worker, task_id, description, supervisor, cli)
    packet = build_wake_packet(worker, context)
    generated_for = str(packet.get("generated_for") or "").strip()
    snapshot_session = str((packet.get("snapshot") or {}).get("session_id") or "").strip()
    if generated_for != worker or snapshot_session != worker:
        raise RuntimeError(
            "dispatch wake packet recipient mismatch: "
            f"worker={worker!r} generated_for={generated_for!r} "
            f"snapshot_session={snapshot_session!r}"
        )
    world_publication = publish_world_manifest_v0(
        subject={"worker": worker, "task_id": task_id, "supervisor": supervisor or CAUSAL_UNKNOWN},
        parents=causal_event_ids or [],
        packet_id=str(packet.get("packet_id") or ""),
        packet_provenance_hash=CAUSAL_UNKNOWN,
    )
    world_manifest_event_id = str(world_publication.get("event_id") or "")
    packet["world_manifest"] = world_publication.get("manifest") or {}
    packet_causal_event_ids = [
        event_id
        for event_id in [*(causal_event_ids or []), world_manifest_event_id]
        if event_id
    ]
    dispatch_record = {
        "from": dispatcher,
        "type": "dispatch",
        "task_id": task_id,
        "description": description,
        "supervisor": supervisor or "",
        "body": dispatch_body,
    }
    if warning:
        dispatch_record["context_warning"] = warning
    packet.setdefault("human", {}).setdefault("replies_since_last", []).append(dispatch_record)
    proof_capsule = attach_wake_proof_capsule(
        packet,
        {
            "worker": worker,
            "task_id": task_id,
            "supervisor": supervisor or "",
            "dispatcher": dispatcher,
            "causal_event_ids": packet_causal_event_ids,
            "world_manifest": world_publication.get("manifest") or {},
            "world_id": world_publication.get("world_id") or "",
            "world_manifest_sha256": world_publication.get("manifest_sha256") or "",
        },
    )
    rendered = assemble_wake_packet(packet, cli)
    receipt = task_ref_receipt(packet)
    return rendered, {
        "cli": cli,
        "packet_id": packet.get("packet_id", ""),
        "provenance_hash": packet.get("provenance_hash", ""),
        "proof_capsule": proof_capsule,
        "world_id": proof_capsule.get("world_id", ""),
        "world_manifest_event_id": world_manifest_event_id,
        "world_manifest_path": world_publication.get("manifest_path", ""),
        "world_manifest_sha256": world_publication.get("manifest_sha256", ""),
        "world_manifest": world_publication.get("manifest") or {},
        "injection_receipt": receipt,
        "size_report": wake_size_report(rendered, packet),
        "rules": [
            {"scope": rule.get("scope", ""), "path": rule.get("path", "")}
            for rule in context.get("rules") or []
        ],
        "refs": {
            tier: [ref.get("path", "") for ref in context.get(f"{tier}_refs") or []]
            for tier in ("overall", "supervisor", "project", "phase", "task")
        },
    }


def dispatch(
    worker: str,
    task_id: str,
    description: str,
    supervisor: Optional[str] = None,
    prompt_body: Optional[str] = None,
    priority: str = "normal",
    is_bugfix: bool = False,
    force: bool = False,
) -> None:
    """Record the task on the worker side and inject the mandatory wake packet.

    Side effects (in order):

    1. Reject a cross-supervisor dispatch to a registered ``*-codex`` control
       before any worker-state mutation. Control seats own and supervise work;
       their Claude/Gemini/Grok seats execute delegated worker commands.
    2. If the worker maps to a product in ``ORCH_PRODUCT_OWNER_MAP`` / ``PRODUCT_OWNER_MAP`` and that
       product has ``support:product:<id>:bug_lock == "true"``, raise
       ``BugLockActive`` before any worker-state mutation unless
       ``is_bugfix=True``.
    3. Write ``taey:<worker>:current_task`` JSON {task_id, description,
       supervisor, started_at} — the universal Stop hook reads this to
       build its supervisor-notify body. Cleared by the Stop hook after
       the supervisor is notified.
    4. If ``supervisor`` is provided for a worker seat, write
       ``taey:<worker>:parent`` so
       the Stop hook addresses notifications correctly even for multi-
       level trees (where suffix-strip wouldn't reach the right node).
    5. Refuse if the target CLI does not have managed notify hooks installed;
       without hooks the worker cannot maintain wake/stop-discipline state.
    6. Assemble a wake packet for ``worker`` and ``task_id``. The original
       dispatch body is embedded in the packet's Human section, so direct
       dispatch still receives rules and context.
    7. Inject that rendered packet by invoking ``taey-notify <worker> --body-file <path>``,
       which the released fleet-notify daemon will pick up and deliver
       via tmux as soon as the worker is idle.

    The prompt body defaults to a text envelope wrapping {task_id,
    description} so the worker can identify what it's being asked to do.
    Pass ``prompt_body`` to override the dispatch body embedded inside the
    packet; it is never sent as an un-injected standalone prompt.
    """
    registered_sessions = _parse_session_ids()
    from_session = supervisor or os.environ.get("TAEY_NODE_ID", "dispatch")
    worker_control = control_principal_for_session(worker, registered_sessions)
    dispatcher_control = control_principal_for_session(from_session, registered_sessions)
    target_is_codex_control = (
        worker.lower().endswith("-codex")
        and worker_control.lower() == worker.lower()
    )
    if target_is_codex_control and dispatcher_control.lower() != worker.lower():
        raise ControlSeatTarget(
            f"registered control seat {worker!r} cannot execute a worker dispatch from "
            f"{from_session!r}; target one of its Claude/Gemini/Grok worker seats"
        )

    hook_status = hook_installation_status(worker)
    if not hook_status.ok:
        raise HooksNotInstalled(hook_status.detail)

    r = _redis_connect()
    product_id = _resolve_product_id(worker)
    if product_id and not is_bugfix:
        bug_lock_key = f"support:product:{product_id}:bug_lock"
        if r.get(bug_lock_key) == "true":
            reason = (
                r.get(f"support:product:{product_id}:bug_lock_reason")
                or "(no reason recorded)"
            )
            raise BugLockActive(f"BUG_LOCK_ACTIVE for {product_id}: {reason}")

    previous_force_bindings = _current_task_binding_candidates(task_id) if force else set()
    _claim_ready_orch_task(
        task_id=task_id,
        worker=worker,
        supervisor=supervisor,
        force=force,
    )
    try:
        binding_nonce = bind_current_task(
            worker=worker,
            task_id=task_id,
            description=description,
            supervisor=supervisor,
            set_parent=bool(supervisor) and not target_is_codex_control,
            force=force,
            guard_existing=True,
            dispatcher=from_session,
        )
    except WorkerBusy:
        _rollback_claim_only(worker, task_id)
        raise
    had_prompt_override = prompt_body is not None
    try:
        dispatch_claimed_row = append_causal_event(
            "dispatch_claimed",
            subject={"worker": worker, "task_id": task_id, "supervisor": supervisor or CAUSAL_UNKNOWN},
            payload={
                "description": description,
                "dispatcher": from_session,
                "priority": priority,
                "is_bugfix": is_bugfix,
                "force": force,
                "binding_nonce": binding_nonce,
                "prompt_override": had_prompt_override,
            },
        )
    except Exception as exc:
        _rollback_claim(worker, task_id, binding_nonce)
        raise RuntimeError(f"causal dispatch_claimed append failed: {exc}") from exc
    dispatch_event_id = _causal_event_id(dispatch_claimed_row)
    dispatch_body = _with_record_outcome_footer(
        prompt_body if prompt_body is not None else _default_dispatch_body(task_id, description),
        worker,
    )
    packet_meta: dict[str, Any] = {}
    attestation: dict[str, Any] = {}
    attestation_id = CAUSAL_UNKNOWN
    wake_packet_event_id = ""
    try:
        prompt_body, packet_meta = _assemble_dispatch_prompt(
            worker,
            task_id,
            description,
            supervisor,
            from_session,
            dispatch_body,
            causal_event_ids=[dispatch_event_id] if dispatch_event_id else [],
        )
        prompt_sha256 = hashlib.sha256(prompt_body.encode("utf-8")).hexdigest()
        work_snapshot = _worker_work_snapshot(worker)
        attestation = build_actor_attestation(
            seat_id=worker,
            supervisor=supervisor,
            task_id=task_id,
            dispatch_event_id=dispatch_event_id,
            packet_id=str(packet_meta.get("packet_id", "")),
            packet_provenance_hash=str(packet_meta.get("provenance_hash", "")),
            prompt_sha256=prompt_sha256,
            cli=str(packet_meta.get("cli", "")),
            dispatcher=from_session,
            binding_nonce=binding_nonce,
            repo=work_snapshot.get("repo"),
            branch=work_snapshot.get("branch"),
            worktree=work_snapshot.get("worktree"),
        )
        attestation_id = str(attestation["attestation_id"])
        wake_packet_row = append_causal_event(
            "wake_packet_assembled",
            subject={"worker": worker, "task_id": task_id, "supervisor": supervisor or CAUSAL_UNKNOWN},
            parents=[
                event_id
                for event_id in (dispatch_event_id, packet_meta.get("world_manifest_event_id"))
                if event_id
            ],
            actor_attestation_id=attestation_id,
            packet_id=str(packet_meta.get("packet_id", "")),
            packet_provenance_hash=str(packet_meta.get("provenance_hash", "")),
            payload={
                "attestation": attestation,
                "prompt_sha256": prompt_sha256,
                "cli": packet_meta.get("cli", ""),
                "injection_receipt": packet_meta.get("injection_receipt", {}),
                "size_report": packet_meta.get("size_report", {}),
                "rules": packet_meta.get("rules", []),
                "refs": packet_meta.get("refs", {}),
                "world_id": packet_meta.get("world_id", CAUSAL_UNKNOWN),
                "world_manifest_event_id": packet_meta.get("world_manifest_event_id", CAUSAL_UNKNOWN),
                "world_manifest_path": packet_meta.get("world_manifest_path", CAUSAL_UNKNOWN),
                "world_manifest_sha256": packet_meta.get("world_manifest_sha256", CAUSAL_UNKNOWN),
            },
        )
    except Exception as exc:
        _rollback_claim(worker, task_id, binding_nonce)
        _append_dispatch_delivery_failed_causal_event(
            worker=worker,
            task_id=task_id,
            supervisor=supervisor,
            dispatch_event_id=dispatch_event_id,
            wake_packet_event_id=wake_packet_event_id,
            attestation_id=attestation_id,
            packet_meta=packet_meta,
            failure_stage="wake_packet_assembly",
            binding_nonce=binding_nonce,
            rollback="claim_and_current_task_reverted_if_nonce_matched",
            error=exc,
        )
        raise RuntimeError(f"mandatory dispatch wake-packet assembly/provenance failed: {exc}") from exc
    wake_packet_event_id = _causal_event_id(wake_packet_row)
    _attach_causal_metadata_to_current_task(
        r,
        worker,
        task_id,
        binding_nonce,
        {
            "attestation_id": attestation_id,
            "dispatch_event_id": dispatch_event_id,
            "wake_packet_event_id": wake_packet_event_id,
            "world_manifest_event_id": packet_meta.get("world_manifest_event_id", ""),
            "world_id": packet_meta.get("world_id", CAUSAL_UNKNOWN),
            "packet_id": packet_meta.get("packet_id", ""),
            "packet_provenance_hash": packet_meta.get("provenance_hash", ""),
            "prompt_sha256": prompt_sha256,
        },
    )

    mark_superseded_for_task(_redis_connect(), from_session, task_id)

    cli = notify_cli()
    body_file_path = ""
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            prefix="orch-dispatch-body-",
            suffix=".txt",
            delete=False,
        ) as body_file:
            body_file.write(prompt_body)
            body_file_path = body_file.name
        result = subprocess.run(
            [
                cli,
                worker,
                "--body-file",
                body_file_path,
                "--from",
                from_session,
                "--type",
                "command",
                "--priority",
                priority,
                "--handoff",
                "--dispatcher-task-id",
                task_id,
                "--actionable-inputs",
                "{}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        if body_file_path:
            try:
                os.unlink(body_file_path)
            except FileNotFoundError:
                pass
    if result.returncode != 0:
        # Wake delivery failed AFTER the claim+bind. Without this rollback the task
        # lingers as status=in_progress (a "live resolver" per _LIVE_RESOLVER_STATUSES)
        # with NO wake delivered -> a supervisor blocked_on it would ALLOW_STOP and the
        # work silently dead-locks (dispatched-wake guarantee: live-set membership
        # is necessary, not sufficient; a live resolver must have an ACTUAL wake). Revert
        # to ready so next-ready re-surfaces it for redispatch instead. The binding_nonce
        # is the claim-token: rollback only reverts THIS dispatch's binding, never a newer
        # one a concurrent same-worker dispatch may have rebound (grok PR#25 V1/V2).
        # taey-notify exits non-zero only BEFORE it lpushes the inbox message, so rc!=0
        # means the wake was not delivered (V3): reverting is correct.
        _rollback_claim(worker, task_id, binding_nonce)
        _append_dispatch_delivery_failed_causal_event(
            worker=worker,
            task_id=task_id,
            supervisor=supervisor,
            dispatch_event_id=dispatch_event_id,
            wake_packet_event_id=wake_packet_event_id,
            attestation_id=attestation_id,
            packet_meta=packet_meta,
            failure_stage="taey_notify",
            binding_nonce=binding_nonce,
            rollback="claim_and_current_task_reverted_if_nonce_matched",
            returncode=result.returncode,
            stdout=result.stdout or "",
            stderr=result.stderr or "",
        )
        raise RuntimeError(result.stderr.strip() or f"{cli} failed")
    _clear_replaced_force_bindings(task_id, previous_force_bindings, worker)
    capture_failure: Optional[dict[str, Any]] = None
    wake_delivered_event_id = ""
    try:
        wake_delivered_row = append_causal_event(
            "wake_delivered",
            subject={"worker": worker, "task_id": task_id, "supervisor": supervisor or CAUSAL_UNKNOWN},
            parents=[wake_packet_event_id] if wake_packet_event_id else ([dispatch_event_id] if dispatch_event_id else []),
            actor_attestation_id=attestation_id,
            packet_id=str(packet_meta.get("packet_id", "")),
            packet_provenance_hash=str(packet_meta.get("provenance_hash", "")),
            payload={
                "notify_cli": cli,
                "returncode": result.returncode,
                "stdout": (result.stdout or "")[:2000],
                "priority": priority,
            },
        )
    except Exception as exc:
        capture_failure = {
            "marker": "capture_failure",
            "event_type": "wake_delivered",
            "stage": "wake_delivered",
            "attestation_id": attestation_id,
            "reason": f"{exc.__class__.__name__}: {exc}"[:2000],
            "terminal_child_missing": True,
        }
        logger.warning(
            "wake delivered but causal wake_delivered append failed worker=%s task=%s: %r",
            worker,
            task_id,
            exc,
        )
        _attach_causal_metadata_to_current_task(
            r,
            worker,
            task_id,
            binding_nonce,
            {
                "wake_delivered_event_id": CAUSAL_UNKNOWN,
                "capture_failure": capture_failure,
            },
        )
    else:
        wake_delivered_event_id = _causal_event_id(wake_delivered_row)
        _attach_causal_metadata_to_current_task(
            r,
            worker,
            task_id,
            binding_nonce,
            {"wake_delivered_event_id": wake_delivered_event_id},
        )
    causal_event_ids = [
        event_id
        for event_id in (
            dispatch_event_id,
            packet_meta.get("world_manifest_event_id", ""),
            wake_packet_event_id,
            wake_delivered_event_id,
        )
        if event_id
    ]
    proof_capsule = packet_meta.get("proof_capsule") if isinstance(packet_meta.get("proof_capsule"), dict) else {}
    world_id = str(packet_meta.get("world_id") or proof_capsule.get("world_id") or CAUSAL_UNKNOWN)
    observable_state = {
        "source": "dispatch",
        "worker": worker,
        "task_id": task_id,
        "supervisor": supervisor,
        "priority": priority,
        "prompt_sha256": prompt_sha256,
        "cli": packet_meta.get("cli", ""),
        "packet_id": packet_meta.get("packet_id", ""),
        "provenance_hash": packet_meta.get("provenance_hash", ""),
        "world_id": world_id,
        "attestation_id": attestation_id,
        "size_report": packet_meta.get("size_report", {}),
        "actor_attestation_id": attestation_id,
        "causal_event_ids": causal_event_ids,
        "proof_capsule": proof_capsule,
    }
    if capture_failure:
        observable_state["capture_failure"] = capture_failure
    maybe_emit_decision_receipt(
        "wake",
        {
            "why_this_context": "dispatch assembled the mandatory wake packet and delivered it through taey-notify",
            "refs_used": packet_meta.get("refs", {}),
            "rule_tier_applied": packet_meta.get("rules", []),
            "observable_state": observable_state,
            "world_id": world_id,
            "attestation_id": attestation_id,
            "causal_event_ids": causal_event_ids,
            "target": worker,
            "task_id": task_id,
            "next_contract": (
                f"worker first replies `{packet_meta.get('injection_receipt', {}).get('line', 'loaded refs: none')}`; "
                "worker records outcome when the dispatched task is complete"
            ),
        },
    )


_VALID_OUTCOMES = ("done", "error", "interrupted")
_COMPLETION_RECEIPT_TTL_SECS = 1800


def _current_task_id(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    try:
        cur = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(cur, dict):
        return None
    task_id = cur.get("task_id")
    return str(task_id) if task_id else None


def _outcome_payload(outcome: str, details: Optional[str], current_task: Optional[dict[str, Any]]) -> dict[str, Any]:
    payload = {"outcome": outcome}
    detail_text = str(details or "")
    task_id = str((current_task or {}).get("task_id") or "").strip()
    if task_id:
        payload["task_id"] = task_id
        if task_id not in detail_text:
            suffix = f" [task_id={task_id}]"
            detail_text = f"{detail_text[:max(0, 500 - len(suffix))]}{suffix}".strip()
    if detail_text:
        payload["details"] = detail_text[:500]
    return payload


def _write_completion_receipt(r: Any, worker: str, current_task_id: str, payload: dict[str, Any]) -> None:
    receipt = {
        "outcome": "done",
        "task_id": current_task_id,
        "worker": worker,
        "ts": time.time(),
    }
    details = str(payload.get("details") or "").strip()
    if details:
        receipt["details"] = details[:500]
    try:
        key = _state_key(worker, "last_completion_receipt")
        existing = _decode_current_task(r.get(key))
        if (
            existing
            and str(existing.get("outcome") or "").strip().lower() == "done"
            and str(existing.get("task_id") or "").strip() == current_task_id
        ):
            return
        r.set(
            key,
            json.dumps(receipt, sort_keys=True),
            ex=_COMPLETION_RECEIPT_TTL_SECS,
        )
    except Exception:
        logger.warning(
            "completion receipt write failed worker=%s task=%s",
            worker,
            current_task_id,
            exc_info=True,
        )


def write_completion_receipt(worker: str, task_id: str, details: Optional[str] = None) -> None:
    clean_worker = str(worker or "").strip()
    clean_task_id = str(task_id or "").strip()
    if not clean_worker or not clean_task_id:
        return
    payload = _outcome_payload("done", details, {"task_id": clean_task_id})
    _write_completion_receipt(_redis_connect(), clean_worker, clean_task_id, payload)


def _notify_supervisor_response_ready(worker: str,
                                      current_task: Optional[dict[str, Any]],
                                      payload: dict[str, Any]) -> None:
    if not current_task:
        return
    supervisor = str(current_task.get("supervisor") or "").strip()
    if not supervisor or supervisor == worker:
        return
    task_id = str(current_task.get("task_id") or "").strip()
    description = str(current_task.get("description") or "").strip()
    outcome = str(payload.get("outcome") or "unknown").strip().lower() or "unknown"
    details = str(payload.get("details") or "").strip()
    body = (
        f"{worker} reported {outcome}"
        f"{f' for {task_id}' if task_id else ''}"
        f"{f': {description}' if description else ''}"
        f"{f' - {details}' if details else ''}"
    )
    cli = notify_cli()
    try:
        result = subprocess.run(
            [
                cli,
                supervisor,
                body,
                "--from",
                worker,
                "--type",
                "response_ready",
                "--priority",
                "high",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(
            "supervisor response_ready wake failed worker=%s supervisor=%s task=%s error=%s",
            worker,
            supervisor,
            task_id,
            exc,
        )
        return
    if result.returncode != 0:
        logger.warning(
            "supervisor response_ready wake failed worker=%s supervisor=%s task=%s rc=%s stderr=%s",
            worker,
            supervisor,
            task_id,
            result.returncode,
            (result.stderr or "").strip(),
        )


def _revert_outcome_claim(worker: str, task_id: str) -> None:
    if not _orch_task_exists(task_id):
        return
    cfg = OrchConfig()
    with get_neo4j_session(cfg) as session:
        session.run(
            """
            MATCH (t:OrchTask {id: $task_id})
            WHERE t.status = 'in_progress'
              AND (
                  coalesce(t.dispatched_to, '') = $worker
                  OR (coalesce(t.dispatched_to, '') = '' AND coalesce(t.owner, '') = $worker)
              )
            SET t.status = 'pending',
                t.dispatched_to = NULL,
                t.updated_at = datetime()
            """,
            task_id=task_id,
            worker=worker,
        )


def _clean_str(value: Any) -> str:
    return str(value or "").strip()


def _load_rework_task(task_id: str) -> dict[str, Any]:
    cfg = OrchConfig()
    with get_neo4j_session(cfg) as session:
        record = session.run(
            """
            MATCH (t:OrchTask {id: $task_id})
            RETURN t
            """,
            task_id=task_id,
        ).single()
    if not record:
        raise ChangesRequestedError(f"task not found: {task_id}")
    return dict(record["t"])


def _rework_worker_for_task(task: dict[str, Any], worker: Optional[str]) -> str:
    candidates = (
        worker,
        task.get("dispatched_to"),
        task.get("worker_liveness_worker"),
    )
    for candidate in candidates:
        value = _clean_str(candidate)
        if value:
            return value
    owner = _clean_str(task.get("owner"))
    if owner.endswith(("-codex", "-gemini", "-grok", "-claude")):
        return owner
    task_id = _clean_str(task.get("id")) or "unknown-task"
    raise ChangesRequestedError(
        f"cannot infer peer for changes_requested task={task_id}; pass peer explicitly"
    )


def _mark_changes_requested_pending(task_id: str, worker: str, requested_by: str, reason: str) -> dict[str, Any]:
    entry = json.dumps(
        {
            "task_id": task_id,
            "worker": worker,
            "requested_by": requested_by,
            "reason": reason,
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        },
        sort_keys=True,
    )
    cfg = OrchConfig()
    with get_neo4j_session(cfg) as session:
        record = session.run(
            """
            MATCH (t:OrchTask {id: $task_id})
            SET t.status = 'pending',
                t.dispatched_to = NULL,
                t.blocked_on = NULL,
                t.last_changes_requested_at = datetime(),
                t.last_changes_requested_by = $requested_by,
                t.last_changes_requested_worker = $worker,
                t.last_changes_requested_reason = $reason,
                t.changes_requested_count = coalesce(t.changes_requested_count, 0) + 1,
                t.changes_requested_log = coalesce(t.changes_requested_log, []) + [$entry],
                t.updated_at = datetime()
            RETURN t.id AS task_id,
                   t.description AS description,
                   t.status AS status,
                   t.last_changes_requested_reason AS reason,
                   t.last_changes_requested_worker AS worker,
                   t.last_changes_requested_by AS requested_by,
                   t.changes_requested_count AS count
            """,
            task_id=task_id,
            worker=worker,
            requested_by=requested_by,
            reason=reason,
            entry=entry,
        ).single()
    if not record:
        raise ChangesRequestedError(f"task not found: {task_id}")
    return dict(record)


def _changes_requested_prompt(task_id: str, description: str, requested_by: str, reason: str) -> str:
    return (
        "CHANGES REQUESTED\n\n"
        f"Task: {task_id}\n"
        f"Description: {description}\n"
        f"Validator: {requested_by}\n\n"
        "The supervisor production/audit validation rejected the prior result. "
        "Rework the same task; do not mark it complete until the requested change is addressed.\n\n"
        f"Reason:\n{reason}\n"
    )


def request_changes(
    task_id: str,
    *,
    requested_by: str,
    reason: str,
    worker: Optional[str] = None,
    priority: str = "high",
) -> dict[str, Any]:
    """Supervisor-side primitive for audit-rejected peer work.

    The validator requests changes without self-fixing. The same peer is
    re-bound through the canonical dispatch path, so the task moves directly
    from rejected in-flight work to a new in-progress worker wake.
    """
    clean_task_id = _clean_str(task_id)
    clean_requested_by = _clean_str(requested_by)
    clean_reason = _clean_str(reason)
    if not clean_task_id:
        raise ChangesRequestedError("changes_requested requires task_id")
    if not clean_requested_by:
        raise ChangesRequestedError("changes_requested requires requested_by/from")
    if not clean_reason:
        raise ChangesRequestedError("changes_requested requires a non-empty reason")

    task = _load_rework_task(clean_task_id)
    rework_worker = _rework_worker_for_task(task, worker)
    if clean_requested_by == rework_worker:
        raise ChangesRequestedError(
            "changes_requested must be requested by the validator/supervisor, not the worker being re-dispatched"
        )

    description = _clean_str(task.get("description"))
    marked = _mark_changes_requested_pending(
        clean_task_id,
        rework_worker,
        clean_requested_by,
        clean_reason,
    )
    dispatch(
        rework_worker,
        clean_task_id,
        description,
        supervisor=clean_requested_by,
        prompt_body=_changes_requested_prompt(clean_task_id, description, clean_requested_by, clean_reason),
        priority=priority,
    )
    marked.update(
        {
            "ok": True,
            "task_id": clean_task_id,
            "status": "in_progress",
            "dispatched_to": rework_worker,
            "requested_by": clean_requested_by,
            "reason": clean_reason,
        }
    )
    return marked


def record_outcome(worker: str, outcome: str, details: Optional[str] = None) -> None:
    """Worker-side helper: record the task outcome before stopping.

    ``outcome`` MUST be one of ``done``, ``error``, ``interrupted``. Any
    other value raises ``ValueError``. The enum is load-bearing: every
    terminal outcome records ``last_outcome``, clears the matching
    current_task binding, and immediately wakes the binding supervisor.
    A successful ``done`` cleanup also writes ``last_completion_receipt`` so
    schedulers can prove a session boundary before clearing context.

    Semantics:
    - ``done`` — task completed successfully. Supervisor can move on, and
      current_task clears.
    - ``error`` — task hit an unrecoverable error. The task returns to
      pending, last_outcome preserves the failure, and current_task clears.
    - ``interrupted`` — Ctrl-C, timeout, or external cancel. The task returns
      to pending, last_outcome preserves the interruption, and current_task clears.

    Workers that stop without calling this end up with outcome=``unknown``
    in the peer_idle body, which is also load-bearing: the supervisor
    sees "stopped but did not signal" and can investigate.
    """
    if outcome not in _VALID_OUTCOMES:
        raise ValueError(
            f"outcome must be one of {_VALID_OUTCOMES!r}, got {outcome!r}"
        )
    r = _redis_connect()
    last_outcome_key = _state_key(worker, "last_outcome")
    current_task_key = _state_key(worker, "current_task")
    current_task: Optional[dict[str, Any]] = None
    current_task_id: Optional[str] = None
    payload: dict[str, Any] = {"outcome": outcome}
    stored = False
    if outcome == "done":
        current_task_raw = r.get(current_task_key)
        current_task = _decode_current_task(current_task_raw)
        current_task_id = _current_task_id(current_task_raw)
        payload = _outcome_payload(outcome, details, current_task)
        r.set(last_outcome_key, json.dumps(payload))
        stored = True
    else:
        from redis import WatchError

        for _attempt in range(_WATCH_MAX_ATTEMPTS):
            with r.pipeline() as pipe:
                try:
                    pipe.watch(current_task_key)
                    current_task_raw = pipe.get(current_task_key)
                    current_task = _decode_current_task(current_task_raw)
                    current_task_id = _current_task_id(current_task_raw)
                    payload = _outcome_payload(outcome, details, current_task)
                    pipe.multi()
                    pipe.set(last_outcome_key, json.dumps(payload))
                    pipe.execute()
                    stored = True
                    break
                except WatchError:
                    # Bounded retry + small backoff so a hot current_task key cannot
                    # livelock this loop (grok ws2-state WATCH-livelock note).
                    time.sleep(_WATCH_BACKOFF_S * (_attempt + 1))
                    continue
    if stored:
        try:
            worker_outcome_event_id = _append_worker_outcome_causal_event(
                worker,
                outcome,
                details,
                current_task,
                payload,
            )
            if worker_outcome_event_id:
                payload["causal_event_id"] = worker_outcome_event_id
                r.set(last_outcome_key, json.dumps(payload))
        except Exception as exc:
            logger.warning("worker_outcome_recorded causal event append failed worker=%s: %r", worker, exc)
        try:
            if current_task_id:
                if outcome != "done":
                    _revert_outcome_claim(worker, current_task_id)
                    from .worker_liveness import clear_worker_task_liveness

                    clear_worker_task_liveness(current_task_id)
                from .current_task_binding import clear_matching_current_task

                cleared_current_task = clear_matching_current_task(
                    worker,
                    current_task_id,
                    redis_client=r,
                    reason=f"record_outcome:{outcome}",
                )
                if outcome == "done" and cleared_current_task:
                    _write_completion_receipt(r, worker, current_task_id, payload)
        finally:
            _notify_supervisor_response_ready(worker, current_task, payload)


def check_previous_task(worker: str) -> Optional[dict]:
    """Supervisor-side helper: inspect a worker's current_task before dispatch.

    Returns ``None`` if the worker has no current_task (clean to dispatch).
    Returns the current_task dict {task_id, description, supervisor,
    started_at, last_outcome?} if a previous dispatch is unresolved
    (outcome was not ``done``, so the Stop hook left current_task in place).

    Supervisors SHOULD call this before issuing new dispatches. The
    common pattern:

        prev = check_previous_task("worker-codex")
        if prev:
            # Previous dispatch didn't complete cleanly. Decide:
            # - retry: re-dispatch the same task body
            # - investigate: open the worker pane, look at the error
            # - cancel: clear_current_task() and move on
            ...
        dispatch("worker-codex", ...)
    """
    r = _redis_connect()
    raw = r.get(_state_key(worker, "current_task"))
    if not raw:
        return None
    try:
        task = json.loads(raw)
    except Exception:
        return {"raw": raw}
    last_outcome_raw = r.get(_state_key(worker, "last_outcome"))
    if last_outcome_raw:
        try:
            task["last_outcome"] = json.loads(last_outcome_raw)
        except Exception:
            task["last_outcome"] = {"outcome": "unknown", "details": last_outcome_raw}
    return task


class UnbindError(Exception):
    """Fail-loud unbind / graph-reconcile error with an HTTP-facing status code."""

    def __init__(self, message: str, *, status_code: int = 409) -> None:
        super().__init__(message)
        self.status_code = int(status_code)
        self.message = str(message)


def _read_session_current_task(worker: str) -> Dict[str, Any]:
    raw = _redis_connect().get(_state_key(worker, "current_task"))
    return decode_current_task(raw) or {}


def _reconcile_unbind_graph(worker: str, task_id: str) -> Dict[str, Any]:
    """Identity-bound fail-loud graph reconcile for unbind.

    Preserve owner; set pending; clear dispatched_to + worker_liveness_*.
    Raises UnbindError when the exact bound task cannot be reconciled.
    """
    cfg = OrchConfig()
    try:
        with get_neo4j_session(cfg) as session:
            record = session.run(
                """
                MATCH (t:OrchTask {id: $task_id})
                WHERE coalesce(t.dispatched_to, '') = $worker
                   OR (
                        coalesce(t.dispatched_to, '') = ''
                        AND coalesce(t.owner, '') = $worker
                        AND coalesce(t.status, '') = 'in_progress'
                   )
                   OR coalesce(t.worker_liveness_worker, '') = $worker
                SET t.status = 'pending',
                    t.dispatched_to = NULL,
                    t.blocked_on = NULL,
                    t.worker_liveness_worker = NULL,
                    t.worker_liveness_supervisor = NULL,
                    t.worker_liveness_started_at = NULL,
                    t.worker_liveness_heartbeat_at = NULL,
                    t.worker_liveness_ttl_secs = NULL,
                    t.worker_liveness_ack_at = NULL,
                    t.worker_liveness_escalated_at = NULL,
                    t.worker_liveness_escalation_reason = NULL,
                    t.updated_at = datetime()
                RETURN t.id AS task_id,
                       t.status AS status,
                       t.owner AS owner,
                       coalesce(t.dispatched_to, '') AS dispatched_to,
                       coalesce(t.worker_liveness_worker, '') AS worker_liveness_worker
                """,
                task_id=task_id,
                worker=worker,
            ).single()
    except UnbindError:
        raise
    except Exception as exc:
        raise UnbindError(
            f"Neo4j unbind reconcile failed for session={worker} task={task_id}: {exc}",
            status_code=500,
        ) from exc
    if record is None:
        raise UnbindError(
            f"No identity-bound OrchTask to unbind for session={worker} task={task_id}; "
            f"refusing to clear Redis while the graph relation is unmatched. "
            f"Pass an explicit repair task_id only when dispatched_to/liveness names this session.",
            status_code=409,
        )
    return {
        "task_id": str(record["task_id"]),
        "status": str(record["status"] or ""),
        "owner": str(record["owner"] or ""),
        "dispatched_to": str(record["dispatched_to"] or "") or None,
        "worker_liveness_worker": str(record["worker_liveness_worker"] or "") or None,
    }


def clear_current_task(worker: str, *, task_id: Optional[str] = None) -> Dict[str, Any]:
    """Unbind a session: fail-loud graph reconcile, then clear Redis live bind.

    Normally the Stop hook clears Redis after notifying the supervisor —
    but ONLY when the recorded outcome was ``done``. For ``error`` /
    ``interrupted`` / ``unknown`` outcomes the keys persist as a signal
    to the next dispatcher. This helper is the supervisor's explicit
    "I've seen the previous task's outcome, I'm moving on" acknowledgment
    — call it after investigating or after deciding to cancel.

    Order (authoritative):
      1. Resolve identity from Redis ``current_task`` (or explicit repair task_id)
      2. Reconcile Neo4j: pending + clear dispatched_to/liveness (fail loud)
      3. Clear Redis current_task/last_outcome + worker-task liveness sidecar

    Graph-only ``/current`` stays truthful after step 2; Redis is cleared last so a
    Neo failure cannot return success with a stale executor relation.
    """
    from .worker_liveness import worker_task_liveness_key

    session_id = str(worker or "").strip()
    if not session_id:
        raise UnbindError("session_id is required for unbind", status_code=400)

    bound = _read_session_current_task(session_id)
    bound_task_id = str(bound.get("task_id") or "").strip()
    repair_task_id = str(task_id or "").strip()
    repair = False

    if bound_task_id:
        if repair_task_id and repair_task_id != bound_task_id:
            raise UnbindError(
                f"Redis bind for {session_id} is {bound_task_id}; refusing repair task_id="
                f"{repair_task_id}. Unbind without --task-id, or clear the live bind first.",
                status_code=409,
            )
        target_task_id = bound_task_id
    elif repair_task_id:
        # Explicit repair when Redis is already absent but a stale graph claim remains.
        target_task_id = repair_task_id
        repair = True
    else:
        raise UnbindError(
            f"No Redis current_task bind for {session_id}. If a stale dispatched_to/"
            f"worker_liveness relation remains, re-run with an explicit repair task_id "
            f"(DELETE .../current-task?task_id=... or `taey-task unbind {session_id} --task-id ...`).",
            status_code=409,
        )

    reconciled = _reconcile_unbind_graph(session_id, target_task_id)

    try:
        r = _redis_connect()
        cleared = clear_session_current_task(session_id, redis_client=r)
        r.delete(worker_task_liveness_key(target_task_id))
    except Exception as exc:
        raise UnbindError(
            f"Graph reconciled to pending for {target_task_id}, but Redis clear failed for "
            f"{session_id}: {exc}. Retry unbind; graph is already dispatchable.",
            status_code=500,
        ) from exc

    return {
        "session": session_id,
        "ok": True,
        "unbound": True,
        "repair": repair,
        "previous_task_id": target_task_id,
        "task_id": reconciled["task_id"],
        "status": reconciled["status"],
        "owner": reconciled["owner"],
        "dispatched_to": reconciled["dispatched_to"],
        "worker_liveness_worker": reconciled["worker_liveness_worker"],
        "redis_cleared": bool(cleared.get("cleared")),
        "cleared": cleared,
    }
