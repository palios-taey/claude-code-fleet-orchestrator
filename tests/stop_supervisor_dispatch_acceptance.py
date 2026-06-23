"""Ship-gate e2e — supervisor keep-going is DEFAULT-ON (no flag) and blocks the
stop ONLY when there is peer work for the SUPERVISOR to act on.

The stop-engine's whole purpose is that a supervisor does not stop while there is fleet
work that NEEDS IT. The own-ready loop only surfaces the supervisor's OWN tasks; a
supervised active project with peer-owned work must BLOCK the stop when the supervisor
must act:
  - pending  -> must DISPATCH it;
  - in-flight AND the peer is NOT actively working it (reported done -> gate, or
    stalled -> investigate) -> must act.
But in-flight work that the peer IS actively working (its live current_task is bound to
the task) does NOT block: the peer's RESPONSE_READY re-wakes the supervisor when done, so
blocking during that window is a busy-loop with nothing to do (the repeated-Stop-hook
symptom). There is no flag to turn keep-going off; the actively-working carve-out is the
difference between "wait for the right moment" and "spin uselessly."

Env: ORCH_NEO4J_URI, ORCH_NEO4J_DB, ORCH_REDIS_HOST/PORT, ORCH_DASHBOARD_URL,
     ORCH_TEST_NAMESPACE (required; must include test/ci/acceptance).
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet_orchestrator.orch_schema import (  # noqa: E402
    create_project, create_phase, create_task, update_task_status,
    _raw_stop_decision, init_schema, get_neo4j_driver, _state_key,
    _PEER_HEARTBEAT_STALE_SEC, set_project_user_stop_conditions,
)
from fleet_orchestrator.config import OrchConfig  # noqa: E402
from fleet_orchestrator.notify_state import redis_connect as notify_redis_connect  # noqa: E402


def _require_test_namespace() -> str:
    raw = (os.environ.get("ORCH_TEST_NAMESPACE") or "").strip()
    if not raw:
        raise SystemExit(
            "ORCH_TEST_NAMESPACE is required so this acceptance cannot run against production Neo4j without an isolated namespace"
        )
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{5,80}", raw):
        raise SystemExit("ORCH_TEST_NAMESPACE must be 6-80 chars of letters/digits/._:-")
    lowered = raw.lower()
    if not any(marker in lowered for marker in ("test", "ci", "acceptance")):
        raise SystemExit("ORCH_TEST_NAMESPACE must include test, ci, or acceptance")
    if lowered in {"prod", "production", "neo4j", "default"}:
        raise SystemExit("ORCH_TEST_NAMESPACE must not name a production/default namespace")
    return raw


_NAMESPACE = _require_test_namespace()
CFG = OrchConfig()
_R = notify_redis_connect()
_PFX = f"{_NAMESPACE}-supkeep-{uuid.uuid4().hex[:8]}"
_SUP = f"{_PFX}-sup"
_PEER = f"{_SUP}-codex"
_UNRELATED = f"{_PFX}-hands"
_FAILURES: list[str] = []


def _set_active(worker: str, task_id: str) -> None:
    """Peer ALIVE + actively working: current_task bound, NOT idle, FRESH heartbeat,
    no terminal outcome. The only state that should license ALLOW_STOP for a binding
    peer."""
    _R.set(_state_key(worker, "current_task"),
           json.dumps({"task_id": task_id, "started_at": time.time()}))
    _R.delete(_state_key(worker, "idle"))
    _R.set(_state_key(worker, "last_tool_activity"), str(time.time()))
    _R.delete(_state_key(worker, "last_outcome"))


def _set_done(worker: str, task_id: str) -> None:
    """Peer reported clean done -- REALISTIC: record_outcome(done) sets last_outcome
    AND the Stop hook clears current_task + sets idle. last_outcome is the queryable
    done-signal the engine reads (esp. for non-binding codex/grok that never bind)."""
    _R.delete(_state_key(worker, "current_task"))
    _R.set(_state_key(worker, "idle"), "1")
    _R.set(_state_key(worker, "last_outcome"),
           json.dumps({"outcome": "done", "details": f"DONE [{task_id}]"}))


def _set_stopped_bound(worker: str, task_id: str) -> None:
    """Peer STOPPED without a clean done (error/interrupt/forgot). In the heartbeat
    model a stopped peer's last_tool_activity STOPS refreshing -> goes STALE; no terminal
    outcome. -> NOT working -> BLOCK (subsumes PR#39 grok-V1). idle is irrelevant now."""
    _R.set(_state_key(worker, "current_task"),
           json.dumps({"task_id": task_id, "started_at": time.time()}))
    _R.set(_state_key(worker, "idle"), "1")
    _R.set(_state_key(worker, "last_tool_activity"),
           str(time.time() - (_PEER_HEARTBEAT_STALE_SEC + 60)))
    _R.delete(_state_key(worker, "last_outcome"))


def _set_crashed_bound(worker: str, task_id: str) -> None:
    """Peer HARD-KILLED (kill -9 / OOM / hook failure -- NO Stop hook ran):
    current_task PERSISTS, heartbeat STALE, no terminal outcome. -> NOT working ->
    BLOCK (the stale heartbeat catches it). gatekeeper outcome=unknown."""
    _R.set(_state_key(worker, "current_task"),
           json.dumps({"task_id": task_id, "started_at": time.time()}))
    _R.delete(_state_key(worker, "idle"))
    _R.set(_state_key(worker, "last_tool_activity"),
           str(time.time() - (_PEER_HEARTBEAT_STALE_SEC + 60)))
    _R.delete(_state_key(worker, "last_outcome"))


def _clear_peer(worker: str) -> None:
    for k in ("current_task", "idle", "last_activity", "last_tool_activity", "last_outcome"):
        _R.delete(_state_key(worker, k))


def _check(label: str, cond: bool, extra: str = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        _FAILURES.append(label)


def _decide():
    return _raw_stop_decision(_SUP, config=CFG)


def _cleanup(drv) -> None:
    _clear_peer(_PEER)
    _clear_peer(_UNRELATED)
    with drv.session(database=CFG.neo4j_db) as s:
        s.run("MATCH (n) WHERE n.id STARTS WITH $p DETACH DELETE n", p=_PFX)


def main() -> int:
    drv = get_neo4j_driver(CFG)
    _cleanup(drv)
    init_schema(config=CFG)

    def setp(pid, status="active"):
        with drv.session(database=CFG.neo4j_db) as s:
            s.run("MATCH (p:OrchProject {id:$i}) SET p.status=$st", i=pid, st=status)

    def mktask(pid, name, owner):
        create_task(phase_id=f"{pid}::ph", task_id=f"{pid}::{name}", description=f"{name} work",
                    owner=owner, wake_owner_if_ready=False, config=CFG)
        return f"{pid}::{name}"

    try:
        P = f"{_PFX}-A"
        create_project(project_id=P, name=P, supervisor=_SUP, config=CFG)
        create_phase(project_id=P, phase_id=f"{P}::ph", name="ph", config=CFG)
        setp(P)

        # 0. empty active project -> ALLOW_STOP (no false-positive keep-going)
        _check("empty project -> ALLOW_STOP", _decide().get("wake_type") == "ALLOW_STOP")

        # 1. pending peer task -> BLOCK to DISPATCH (default-on, NO flag set anywhere)
        peer = mktask(P, "peerwork", _PEER)
        d = _decide()
        _check("pending peer work BLOCKS (default-on, no flag)", d.get("block") is True and d.get("task_id") == peer, str(d))
        _check("pending -> dispatch_to the peer", d.get("dispatch_to") == _PEER, str(d.get("dispatch_to")))

        # 2. in-flight (in_progress) peer task -- the LIVENESS MATRIX. Only a peer
        #    that is alive AND working (bound + not idle + fresh heartbeat) licenses
        #    ALLOW_STOP; every not-working state must BLOCK (grok+gatekeeper PR#39).
        update_task_status(peer, "in_progress", owner=_PEER, config=CFG)
        #   2a. BUSY-LOOP FIX: peer ALIVE + actively working -> ALLOW_STOP.
        _set_active(_PEER, peer)
        _check("in-flight + peer ALIVE+working -> ALLOW_STOP (busy-loop fix)",
               _decide().get("wake_type") == "ALLOW_STOP", str(_decide()))
        set_project_user_stop_conditions(
            P,
            [{"label": "human-review", "active": True}],
            created_by=_SUP,
            config=CFG,
        )
        _check("in-flight + peer ALIVE+working + unmet stop condition -> ALLOW_STOP (issue #66)",
               _decide().get("wake_type") == "ALLOW_STOP", str(_decide()))
        set_project_user_stop_conditions(P, [], created_by=_SUP, config=CFG)
        #   2b. clean done (current_task CLEARED, idle set) -> BLOCK to GATE.
        _set_done(_PEER, peer)
        d = _decide()
        _check("in-flight + clean done BLOCKS to GATE (the 7h-stop hole)",
               d.get("block") is True and d.get("task_id") == peer, str(d))
        _check("in-flight gate -> gate_for the peer", d.get("gate_for") == _PEER, str(d.get("gate_for")))
        #   2c. STRAND case A (grok V1): stopped without clean done -- current_task
        #       persists but idle SET. Must BLOCK, not ALLOW.
        _set_stopped_bound(_PEER, peer)
        _check("in-flight + bound-but-IDLE (stopped, no done) BLOCKS (grok V1)",
               _decide().get("block") is True, str(_decide()))
        #   2d. STRAND case B (gatekeeper unknown): hard-kill, NO Stop hook --
        #       current_task persists, idle NOT set, heartbeat STALE. Must BLOCK.
        _set_crashed_bound(_PEER, peer)
        _check("in-flight + bound-but-STALE-heartbeat (hard-kill) BLOCKS (gatekeeper unknown)",
               _decide().get("block") is True, str(_decide()))

        # 3. dispatched peer task: same matrix in miniature.
        with drv.session(database=CFG.neo4j_db) as s:
            s.run("MATCH (t:OrchTask {id:$i}) SET t.status='dispatched'", i=peer)
        _set_done(_PEER, peer)
        _check("dispatched + not working BLOCKS to GATE", _decide().get("block") is True)
        _set_active(_PEER, peer)
        _check("dispatched + peer ALIVE+working -> ALLOW_STOP", _decide().get("wake_type") == "ALLOW_STOP")
        _clear_peer(_PEER)

        # 4. peer task terminal (completed) -> nothing in-flight -> ALLOW_STOP
        update_task_status(peer, "completed", owner=_PEER,
                           completion_evidence={"production_observation": "supkeep gate done"}, config=CFG)
        _check("completed peer work -> ALLOW_STOP (nothing left)", _decide().get("wake_type") == "ALLOW_STOP")

        # 5. isolation: a NON-peer (different base) in-flight task must NOT block
        stranger = mktask(P, "stranger", "someoneelse-codex")
        update_task_status(stranger, "in_progress", owner="someoneelse-codex", config=CFG)
        _check("non-peer in-flight work -> NOT blocked (isolation)", _decide().get("wake_type") == "ALLOW_STOP")

        # 5b. structured AWAIT peer work is a legitimate external wait, not a gate/stall.
        awaiting = mktask(P, "awaiting", _PEER)
        update_task_status(
            awaiting,
            "in_progress",
            owner=_PEER,
            blocked_on="AWAIT:external-signal:bg fetch harness",
            config=CFG,
        )
        _clear_peer(_PEER)
        d = _decide()
        _check("in-flight + structured AWAIT peer work -> ALLOW_STOP",
               d.get("wake_type") == "ALLOW_STOP" and d.get("task_id") is None, str(d))
        update_task_status(awaiting, "completed", owner=_PEER,
                           completion_evidence={"production_observation": "structured await honored"}, config=CFG)

        # 5c. a child/worker session with an explicit parent must not inherit the
        # supervisor's peer-work gate; it owns only its own ready/current bindings.
        scoped = mktask(P, "scoped", _PEER)
        update_task_status(scoped, "in_progress", owner=_PEER, config=CFG)
        _clear_peer(_PEER)
        _clear_peer(_UNRELATED)
        _R.set(_state_key(_UNRELATED, "parent"), _SUP)
        d = _raw_stop_decision(_UNRELATED, config=CFG)
        _check("child session does NOT gate parent peer work",
               d.get("wake_type") == "ALLOW_STOP" and d.get("task_id") is None, str(d))
        owned = mktask(P, "owned-by-child", _UNRELATED)
        d = _raw_stop_decision(_UNRELATED, config=CFG)
        _check("child session still surfaces its own ready work",
               d.get("block") is True and d.get("task_id") == owned, str(d))
        update_task_status(owned, "completed", owner=_UNRELATED,
                           completion_evidence={"production_observation": "child-owned work preserved"}, config=CFG)
        d = _decide()
        _check("supervisor still gates its own un-gated peer work",
               d.get("block") is True and d.get("task_id") == scoped and d.get("gate_for") == _PEER, str(d))
        update_task_status(scoped, "completed", owner=_PEER,
                           completion_evidence={"production_observation": "scoping gate preserved"}, config=CFG)

        # 6. stopped project -> peer work does not keep the supervisor up
        live = mktask(P, "live", _PEER)  # pending peer work
        setp(P, "stopped")
        _check("stopped project -> peer work does not block", _decide().get("wake_type") == "ALLOW_STOP")
        setp(P, "active")

        # 7. precedence: a pending peer task is surfaced to DISPATCH before an in-flight one is gated
        other = mktask(P, "other", _PEER)
        update_task_status(other, "in_progress", owner=_PEER, config=CFG)  # in-flight
        d = _decide()  # 'live' is pending -> dispatch must take precedence over gating 'other'
        _check("pending DISPATCH takes precedence over in-flight GATE",
               d.get("task_id") == live and d.get("dispatch_to") == _PEER, str(d))
    finally:
        _cleanup(drv)
    if _FAILURES:
        print(f"\nFAIL — {len(_FAILURES)}: {_FAILURES}")
        return 1
    print("\nPASS — supervisor keep-going is default-on, covers pending(dispatch)+in-flight(gate), isolated, no off-switch.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
