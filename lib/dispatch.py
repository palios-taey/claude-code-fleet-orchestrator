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
             prompt_body=None, priority="normal") -> None

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
        prompt_body="Run the scout per /path/to/repo ...",
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


def _redis_connect():
    """Connect to Redis via the fleet-notify identity module so we honor
    REDIS_HOST / REDIS_PORT / NOTIFY_KEY_PREFIX the same way hooks do."""
    # Try the installed fleet-notify path first, fall back to canonical clone.
    for path in (
        "/usr/local/lib/claude-code-fleet-notify",
        "/path/to/repo",
    ):
        if os.path.isdir(path):
            sys.path.insert(0, path)
            break
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


def dispatch(
    worker: str,
    task_id: str,
    description: str,
    supervisor: Optional[str] = None,
    prompt_body: Optional[str] = None,
    priority: str = "normal",
) -> None:
    """Record the task on the worker side and inject the prompt.

    Side effects (in order):

    1. Write ``taey:<worker>:current_task`` JSON {task_id, description,
       supervisor, started_at} — the universal Stop hook reads this to
       build its supervisor-notify body. Cleared by the Stop hook after
       the supervisor is notified.
    2. If ``supervisor`` is provided, write ``taey:<worker>:parent`` so
       the Stop hook addresses notifications correctly even for multi-
       level trees (where suffix-strip wouldn't reach the right node).
    3. Inject the prompt by invoking ``taey-notify <worker> <body>``,
       which the released fleet-notify daemon will pick up and deliver
       via tmux as soon as the worker is idle.

    The prompt body defaults to a JSON envelope wrapping {task_id,
    description} so the worker can identify what it's being asked to do.
    Pass ``prompt_body`` to override with custom text.
    """
    r = _redis_connect()
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
            f"python3 -c \"import sys; sys.path.insert(0,'/path/to/repo'); "
            f"from lib.dispatch import record_outcome; "
            f"record_outcome('{worker}', 'done', '<short outcome summary>')\"\n\n"
            "Replace 'done' with 'error' or 'interrupted' if the task did not "
            "complete cleanly. The '<short outcome summary>' is your one-line "
            "report — what landed, what's at /tmp/..., what's the verdict."
        )

    # Use the released taey-notify CLI so the message routes through the
    # canonical inbox + daemon path. Falls back to direct Redis push if
    # the CLI is missing (e.g., in a stripped test environment).
    cli = "/usr/local/bin/taey-notify"
    if os.path.isfile(cli) and os.access(cli, os.X_OK):
        from_session = supervisor or os.environ.get("TAEY_NODE_ID", "dispatch")
        subprocess.run(
            [cli, worker, prompt_body, "--from", from_session,
             "--type", "command", "--priority", priority],
            check=False,
        )
    else:
        msg = json.dumps({
            "from": supervisor or "dispatch",
            "type": "command",
            "body": prompt_body,
            "priority": priority,
            "msg_id": f"dispatch-{task_id}-{int(time.time())}",
            "timestamp": time.time(),
        })
        r.lpush(_state_key(worker, "inbox"), msg)


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
