"""Supervisor→worker dispatch primitive.

Writes the task body to the worker's Redis inbox AND records
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

Once dispatched, the worker receives the prompt via the released
``claude-code-fleet-notify`` daemon (idle=1 + inbox>0 → tmux inject).
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
import time
from typing import Any, Optional

from .config import OrchConfig, get_neo4j_session, notify_cli
from .notify_state import redis_connect as _notify_redis_connect
from .notify_state import state_key as _notify_state_key
from .decision_receipt import maybe_emit_receipt as maybe_emit_decision_receipt
from .handoff_validation import mark_superseded_for_task
from .worker_liveness import register_worker_task_liveness

logger = logging.getLogger(__name__)

# Bounded Redis WATCH retry (grok ws2-state WATCH-livelock note): a hot current_task key
# must not let an optimistic-lock loop spin forever. ~8 attempts with linear backoff caps
# the worst case at well under a second, after which we give up rather than livelock.
_WATCH_MAX_ATTEMPTS = 8
_WATCH_BACKOFF_S = 0.02
_TERMINAL_TASK_STATUSES = {"completed", "failed", "interrupted"}


class BugLockActive(Exception):
    """Dispatch blocked because the target product is under an active bug lock."""


class OrchTaskNotReady(Exception):
    """Dispatch blocked because the OrchTask is not ready at claim time."""


class WorkerBusy(Exception):
    """Dispatch blocked because another live dispatcher already owns the worker slot."""


def _base_session_name(worker: str) -> str:
    for suffix in ("-codex", "-gemini", "-grok", "-claude"):
        if worker.endswith(suffix):
            return worker[: -len(suffix)]
    return worker


def _resolve_product_id(worker: str) -> Optional[str]:
    return OrchConfig().product_owner_map.get(_base_session_name(worker))


def _redis_connect():
    """Connect to Redis via the fleet-notify identity module."""
    return _notify_redis_connect()


def _state_key(node_id: str, suffix: str) -> str:
    return _notify_state_key(node_id, suffix)


def _decode_current_task(raw: Optional[str]) -> Optional[dict[str, Any]]:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _current_task_status(task_id: str) -> Optional[str]:
    if not task_id or not _orch_task_exists(task_id):
        return None
    cfg = OrchConfig()
    with get_neo4j_session(cfg) as session:
        row = session.run(
            "MATCH (t:OrchTask {id: $task_id}) RETURN coalesce(t.status, 'pending') AS status",
            task_id=task_id,
        ).single()
    return str(row["status"]) if row else None


def _busy_current_task_error(worker: str, existing: Optional[dict[str, Any]],
                             dispatcher: Optional[str]) -> Optional[WorkerBusy]:
    if not existing:
        return None
    existing_dispatcher = str(existing.get("dispatcher") or existing.get("supervisor") or "").strip()
    incoming_dispatcher = str(dispatcher or "").strip()
    if existing_dispatcher == incoming_dispatcher:
        return None
    existing_task_id = str(existing.get("task_id") or "").strip()
    status = _current_task_status(existing_task_id)
    if status in _TERMINAL_TASK_STATUSES:
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
                if not force:
                    busy = _busy_current_task_error(worker, _decode_current_task(pipe.get(key)), dispatcher)
                    if busy:
                        pipe.unwatch()
                        raise busy
                pipe.multi()
                pipe.delete(_state_key(worker, "last_outcome"))
                pipe.delete(_state_key("orch-watch-stuck", f"{worker}:{current_task['task_id']}"))
                pipe.set(key, json.dumps(current_task))
                if set_parent and supervisor:
                    pipe.set(_state_key(worker, "parent"), supervisor)
                pipe.execute()
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


def _claim_ready_orch_task(task_id: str, worker: str) -> None:
    if not _orch_task_exists(task_id):
        return

    cfg = OrchConfig()
    owner = _base_session_name(worker)
    with get_neo4j_session(cfg) as session:
        record = session.run(
            """
            MATCH (t:OrchTask {id: $task_id})
            SET t._claim_lock = true
            WITH t, coalesce(t.status, 'pending') AS prior_status
            WHERE (
                  prior_status = 'pending'
                  OR (prior_status = 'completed' AND coalesce(t.recurring, false) = true)
              )
              AND NOT EXISTS {
                  MATCH (t)-[:DEPENDS_ON]->(dep:OrchTask)
                  WHERE dep.status <> 'completed'
              }
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
        ).single()

        if record is not None:
            return

        detail = session.run(
            """
            MATCH (t:OrchTask {id: $task_id})
            OPTIONAL MATCH (t)-[:DEPENDS_ON]->(dep:OrchTask)
            RETURN coalesce(t.status, 'pending') AS status,
                   count(CASE WHEN dep.status <> 'completed' THEN 1 END) AS incomplete_deps
            """,
            task_id=task_id,
        ).single()

    if detail is None:
        return

    raise OrchTaskNotReady(
        f"ORCH_TASK_NOT_READY task={task_id} status={detail['status']} "
        f"incomplete_deps={detail['incomplete_deps']}"
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
    owner = _base_session_name(worker)
    with get_neo4j_session(cfg) as session:
        record = session.run(
            """
            MATCH (t:OrchTask {id: $task_id})
            SET t._claim_lock = true
            WITH t
            WHERE coalesce(t.status, 'pending') = 'pending'
              AND NOT EXISTS {
                  MATCH (t)-[:DEPENDS_ON]->(dep:OrchTask)
                  WHERE dep.status <> 'completed'
              }
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
    """Record the task on the worker side and inject the prompt.

    Side effects (in order):

    1. If the worker maps to a product in ``ORCH_PRODUCT_OWNER_MAP`` / ``PRODUCT_OWNER_MAP`` and that
       product has ``support:product:<id>:bug_lock == "true"``, raise
       ``BugLockActive`` before any worker-state mutation unless
       ``is_bugfix=True``.
    2. Write ``taey:<worker>:current_task`` JSON {task_id, description,
       supervisor, started_at} — the universal Stop hook reads this to
       build its supervisor-notify body. Cleared by the Stop hook after
       the supervisor is notified.
    3. If ``supervisor`` is provided, write ``taey:<worker>:parent`` so
       the Stop hook addresses notifications correctly even for multi-
       level trees (where suffix-strip wouldn't reach the right node).
    4. Inject the prompt by invoking ``taey-notify <worker> <body>``,
       which the released fleet-notify daemon will pick up and deliver
       via tmux as soon as the worker is idle.

    The prompt body defaults to a JSON envelope wrapping {task_id,
    description} so the worker can identify what it's being asked to do.
    Pass ``prompt_body`` to override with custom text.
    """
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

    from_session = supervisor or os.environ.get("TAEY_NODE_ID", "dispatch")

    _claim_ready_orch_task(task_id=task_id, worker=worker)
    try:
        binding_nonce = bind_current_task(
            worker=worker,
            task_id=task_id,
            description=description,
            supervisor=supervisor,
            set_parent=bool(supervisor),
            force=force,
            guard_existing=True,
            dispatcher=from_session,
        )
    except WorkerBusy:
        _rollback_claim_only(worker, task_id)
        raise
    mark_superseded_for_task(_redis_connect(), from_session, task_id)

    if prompt_body is None:
        prompt_body = (
            f"DISPATCH task={task_id}\n"
            f"description: {description}\n"
            f"(Recorded as your current_task in Redis. Your Stop hook will "
            f"notify the supervisor when you finish.)"
        )

    # Standard record_outcome footer: workers reliably forget to call
    # record_outcome unless told explicitly. Without it, peer_idle reaches the
    # supervisor with
    # outcome=unknown and outcome_details=None — the supervisor can't
    # tell clean-finish from error-restart from interrupted, and the
    # CAS done-clear never fires so current_task persists as "previous
    # dispatch did not complete cleanly" (dispatch persistence rule).
    # Auto-append unless caller opts out by passing the footer themselves
    # (we detect via 'record_outcome' substring already present).
    if "record_outcome" not in prompt_body:
        prompt_body = prompt_body.rstrip() + (
            "\n\n---\nWHEN DONE — call record_outcome so the supervisor knows "
            "the result (otherwise outcome=unknown + current_task persists as "
            "'previous dispatch unresolved'). One line via bash tool:\n\n"
            f"python3 -c \"from fleet_orchestrator import record_outcome; "
            f"record_outcome('{worker}', 'done', '<short outcome summary>')\"\n\n"
            "Replace 'done' with 'error' or 'interrupted' if the task did not "
            "complete cleanly. The '<short outcome summary>' is your one-line "
            "report — what landed, what's at /tmp/..., what's the verdict."
        )

    cli = OrchConfig().notify_cli_path
    result = subprocess.run(
        [
            cli,
            worker,
            prompt_body,
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
        raise RuntimeError(result.stderr.strip() or f"{cli} failed")
    maybe_emit_decision_receipt(
        "wake",
        {
            "why_this_context": "dispatch delivered a task wake through taey-notify",
            "refs_used": [],
            "rule_tier_applied": "dispatch",
            "observable_state": {
                "source": "dispatch",
                "worker": worker,
                "task_id": task_id,
                "supervisor": supervisor,
                "priority": priority,
                "prompt_sha256": hashlib.sha256(prompt_body.encode("utf-8")).hexdigest(),
            },
            "target": worker,
            "task_id": task_id,
            "next_contract": "worker records outcome when the dispatched task is complete",
        },
    )


_VALID_OUTCOMES = ("done", "error", "interrupted")


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
    details = str(payload.get("details") or "").strip()
    body = (
        f"{worker} reported done"
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


def record_outcome(worker: str, outcome: str, details: Optional[str] = None) -> None:
    """Worker-side helper: record the task outcome before stopping.

    ``outcome`` MUST be one of ``done``, ``error``, ``interrupted``. Any
    other value raises ``ValueError`` — the enum is load-bearing per the
    the Stop hook clears the worker's current_task ONLY when outcome == ``done``. Any other
    outcome (or absent record_outcome call entirely) leaves current_task
    persisting as the "previous dispatch did not complete cleanly"
    signal for the next dispatcher.

    Semantics:
    - ``done`` — task completed successfully. Supervisor can move on.
    - ``error`` — task hit an unrecoverable error. current_task persists;
      supervisor sees outcome=error in peer_idle body and decides retry
      vs investigation.
    - ``interrupted`` — Ctrl-C, timeout, or external cancel. current_task
      persists; supervisor knows the task was not given a fair shot.

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
    if outcome == "done":
        current_task = _decode_current_task(r.get(current_task_key))
        payload = _outcome_payload(outcome, details, current_task)
        r.set(last_outcome_key, json.dumps(payload))
        _notify_supervisor_response_ready(worker, current_task, payload)
        return

    from redis import WatchError

    current_task_id: Optional[str] = None
    for _attempt in range(_WATCH_MAX_ATTEMPTS):
        with r.pipeline() as pipe:
            try:
                pipe.watch(current_task_key)
                current_task_raw = pipe.get(current_task_key)
                current_task_id = _current_task_id(current_task_raw)
                payload = _outcome_payload(outcome, details, _decode_current_task(current_task_raw))
                pipe.multi()
                pipe.set(last_outcome_key, json.dumps(payload))
                pipe.execute()
                break
            except WatchError:
                # Bounded retry + small backoff so a hot current_task key cannot
                # livelock this loop (grok ws2-state WATCH-livelock note).
                time.sleep(_WATCH_BACKOFF_S * (_attempt + 1))
                continue
    if current_task_id:
        _revert_outcome_claim(worker, current_task_id)


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


def clear_current_task(worker: str) -> None:
    """Force-clear the worker's current_task + last_outcome keys.

    Normally the Stop hook clears these after notifying the supervisor —
    but ONLY when the recorded outcome was ``done``. For ``error`` /
    ``interrupted`` / ``unknown`` outcomes the keys persist as a signal
    to the next dispatcher. This helper is the supervisor's explicit
    "I've seen the previous task's outcome, I'm moving on" acknowledgment
    — call it after investigating or after deciding to cancel.
    """
    r = _redis_connect()
    r.delete(_state_key(worker, "current_task"))
    r.delete(_state_key(worker, "last_outcome"))
