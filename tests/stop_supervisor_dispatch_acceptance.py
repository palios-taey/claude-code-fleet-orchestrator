"""Ship-gate e2e — supervisor must not stop while a supervised active project has a
pending-ready task owned by an autonomous PEER (the second stop-engine hole).

_raw_stop_decision's own-ready loop only surfaces work owned by the supervisor itself;
peer-owned ready work (e.g. conductor-codex) did not block the supervisor's stop, so it
dead-locked until a human re-dispatched. The new branch (flag-default-OFF) blocks the
supervisor with a DISPATCH reason. Verified here vs LIVE Neo4j.

Env: ORCH_NEO4J_URI, ORCH_NEO4J_DB, ORCH_REDIS_HOST/PORT, ORCH_DASHBOARD_URL.
"""
from __future__ import annotations

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.orch_schema import (  # noqa: E402
    create_project, create_phase, create_task, update_task_status, add_dependency,
    _raw_stop_decision, init_schema, get_neo4j_driver,
)
from lib.config import OrchConfig  # noqa: E402

CFG = OrchConfig()
_PFX = f"supdisp-ci-{uuid.uuid4().hex[:8]}"
_SUP = f"{_PFX}-sup"
_PEER = f"{_SUP}-codex"
_FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: str = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        _FAILURES.append(label)


def _flag(on: bool) -> None:
    if on:
        os.environ["CF_SUPERVISOR_DISPATCH"] = "1"
        os.environ["CF_SUPERVISOR_DISPATCH_SESSIONS"] = _SUP
    else:
        os.environ.pop("CF_SUPERVISOR_DISPATCH", None)
        os.environ.pop("CF_SUPERVISOR_DISPATCH_SESSIONS", None)


def _blocks_on(task_id: str) -> bool:
    d = _raw_stop_decision(_SUP, config=CFG)
    return d.get("block") is True and d.get("task_id") == task_id


def _allows() -> bool:
    return _raw_stop_decision(_SUP, config=CFG).get("wake_type") == "ALLOW_STOP"


def main() -> int:
    init_schema(config=CFG)
    drv = get_neo4j_driver(CFG)

    def mkproj(pid: str, status: str = "active"):
        create_project(project_id=pid, name=pid, supervisor=_SUP, config=CFG)
        create_phase(project_id=pid, phase_id=f"{pid}::ph", name="ph", config=CFG)
        with drv.session(database=CFG.neo4j_db) as s:
            s.run("MATCH (p:OrchProject {id:$i}) SET p.status=$st", i=pid, st=status)

    def mktask(pid: str, name: str, owner: str):
        create_task(phase_id=f"{pid}::ph", task_id=f"{pid}::{name}", description=f"{name} work",
                    owner=owner, wake_owner_if_ready=False, config=CFG)
        return f"{pid}::{name}"

    with drv.session(database=CFG.neo4j_db) as s:
        s.run("MATCH (n) WHERE n.id STARTS WITH $p DETACH DELETE n", p=_PFX)
    try:
        P = f"{_PFX}-A"
        mkproj(P)
        peer_task = mktask(P, "peerwork", _PEER)  # pending, owned by SUP-codex

        # 1. flag OFF -> ALLOW_STOP (default-off safety; no behavior change)
        _flag(False)
        _check("flag OFF: supervisor may stop despite pending peer work (default-off)", _allows())

        # 2. flag ON -> blocks on the pending peer task, with dispatch_to
        _flag(True)
        d = _raw_stop_decision(_SUP, config=CFG)
        _check("flag ON: blocks on the pending peer task", d.get("block") is True and d.get("task_id") == peer_task, str(d))
        _check("flag ON: names the peer to dispatch to", d.get("dispatch_to") == _PEER, str(d.get("dispatch_to")))

        # 3. peer task in_progress (peer already on it) -> NOT blocked
        update_task_status(peer_task, "in_progress", owner=_PEER, config=CFG)
        _check("peer task in_progress -> supervisor may stop (peer is on it)", _allows())
        update_task_status(peer_task, "pending", owner=_PEER, config=CFG)

        # 4. peer task has an incomplete DEPENDS_ON -> NOT dispatchable -> NOT blocked
        gate = mktask(P, "gate", _PEER)
        update_task_status(gate, "in_progress", owner=_PEER, config=CFG)
        add_dependency(peer_task, gate, config=CFG)
        _check("peer task dep-gated -> not blocked", _allows())
        update_task_status(gate, "completed",
                           completion_evidence={"production_observation": "supdisp acceptance gate"}, config=CFG)
        _check("peer task dep satisfied -> blocked again", _blocks_on(peer_task))

        # 5. project not active (stopped) -> NOT blocked
        with drv.session(database=CFG.neo4j_db) as s:
            s.run("MATCH (p:OrchProject {id:$i}) SET p.status='stopped'", i=P)
        _check("stopped project -> peer work does not block", _allows())
        with drv.session(database=CFG.neo4j_db) as s:
            s.run("MATCH (p:OrchProject {id:$i}) SET p.status='active'", i=P)

        # 6. task owned by a NON-peer (different base) -> isolation -> NOT blocked
        update_task_status(peer_task, "in_progress", owner=_PEER, config=CFG)  # park the real peer task
        stranger = mktask(P, "stranger", "someoneelse-codex")
        _check("non-peer-owned ready task -> not blocked (isolation)", _allows())
        update_task_status(stranger, "completed",
                           completion_evidence={"production_observation": "supdisp stranger"}, config=CFG)
        update_task_status(peer_task, "pending", owner=_PEER, config=CFG)

        # 7. supervisor's OWN ready task takes precedence via the existing own-ready path
        own = mktask(P, "ownwork", _SUP)
        d = _raw_stop_decision(_SUP, config=CFG)
        _check("supervisor own ready work still blocks (no regression, own precedence)",
               d.get("block") is True and d.get("task_id") == own, str(d))
    finally:
        _flag(False)
        with drv.session(database=CFG.neo4j_db) as s:
            s.run("MATCH (n) WHERE n.id STARTS WITH $p DETACH DELETE n", p=_PFX)
    if _FAILURES:
        print(f"\nFAIL — {len(_FAILURES)}: {_FAILURES}")
        return 1
    print("\nPASS — supervisor keeps up to dispatch peer-owned ready work (flag-gated, isolated, no regression).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
