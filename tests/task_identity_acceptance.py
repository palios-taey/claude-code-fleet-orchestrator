"""Ship-gate regression test — the v1.7.0 project-scoped task-identity invariant.

Runs against an ephemeral Neo4j (CI service container or a local instance). Does NOT need the
fleet-notify sibling: every guard raises BEFORE the notify path, and happy-paths pass
wake_owner_if_ready=False. This is the executable form of the invariant the Family + Gatekeeper
verified at v1.7.0 — it exists so a FUTURE change that re-opens cross-project clobber/fusion fails CI.

Scope (honest): this is an INTEGRATION e2e of the identity-write guards, not a browser UI e2e.
Env: ORCH_NEO4J_URI (default bolt://localhost:7687), ORCH_NEO4J_DB (default neo4j).
"""
from __future__ import annotations

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.orch_schema import (  # noqa: E402
    create_project, create_phase, create_task, assign_task_to_phase,
    TaskIdCollisionError, init_schema, get_neo4j_driver,
)
from lib.config import OrchConfig  # noqa: E402
from lib.plan_loader import scope_declared_id, PlanIdError  # noqa: E402

CFG = OrchConfig()
_PFX = f"sgci-{uuid.uuid4().hex[:8]}"   # unique per run so a shared DB stays clean
_FAILURES: list[str] = []


def _check(name: str, cond: bool) -> None:
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond:
        _FAILURES.append(name)


def _raises(fn) -> bool:
    try:
        fn(); return False
    except TaskIdCollisionError:
        return True


def _cleanup() -> None:
    drv = get_neo4j_driver(CFG)
    with drv.session(database=CFG.neo4j_db) as s:
        s.run("MATCH (n) WHERE (n:OrchProject OR n:OrchPhase OR n:OrchTask) "
              "AND n.id STARTS WITH $p DETACH DELETE n", p=_PFX)
        s.run("MATCH (t:OrchTask) WHERE t.id STARTS WITH $p DETACH DELETE t", p=_PFX)


def main() -> int:
    init_schema(config=CFG)  # UNIQUE constraints orch_task_id / orch_phase_id back the guards
    _cleanup()
    try:
        # --- pure: declared "::" injection + charset rejected; legit scopes ---
        _check("scope_declared_id rejects declared '::'", _pid_raises(lambda: scope_declared_id("victim", "other::audit")))
        _check("scope_declared_id rejects bad charset", _pid_raises(lambda: scope_declared_id("victim", "bad id!")))
        _check("scope_declared_id scopes a plain id", scope_declared_id("proj", "audit") == "proj::audit")

        # --- two projects, both phase "p"; both tasks "audit" -> distinct, no fusion ---
        a, b = f"{_PFX}-a", f"{_PFX}-b"
        for p in (a, b):
            create_project(project_id=p, name=p, config=CFG)
            create_phase(project_id=p, phase_id=f"{p}::p", name="p", config=CFG)
            create_task(phase_id=f"{p}::p", task_id=f"{p}::audit", description="x",
                        wake_owner_if_ready=False, config=CFG)
        drv = get_neo4j_driver(CFG)
        with drv.session(database=CFG.neo4j_db) as s:
            owners = lambda tid: [r["o"] for r in s.run(
                "MATCH (t:OrchTask {id:$id})<-[:HAS_TASK]-(:OrchPhase)<-[:HAS_PHASE]-(o) RETURN o.id AS o", id=tid)]
            _check("projA::audit owned only by A", owners(f"{a}::audit") == [a])
            _check("projB::audit owned only by B (no fusion)", owners(f"{b}::audit") == [b])

        # --- guards: create_task / create_phase / assign refuse foreign / orphan / fused ---
        with drv.session(database=CFG.neo4j_db) as s:
            s.run("MERGE (t:OrchTask {id:$id})", id=f"{_PFX}-orphan::audit")          # orphan task (no phase)
            s.run("MERGE (ph:OrchPhase {id:$id})", id=f"{_PFX}-orphanphase")          # orphan phase (no project)
            s.run("MATCH (p1:OrchProject {id:$a}),(p2:OrchProject {id:$b}) "
                  "MERGE (f:OrchPhase {id:$f}) MERGE (p1)-[:HAS_PHASE]->(f) MERGE (p2)-[:HAS_PHASE]->(f)",
                  a=a, b=b, f=f"{_PFX}-fusedphase")                                    # fused phase (2 owners)
        _check("create_task refuses foreign task id",
               _raises(lambda: create_task(phase_id=f"{a}::p", task_id=f"{b}::audit", description="x", wake_owner_if_ready=False, config=CFG)))
        _check("create_task refuses orphan-phase (fail-closed)",
               _raises(lambda: create_task(phase_id=f"{_PFX}-orphanphase", task_id=f"{a}::z", description="x", wake_owner_if_ready=False, config=CFG)))
        _check("create_task refuses fused-phase (no 500)",
               _raises(lambda: create_task(phase_id=f"{_PFX}-fusedphase", task_id=f"{a}::z2", description="x", wake_owner_if_ready=False, config=CFG)))
        _check("create_phase refuses foreign phase id",
               _raises(lambda: create_phase(project_id=a, phase_id=f"{b}::p", name="x", config=CFG)))
        _check("assign refuses foreign task re-parent",
               _raises(lambda: assign_task_to_phase(f"{b}::audit", f"{a}::p", config=CFG)))
        _check("assign refuses orphan task adopt",
               _raises(lambda: assign_task_to_phase(f"{_PFX}-orphan::audit", f"{a}::p", config=CFG)))

        # --- happy: same-project re-parent allowed ---
        ok = True
        try:
            assign_task_to_phase(f"{a}::audit", f"{a}::p", config=CFG)
        except Exception:
            ok = False
        _check("assign allows same-project re-parent", ok)
    finally:
        _cleanup()

    print(f"\nship-gate identity invariant: {'PASS' if not _FAILURES else 'FAIL ' + str(_FAILURES)}")
    return 1 if _FAILURES else 0


def _pid_raises(fn) -> bool:
    try:
        fn(); return False
    except PlanIdError:
        return True


if __name__ == "__main__":
    raise SystemExit(main())
