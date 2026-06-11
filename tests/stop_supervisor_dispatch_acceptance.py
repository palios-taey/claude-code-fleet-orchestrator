"""Ship-gate e2e — supervisor keep-going is DEFAULT-ON (no flag) and covers BOTH
pending peer work (dispatch) AND in-flight peer work (stay up to gate).

The stop-engine's whole purpose is that a supervisor does not stop while there is fleet
work. The own-ready loop only surfaces the supervisor's OWN tasks; a supervised active
project with peer-owned work that is pending (must dispatch) OR in_progress/dispatched
(must stay up to gate when the peer reports done) must BLOCK the stop. There is no flag
to turn this off -- the in-flight gap is what stranded a supervisor for hours.

Env: ORCH_NEO4J_URI, ORCH_NEO4J_DB, ORCH_REDIS_HOST/PORT, ORCH_DASHBOARD_URL.
"""
from __future__ import annotations

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.orch_schema import (  # noqa: E402
    create_project, create_phase, create_task, update_task_status,
    _raw_stop_decision, init_schema, get_neo4j_driver,
)
from lib.config import OrchConfig  # noqa: E402

CFG = OrchConfig()
_PFX = f"supkeep-ci-{uuid.uuid4().hex[:8]}"
_SUP = f"{_PFX}-sup"
_PEER = f"{_SUP}-codex"
_FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: str = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        _FAILURES.append(label)


def _decide():
    return _raw_stop_decision(_SUP, config=CFG)


def main() -> int:
    init_schema(config=CFG)
    drv = get_neo4j_driver(CFG)

    def setp(pid, status="active"):
        with drv.session(database=CFG.neo4j_db) as s:
            s.run("MATCH (p:OrchProject {id:$i}) SET p.status=$st", i=pid, st=status)

    def mktask(pid, name, owner):
        create_task(phase_id=f"{pid}::ph", task_id=f"{pid}::{name}", description=f"{name} work",
                    owner=owner, wake_owner_if_ready=False, config=CFG)
        return f"{pid}::{name}"

    with drv.session(database=CFG.neo4j_db) as s:
        s.run("MATCH (n) WHERE n.id STARTS WITH $p DETACH DELETE n", p=_PFX)
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

        # 2. THE 7h-stop fix: in-flight (in_progress) peer task -> BLOCK to GATE (stay up)
        update_task_status(peer, "in_progress", owner=_PEER, config=CFG)
        d = _decide()
        _check("in_progress peer work BLOCKS to GATE (the 7h-stop hole)", d.get("block") is True and d.get("task_id") == peer, str(d))
        _check("in-flight -> gate_for the peer", d.get("gate_for") == _PEER, str(d.get("gate_for")))

        # 3. dispatched peer task -> also BLOCK to GATE
        with drv.session(database=CFG.neo4j_db) as s:
            s.run("MATCH (t:OrchTask {id:$i}) SET t.status='dispatched'", i=peer)
        _check("dispatched peer work BLOCKS to GATE", _decide().get("block") is True)

        # 4. peer task terminal (completed) -> nothing in-flight -> ALLOW_STOP
        update_task_status(peer, "completed", owner=_PEER,
                           completion_evidence={"production_observation": "supkeep gate done"}, config=CFG)
        _check("completed peer work -> ALLOW_STOP (nothing left)", _decide().get("wake_type") == "ALLOW_STOP")

        # 5. isolation: a NON-peer (different base) in-flight task must NOT block
        stranger = mktask(P, "stranger", "someoneelse-codex")
        update_task_status(stranger, "in_progress", owner="someoneelse-codex", config=CFG)
        _check("non-peer in-flight work -> NOT blocked (isolation)", _decide().get("wake_type") == "ALLOW_STOP")

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
        with drv.session(database=CFG.neo4j_db) as s:
            s.run("MATCH (n) WHERE n.id STARTS WITH $p DETACH DELETE n", p=_PFX)
    if _FAILURES:
        print(f"\nFAIL — {len(_FAILURES)}: {_FAILURES}")
        return 1
    print("\nPASS — supervisor keep-going is default-on, covers pending(dispatch)+in-flight(gate), isolated, no off-switch.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
