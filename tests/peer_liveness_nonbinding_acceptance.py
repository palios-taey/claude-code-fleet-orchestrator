"""Ship-gate e2e — NON-BINDING peer liveness (the codex/grok busy-loop fix).

codex/grok CLIs never bind current_task and have no working-heartbeat, so the
PR#39 binding check (current_task==T + idle + heartbeat) can NEVER see them as
working -> the supervisor busy-looped on EVERY codex/grok dispatch. Their one
reliable queryable signal is last_outcome (set on done/error) + RESPONSE_READY.

_peer_actively_working_task now, for a worker with NO current_task:
  - presume working (True) -> caller ALLOW_STOPs, RESPONSE_READY re-wakes;
  - UNLESS it reported the task terminal (done/error/interrupt) -> awaits gate -> False;
  - UNLESS the dispatch is older than _PEER_DISPATCH_STALE_SEC with no terminal
    outcome -> dropped/dead -> re-check -> False.
Binding peers (claude-clones) keep the precise PR#39 path. Production oracle:
live Neo4j 10.0.0.163:7689 + Redis 127.0.0.1.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.orch_schema import (  # noqa: E402
    create_project, create_phase, create_task, update_task_status, init_schema,
    get_neo4j_driver, _state_key, _peer_actively_working_task,
    _PEER_DISPATCH_STALE_SEC,
)
from lib.config import OrchConfig, get_redis_sync  # noqa: E402

CFG = OrchConfig()
_R = get_redis_sync(CFG)
_PFX = f"nbliven-ci-{uuid.uuid4().hex[:8]}"
_SUP = f"{_PFX}-sup"
_PEER = f"{_SUP}-codex"   # a non-binding CLI peer (codex)
_FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: str = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        _FAILURES.append(label)


def _no_binding(worker: str) -> None:
    """A non-binding peer: NO current_task ever (codex/grok reality)."""
    for k in ("current_task", "idle", "last_activity", "last_outcome"):
        _R.delete(_state_key(worker, k))


def _set_outcome(worker: str, outcome: str, task_id: str) -> None:
    _R.set(_state_key(worker, "last_outcome"),
           json.dumps({"outcome": outcome, "details": f"{outcome.upper()} [{task_id}]"}))


def _age_task(drv, task_id: str, seconds_ago: float) -> None:
    """Backdate updated_at so the dispatch looks stale (dropped/dead)."""
    with drv.session(database=CFG.neo4j_db) as s:
        s.run("MATCH (t:OrchTask {id:$i}) SET t.updated_at = datetime() - duration({seconds: $sec})",
              i=task_id, sec=int(seconds_ago))


def main() -> int:
    init_schema(config=CFG)
    drv = get_neo4j_driver(CFG)
    with drv.session(database=CFG.neo4j_db) as s:
        s.run("MATCH (n) WHERE n.id STARTS WITH $p DETACH DELETE n", p=_PFX)
    try:
        P = f"{_PFX}-A"
        create_project(project_id=P, name=P, supervisor=_SUP, config=CFG)
        create_phase(project_id=P, phase_id=f"{P}::ph", name="ph", config=CFG)
        T = f"{P}::codexwork"
        create_task(phase_id=f"{P}::ph", task_id=T, description="codex build", owner=_PEER,
                    wake_owner_if_ready=False, config=CFG)
        update_task_status(T, "in_progress", owner=_PEER, config=CFG)  # fresh dispatch

        # 1. THE FIX: non-binding peer, fresh dispatch, no terminal outcome -> WORKING (True)
        _no_binding(_PEER)
        _check("non-binding + fresh + no-outcome -> WORKING (busy-loop fixed)",
               _peer_actively_working_task([_PEER], T, config=CFG) is True)

        # 2. reported DONE -> awaits gate -> NOT working (False)
        _set_outcome(_PEER, "done", T)
        _check("non-binding + last_outcome=done(this task) -> NOT working (gate)",
               _peer_actively_working_task([_PEER], T, config=CFG) is False)

        # 3. reported ERROR -> needs investigation -> NOT working (False)
        _set_outcome(_PEER, "error", T)
        _check("non-binding + last_outcome=error(this task) -> NOT working (investigate)",
               _peer_actively_working_task([_PEER], T, config=CFG) is False)

        # 4. last_outcome is for a DIFFERENT task -> not terminal-for-T -> WORKING (True)
        _set_outcome(_PEER, "done", f"{P}::someoldtask")
        _check("non-binding + last_outcome=done(OTHER task) -> WORKING (still on T)",
               _peer_actively_working_task([_PEER], T, config=CFG) is True)

        # 5. DROPPED/DEAD dispatch: no terminal outcome but dispatch is stale -> NOT working
        _no_binding(_PEER)
        _age_task(drv, T, _PEER_DISPATCH_STALE_SEC + 600)
        _check("non-binding + stale dispatch + no-outcome -> NOT working (dropped/dead, re-check)",
               _peer_actively_working_task([_PEER], T, config=CFG) is False)

        # 6. BINDING peer regression: current_task bound + fresh heartbeat -> WORKING (True)
        with drv.session(database=CFG.neo4j_db) as s:
            s.run("MATCH (t:OrchTask {id:$i}) SET t.updated_at = datetime()", i=T)  # refresh
        _R.set(_state_key(_PEER, "current_task"), json.dumps({"task_id": T, "started_at": time.time()}))
        _R.delete(_state_key(_PEER, "idle"))
        _R.set(_state_key(_PEER, "last_activity"), str(time.time()))
        _check("BINDING peer bound + fresh heartbeat -> WORKING (PR#39 path intact)",
               _peer_actively_working_task([_PEER], T, config=CFG) is True)
    finally:
        _no_binding(_PEER)
        with drv.session(database=CFG.neo4j_db) as s:
            s.run("MATCH (n) WHERE n.id STARTS WITH $p DETACH DELETE n", p=_PFX)
    if _FAILURES:
        print(f"\nFAIL — {len(_FAILURES)}: {_FAILURES}")
        return 1
    print("\nPASS — non-binding (codex/grok) liveness: presume-working-unless-terminal-or-stale; binding path intact.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
