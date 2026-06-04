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
             is_bugfix=False) -> None

The ``supervisor`` argument is informational — it's stamped on the
current_task payload so the Stop hook knows who to address even when
the suffix-strip rule wouldn't reach them (e.g., multi-level trees).
For single-level use, leave it None and the Stop hook uses
``<worker-name>-<cli>`` suffix-strip → supervisor.

Usage from a Python supervisor session::

    from lib.dispatch import dispatch

    dispatch(
        worker="treasurer-codex",
        task_id="reddit-scout-cycle-22",
        description="Scout r/MachineLearning for acute-pain replies",
        prompt_body="Run the deployment's scout workflow.",
    )

CLI equivalent::

    orch-dispatch treasurer-codex reddit-scout-cycle-22 \\
        --description "Scout r/MachineLearning for acute-pain replies" \\
        --prompt-file /tmp/scout.prompt

Once dispatched, the worker receives the prompt via the released
``claude-code-fleet-notify`` daemon (idle=1 + inbox>0 → tmux inject).
When the worker stops, its Stop hook reads current_task and notifies
the supervisor with the task id + description + duration in the
peer_idle body.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from typing import Optional

from .config import OrchConfig, ensure_notify_importable, get_neo4j_session
from .handoff_validation import mark_superseded_for_task


class BugLockActive(Exception):
    """Dispatch blocked because the target product is under an active bug lock."""


class OrchTaskNotReady(Exception):
    """Dispatch blocked because the OrchTask is not ready at claim time."""


def _base_session_name(worker: str) -> str:
    for suffix in ("-codex", "-gemini", "-grok", "-claude"):
        if worker.endswith(suffix):
            return worker[: -len(suffix)]
    return worker


def _resolve_product_id(worker: str) -> Optional[str]:
    return OrchConfig().product_owner_map.get(_base_session_name(worker))


def _redis_connect():
    """Connect to Redis via the fleet-notify identity module."""
    ensure_notify_importable()
    from identity import redis_connect
    return redis_connect()


def _state_key(node_id: str, suffix: str) -> str:
    """Reproduce fleet-notify's state_key without importing — keeps this
    module usable in contexts where the notifications package isn't on
    sys.path."""
    prefix = os.environ.get("NOTIFY_KEY_PREFIX", "taey")
    return f"{prefix}:{node_id}:{suffix}"


def bind_current_task(
    worker: str,
    task_id: str,
    description: str,
    supervisor: Optional[str] = None,
    set_parent: bool = False,
) -> None:
    """Write the canonical dispatch/current-task wire for ``worker``.

    This is the Redis-side half of the dispatch primitive, factored out so
    non-dispatch task flows (for example self-owned ``taey-task`` work) can
    mirror the exact same state shape that the Stop hook and orch-watch
    already understand.
    """
    r = _redis_connect()
    current_task = {
        "task_id": task_id,
        "description": description,
        "supervisor": supervisor,
        "started_at": time.time(),
    }

    pipe = r.pipeline(transaction=True)
    pipe.delete(_state_key(worker, "last_outcome"))
    pipe.delete(f"taey:orch-watch-stuck:{worker}:{task_id}")
    pipe.set(_state_key(worker, "current_task"), json.dumps(current_task))
    if set_parent and supervisor:
        pipe.set(_state_key(worker, "parent"), supervisor)
    pipe.execute()


def _orch_task_exists(task_id: str) -> bool:
    cfg = OrchConfig()
    with get_neo4j_session(cfg) as session:
        record = session.run(
            "MATCH (t:OrchTask {id: $task_id}) RETURN t.id AS id",
            task_id=task_id,
        ).single()
    return record is not None


def _claim_ready_orch_task(task_id: str, worker: str, allow_reclaim: bool = False) -> None:
    if not _orch_task_exists(task_id):
        return

    cfg = OrchConfig()
    with get_neo4j_session(cfg) as session:
        record = session.run(
            """
            MATCH (t:OrchTask {id: $task_id})
            WITH t, coalesce(t.status, 'pending') AS prior_status
            WHERE (
                    prior_status = 'pending'
                    OR ($allow_reclaim AND prior_status IN ['completed', 'in_progress', 'failed', 'interrupted'])
                  )
              AND NOT EXISTS {
                  MATCH (t)-[:DEPENDS_ON]->(dep:OrchTask)
                  WHERE dep.status <> 'completed'
              }
            SET t.status = 'in_progress',
                t.owner = $worker,
                t.blocked_on = NULL,
                t.result = '',
                t.completion_evidence = NULL,
                t.completed_by = NULL,
                t.completed_at = NULL,
                t.dispatch_cycle = CASE
                    WHEN t.dispatch_cycle IS NULL OR t.dispatch_cycle = '' THEN 1
                    ELSE toInteger(t.dispatch_cycle) + 1
                END,
                t.last_claim_mode = CASE
                    WHEN $allow_reclaim AND prior_status <> 'pending' THEN 'reclaim'
                    ELSE 'claim'
                END,
                t.last_claim_from_status = prior_status,
                t.updated_at = datetime()
            RETURN t.id AS task_id, prior_status AS prior_status, t.dispatch_cycle AS dispatch_cycle
            """,
            task_id=task_id,
            worker=worker,
            allow_reclaim=allow_reclaim,
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


def dispatch(
    worker: str,
    task_id: str,
    description: str,
    supervisor: Optional[str] = None,
            prompt_body: Optional[str] = None,
            priority: str = "normal",
            is_bugfix: bool = False,
            allow_reclaim: bool = False,
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

    _claim_ready_orch_task(task_id=task_id, worker=worker, allow_reclaim=allow_reclaim)
    mark_superseded_for_task(_redis_connect(), from_session := (supervisor or os.environ.get("TAEY_NODE_ID", "dispatch")), task_id)

    bind_current_task(
        worker=worker,
        task_id=task_id,
        description=description,
        supervisor=supervisor,
        set_parent=bool(supervisor),
    )

    if prompt_body is None:
        prompt_body = (
            f"DISPATCH task={task_id}\n"
            f"description: {description}\n"
            f"(Recorded as your current_task in Redis. Your Stop hook will "
            f"notify the supervisor when you finish.)"
        )

    # Standard record_outcome footer (Treasurer ergonomic finding 2026-
    # 05-26): workers reliably forget to call record_outcome unless told
    # explicitly. Without it, peer_idle reaches the supervisor with
    # outcome=unknown and outcome_details=None — the supervisor can't
    # tell clean-finish from error-restart from interrupted, and the
    # CAS done-clear never fires so current_task persists as "previous
    # dispatch did not complete cleanly" (Gaia persistence rule).
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
        raise RuntimeError(result.stderr.strip() or f"{cli} failed")


_VALID_OUTCOMES = ("done", "error", "interrupted")


def record_outcome(worker: str, outcome: str, details: Optional[str] = None) -> None:
    """Worker-side helper: record the task outcome before stopping.

    ``outcome`` MUST be one of ``done``, ``error``, ``interrupted``. Any
    other value raises ``ValueError`` — the enum is load-bearing per the
    Phase A consultation (Gaia 2026-05-26): the Stop hook clears the
    worker's current_task ONLY when outcome == ``done``. Any other
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
    payload = {"outcome": outcome}
    if details:
        payload["details"] = details[:500]
    r.set(_state_key(worker, "last_outcome"), json.dumps(payload))


def check_previous_task(worker: str) -> Optional[dict]:
    """Supervisor-side helper: inspect a worker's current_task before dispatch.

    Returns ``None`` if the worker has no current_task (clean to dispatch).
    Returns the current_task dict {task_id, description, supervisor,
    started_at, last_outcome?} if a previous dispatch is unresolved
    (outcome was not ``done``, so the Stop hook left current_task in place).

    Supervisors SHOULD call this before issuing new dispatches. The
    common pattern:

        prev = check_previous_task("treasurer-codex")
        if prev:
            # Previous dispatch didn't complete cleanly. Decide:
            # - retry: re-dispatch the same task body
            # - investigate: open the worker pane, look at the error
            # - cancel: clear_current_task() and move on
            ...
        dispatch("treasurer-codex", ...)
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
