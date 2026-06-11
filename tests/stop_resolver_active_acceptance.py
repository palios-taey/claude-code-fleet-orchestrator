"""Ship-gate e2e — a blocked_on resolver must be ACTIVELY worked to license a stop.

The recurring false-stop: a session creates a tracking task to satisfy blocked_on, leaves it
PENDING, and the engine reads "real non-terminal task -> live resolver -> you may stop." A pending
task nobody is working is not a live wait. _blocked_on_has_live_resolver now requires in_progress.

Env: ORCH_NEO4J_URI (default bolt://localhost:7687), ORCH_NEO4J_DB (default neo4j).
"""
from __future__ import annotations

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet_orchestrator.orch_schema import (  # noqa: E402
    create_project, create_phase, create_task, update_task_status,
    _blocked_on_has_live_resolver, init_schema, get_neo4j_driver,
)
from fleet_orchestrator.config import OrchConfig  # noqa: E402

CFG = OrchConfig()
_PFX = f"stopres-ci-{uuid.uuid4().hex[:8]}"
_FAILURES: list[str] = []


def _check(label: str, cond: bool) -> None:
    print(("  PASS " if cond else "  FAIL ") + label)
    if not cond:
        _FAILURES.append(label)


def main() -> int:
    init_schema(config=CFG)
    drv = get_neo4j_driver(CFG)
    with drv.session(database=CFG.neo4j_db) as s:
        s.run("MATCH (n) WHERE n.id STARTS WITH $p DETACH DELETE n", p=_PFX)
    try:
        create_project(project_id=_PFX, name=_PFX, config=CFG)
        create_phase(project_id=_PFX, phase_id=f"{_PFX}::p", name="p", config=CFG)
        create_task(phase_id=f"{_PFX}::p", task_id=f"{_PFX}::pending", description="x", wake_owner_if_ready=False, config=CFG)
        create_task(phase_id=f"{_PFX}::p", task_id=f"{_PFX}::active", description="y", wake_owner_if_ready=False, config=CFG)
        update_task_status(f"{_PFX}::active", "in_progress", config=CFG)
        # THE invariant: a PENDING resolver is NOT live -> no false stop
        _check("pending resolver is NOT a live wait (no false stop)", _blocked_on_has_live_resolver(f"{_PFX}::pending", config=CFG) is False)
        _check("in_progress resolver IS a live wait", _blocked_on_has_live_resolver(f"{_PFX}::active", config=CFG) is True)
        update_task_status(f"{_PFX}::active", "completed", completion_evidence={"production_observation": "stop-resolver acceptance"}, config=CFG)
        _check("completed resolver is NOT live", _blocked_on_has_live_resolver(f"{_PFX}::active", config=CFG) is False)
        _check("non-existent resolver is NOT live", _blocked_on_has_live_resolver("task-does-not-exist-9", config=CFG) is False)
        _check("empty/None blocked_on is NOT live", _blocked_on_has_live_resolver("", config=CFG) is False)

        # F2: 'ready' (not-live) + 'dispatched' (live) membership. These are not canonical task
        # statuses (the API rejects them), so set them directly to exercise _LIVE_RESOLVER_STATUSES.
        create_task(phase_id=f"{_PFX}::p", task_id=f"{_PFX}::rdy", description="r", wake_owner_if_ready=False, config=CFG)
        create_task(phase_id=f"{_PFX}::p", task_id=f"{_PFX}::disp", description="d", wake_owner_if_ready=False, config=CFG)
        with drv.session(database=CFG.neo4j_db) as s:
            s.run("MATCH (t:OrchTask {id:$i}) SET t.status='ready'", i=f"{_PFX}::rdy")
            s.run("MATCH (t:OrchTask {id:$i}) SET t.status='dispatched'", i=f"{_PFX}::disp")
        _check("ready resolver is NOT live", _blocked_on_has_live_resolver(f"{_PFX}::rdy", config=CFG) is False)
        _check("dispatched resolver IS live", _blocked_on_has_live_resolver(f"{_PFX}::disp", config=CFG) is True)

        # F3 transitive: A->B(in_progress)->C(pending) -> NOT live (chain bottoms at a pending node)
        for n in ("B", "C", "D", "X", "Y"):
            create_task(phase_id=f"{_PFX}::p", task_id=f"{_PFX}::{n}", description=n, wake_owner_if_ready=False, config=CFG)
        update_task_status(f"{_PFX}::B", "in_progress", blocked_on=f"{_PFX}::C", config=CFG)
        _check("transitive in_progress B blocked_on pending C -> NOT live (keep going)", _blocked_on_has_live_resolver(f"{_PFX}::B", config=CFG) is False)
        # multi-hop chain that DOES terminate in an active, not-waiting node -> live
        update_task_status(f"{_PFX}::D", "in_progress", config=CFG)
        update_task_status(f"{_PFX}::B", "in_progress", blocked_on=f"{_PFX}::D", config=CFG)
        _check("multi-hop chain ending in active resolver -> live", _blocked_on_has_live_resolver(f"{_PFX}::B", config=CFG) is True)
        # cycle X<->Y -> NOT live (cycle guard exercises the chain walk)
        update_task_status(f"{_PFX}::Y", "in_progress", blocked_on=f"{_PFX}::X", config=CFG)
        update_task_status(f"{_PFX}::X", "in_progress", blocked_on=f"{_PFX}::Y", config=CFG)
        _check("cycle X<->Y -> NOT live (cycle guard)", _blocked_on_has_live_resolver(f"{_PFX}::X", config=CFG) is False)
    finally:
        with drv.session(database=CFG.neo4j_db) as s:
            s.run("MATCH (n) WHERE n.id STARTS WITH $p DETACH DELETE n", p=_PFX)
    if _FAILURES:
        print(f"\nFAIL — {len(_FAILURES)}: {_FAILURES}")
        return 1
    print("\nPASS — only an actively-worked resolver licenses a stop")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
