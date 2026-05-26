"""Default readiness checker for orch-watch's done-DEL signal.

Wires ``orch-watch --readiness-checker lib.plan_readiness:check_readiness``
so that when a worker's Stop hook clears ``current_task`` on outcome=done,
the watchloop queries the OrchTask graph to see if any of the supervisor's
owned tasks just became ready (their last pending dependency was the task
that just completed), and pages the supervisor if so.

The interface contract (matches ``orch-watch``'s expectation):

    def check_readiness(supervisor: str, completed_task: dict) -> Optional[str]:
        '''Return a wake message body if this completion unblocked work
        for an idle supervisor, else None.'''

``completed_task`` is the dict that was in ``taey:<worker>:current_task``
immediately before the Stop hook's CAS clear: ``{task_id, description,
supervisor, started_at}``. Note ``completed_task['supervisor']`` may be
None for top-level dispatches; the ``supervisor`` argument is the
resolved supervisor that orch-watch already determined.

Semantic choice: LOOSE — wake only on the TRANSITION
(was-blocked → just-became-ready), not on every completion of an owned
task. Reasoning per Phase B/C/D consultation grain analysis (Gaia): a
task with N dependencies completing in sequence shouldn't wake the
supervisor N times — only the last completion that flips the dependent
task from blocked → ready is actionable. Tasks that become ready for
other reasons (e.g., the supervisor manually unblocked one) are pulled
on the supervisor's next loop, not paged.

The check is best-effort: if Neo4j is unreachable or the schema lookup
fails, the function returns None (no wake) rather than raising — the
stuck-task safety-net sweep + the supervisor's own ``taey-plan
next-ready`` poll will catch any missed wake. Strict consistency is not
the orchestrator's job; the plan tracker is the source of truth, this
is just a wake-on-transition heuristic.

If the operator wants STRICT semantics ("wake on any completion of an
owned dependency"), they can write their own checker following the
same signature and pass it via ``orch-watch --readiness-checker
their.module:their_fn``. Phase D ships LOOSE as the default because
it's the lower-noise grain that maps directly to the "no idle
supervisor sits on actionable work" objective.
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Optional

# Support being loaded as a sibling module by importlib.util.spec_from_file_location
# (the path orch-watch's --readiness-checker uses) AND as part of the lib
# package (the path tasks_api uses). Add the orchestrator root to sys.path
# so absolute `from lib.config import ...` works in both contexts.
_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from lib.config import OrchConfig, get_neo4j_session  # noqa: E402

log = logging.getLogger(__name__)


# Cypher: find tasks T newly-unblocked by ``completed_task_id``'s completion.
#
#   - T is owned by the supervisor (or unowned)
#   - T has a DEPENDS_ON edge to the completed task
#   - T's status is pending or blocked (not already in_progress / completed)
#   - All of T's OTHER dependencies are status=completed
#   - Self-loops excluded (t.id != completed_task_id) — guards against the
#     degenerate case where a task depends on itself (Gaia Phase D edge #4).
#
# Single read transaction = consistent snapshot for one process. The
# CONCURRENT-FINALS edge case (two workers each completing the last two
# deps of the same downstream T, each seeing the OTHER as still-pending)
# is handled by a Redis SETNX dedup keyed on T.id AFTER the read — see
# the "wake_dedup" block below.
_READY_TRANSITION_CYPHER = """
MATCH (completed:OrchTask {id: $completed_task_id})
MATCH (t:OrchTask)-[:DEPENDS_ON]->(completed)
WHERE (t.owner = $supervisor OR t.owner IS NULL OR t.owner = '')
  AND (t.status IS NULL OR t.status IN ['pending', 'blocked'])
  AND t.id <> $completed_task_id
WITH t, completed
OPTIONAL MATCH (t)-[:DEPENDS_ON]->(other:OrchTask)
WHERE other.id <> $completed_task_id AND other.id <> t.id
WITH t, completed, collect(other) AS others
WHERE ALL(o IN others WHERE o.status = 'completed')
RETURN
  t.id          AS task_id,
  t.description AS description,
  t.priority    AS priority,
  t.owner       AS owner,
  size(others)  AS other_dep_count
ORDER BY coalesce(t.priority, 50) DESC, t.id
LIMIT 5
"""


def _dedup_wake(redis_client, downstream_task_id: str, ttl_sec: int = 600) -> bool:
    """SETNX on a per-downstream-task wake-dedup key. Returns True if this
    caller owns the wake (the key didn't exist), False if another process
    already fired it within the TTL window.

    Handles Gaia Phase D edge #3 (concurrent final completions): when two
    workers complete the final two dependencies of the same downstream
    task T near-simultaneously, both orch-watch processes will run the
    LOOSE check and both will see T as newly ready. Without dedup, both
    fire a wake — supervisor sees the same notification twice. With
    SETNX, the first wins; the second observes the key and skips.

    The 10-min TTL is short enough that a re-completion (e.g., supervisor
    cancels + re-runs T's dependency, T becomes ready again later) gets
    a fresh wake; long enough to absorb realistic race windows.

    Best-effort: returns True (allow the wake) on any Redis failure so
    the supervisor doesn't silently miss the wake just because Redis
    blipped.
    """
    if redis_client is None:
        return True  # No dedup available — fall through to wake.
    try:
        key = f"taey:orch-wake-fired:{downstream_task_id}"
        return bool(redis_client.set(key, "1", nx=True, ex=ttl_sec))
    except Exception as exc:
        log.debug("wake dedup SETNX failed for %s: %s — allowing wake.",
                  downstream_task_id, exc)
        return True


def check_readiness(supervisor: str, completed_task: dict) -> Optional[str]:
    """Return a wake message body if ``completed_task``'s completion just
    transitioned one or more OrchTasks owned-by-supervisor from blocked
    to ready. Else return None.

    Called from ``orch-watch`` when the Stop hook's CAS done-clear fires.
    Best-effort: returns None on any Neo4j failure rather than raising.

    LOOSE semantic + edge-case handling (Gaia Phase D consultation
    2026-05-26):
    - Self-loops on the completed task or on T excluded from the Cypher.
    - Concurrent finals: SETNX per-downstream-task dedup so two near-
      simultaneous final completions don't both fire.
    - Zero-dep tasks (no DEPENDS_ON edges at all): never reach this
      function (they have no completion event to trigger the check) —
      they need a separate "wake-at-creation" path. Tracked in
      docs/STATUS.md as a v0.4.1 follow-up.
    - Already-completed deps at edge-creation time: tracked as a
      v0.4.1 follow-up; resolve at write time in
      ``orch_schema.add_dependency`` rather than here.
    """
    completed_task_id = completed_task.get("task_id")
    if not completed_task_id:
        return None

    try:
        cfg = OrchConfig()
        with get_neo4j_session(cfg) as session:
            result = session.run(
                _READY_TRANSITION_CYPHER,
                completed_task_id=completed_task_id,
                supervisor=supervisor,
            )
            newly_ready = [dict(record) for record in result]
    except Exception as exc:
        log.debug("plan_readiness check failed: %s", exc)
        return None

    if not newly_ready:
        return None

    # Concurrent-finals dedup: apply SETNX per-downstream-task so two
    # near-simultaneous final completions don't double-fire on the same T.
    # Filter the list to just tasks where THIS process owns the wake.
    #
    # IMPORTANT: use the LOCAL fleet Redis (where orch-watch's dedup keys
    # live), not lib.config's orch-layer Redis (which on multi-machine
    # deployments points at a Spark cluster Redis that may be unreachable
    # from the orchestrator host). Same Redis the rest of the orchestrator
    # stack uses (orch-watch / lib/dispatch / fleet-notify daemon).
    redis_client = None
    try:
        # Try fleet-notify's identity module first — same primitive every
        # orchestrator component uses for local Redis access.
        for _p in (
            "/usr/local/lib/claude-code-fleet-notify",
            "/path/to/repo",
        ):
            if os.path.isdir(_p) and _p not in sys.path:
                sys.path.insert(0, _p)
        from identity import redis_connect  # type: ignore
        redis_client = redis_connect()
    except Exception:
        # Fall back to a direct local-Redis client if fleet-notify isn't
        # installed at the standard paths.
        try:
            import redis as _redis
            redis_client = _redis.Redis(
                host=os.environ.get("REDIS_HOST", "127.0.0.1"),
                port=int(os.environ.get("REDIS_PORT", "6379")),
                decode_responses=True,
                socket_timeout=2,
            )
        except Exception:
            redis_client = None

    owned = [t for t in newly_ready if _dedup_wake(redis_client, t["task_id"])]
    if not owned:
        return None
    newly_ready = owned

    if len(newly_ready) == 1:
        t = newly_ready[0]
        return (
            f"UNBLOCK: completion of task={completed_task_id} just unblocked "
            f"task={t['task_id']} (\"{(t.get('description') or '')[:80]}\") "
            f"owned by you. {t.get('other_dep_count', 0)} other deps were "
            f"already done. Pick it up with `taey-plan next` or dispatch a "
            f"worker."
        )

    # Multiple tasks unblocked by this single completion — list the top.
    lines = [
        f"UNBLOCK: completion of task={completed_task_id} just unblocked "
        f"{len(newly_ready)} of your tasks:"
    ]
    for t in newly_ready[:5]:
        lines.append(
            f"  - {t['task_id']}: \"{(t.get('description') or '')[:80]}\" "
            f"(priority {t.get('priority', 'n/a')})"
        )
    lines.append("Pick the highest-priority one with `taey-plan next`.")
    return "\n".join(lines)
