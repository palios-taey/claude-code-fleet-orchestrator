"""Acceptance test: reset_project must exclude AWAIT: and human-review task holds."""
from __future__ import annotations

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet_orchestrator.orch_schema import (
    create_project, create_phase, create_task, init_schema, get_neo4j_driver,
    reset_project, update_task_status, get_task,
)
from fleet_orchestrator.config import OrchConfig

CFG = OrchConfig()
_PFX = f"reset-ci-{uuid.uuid4().hex[:8]}"
_FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: str = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        _FAILURES.append(label)


def _cleanup() -> None:
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as s:
        s.run("MATCH (n) WHERE n.id STARTS WITH $p DETACH DELETE n", p=_PFX)


def main() -> int:
    init_schema(config=CFG)
    _cleanup()
    
    try:
        # Create project and phase
        create_project(project_id=_PFX, name=_PFX, config=CFG)
        phase_id = f"{_PFX}::phase"
        create_phase(phase_id=phase_id, project_id=_PFX, name="Phase 1", order=1, config=CFG)
        
        # 1. Standard task 1: completed
        t1_id = f"{_PFX}::t1"
        create_task(task_id=t1_id, phase_id=phase_id, description="Task 1", config=CFG)
        update_task_status(t1_id, "in_progress", owner="bob", config=CFG)
        update_task_status(t1_id, "completed", owner="bob", 
                           completion_evidence={"production_observation": "verified live"}, config=CFG)
        
        # 2. AWAIT-held task 2: starts with AWAIT:
        t2_id = f"{_PFX}::t2"
        create_task(task_id=t2_id, phase_id=phase_id, description="Task 2", config=CFG)
        # Update blocked_on to start with AWAIT:
        update_task_status(t2_id, "in_progress", owner="bob", config=CFG)
        update_task_status(t2_id, "in_progress", owner="bob", blocked_on="AWAIT:external-signal:wait", config=CFG)
        
        # 3. Human-review task 3: type = "human-review", non-default state
        t3_id = f"{_PFX}::t3"
        create_task(task_id=t3_id, phase_id=phase_id, description="Task 3", task_type="human-review", config=CFG)
        update_task_status(t3_id, "in_progress", owner="reviewer", config=CFG)
        
        # Verify initial states
        t1 = get_task(t1_id, config=CFG)
        t2 = get_task(t2_id, config=CFG)
        t3 = get_task(t3_id, config=CFG)
        
        _check("t1 is completed", t1.get("status") == "completed")
        _check("t2 has AWAIT block", t2.get("blocked_on") == "AWAIT:external-signal:wait")
        _check("t3 is human-review type", t3.get("task_type") == "human-review")
        _check("t3 human-review pre-state is non-default", t3.get("status") == "in_progress")
        
        # Run reset_project
        reset_project(_PFX, reset_by="tester", config=CFG)
        
        # Load post-reset states
        t1_post = get_task(t1_id, config=CFG)
        t2_post = get_task(t2_id, config=CFG)
        t3_post = get_task(t3_id, config=CFG)
        
        _check("t1 reset back to pending", t1_post.get("status") == "pending")
        _check("t1 blocked_on is cleared", t1_post.get("blocked_on") is None)
        
        _check("t2 AWAIT blocked_on remains intact", t2_post.get("blocked_on") == "AWAIT:external-signal:wait")
        _check("t2 status is preserved", t2_post.get("status") == t2.get("status"))
        
        _check("t3 human-review in_progress status is preserved", t3_post.get("status") == "in_progress")
        _check("t3 human-review blocked_on remains untouched", t3_post.get("blocked_on") == t3.get("blocked_on"))
        
    finally:
        _cleanup()
        
    if _FAILURES:
        print(f"FAIL: {len(_FAILURES)} assertion(s) failed: {_FAILURES}")
        return 1
        
    print("PASS: reset_project holds exclusions as expected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
