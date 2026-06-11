"""Ship-gate e2e — update_task_status must NOT wipe owner on a status-only update.

Bug: owner defaulted to '' and SET t.owner = $owner unconditionally, so any status
update that didn't re-pass owner orphaned the task from owner-scoped readiness. Fix:
empty owner arg preserves the existing owner; a non-empty owner sets it.

Env: ORCH_NEO4J_URI, ORCH_NEO4J_DB, ORCH_REDIS_HOST/PORT, ORCH_DASHBOARD_URL.
"""
from __future__ import annotations

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.orch_schema import (  # noqa: E402
    create_project, create_phase, create_task, update_task_status,
    get_session_next_ready, init_schema, get_neo4j_driver,
)
from lib.config import OrchConfig  # noqa: E402

CFG = OrchConfig()
_PFX = f"ownerpres-ci-{uuid.uuid4().hex[:8]}"
_FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: str = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        _FAILURES.append(label)


def main() -> int:
    init_schema(config=CFG)
    drv = get_neo4j_driver(CFG)

    def owner_of(tid: str):
        with drv.session(database=CFG.neo4j_db) as s:
            r = s.run("MATCH (t:OrchTask {id:$i}) RETURN t.owner AS o", i=tid).single()
            return r["o"] if r else None

    with drv.session(database=CFG.neo4j_db) as s:
        s.run("MATCH (n) WHERE n.id STARTS WITH $p DETACH DELETE n", p=_PFX)
    try:
        create_project(project_id=_PFX, name=_PFX, supervisor=f"{_PFX}-alice", config=CFG)
        create_phase(project_id=_PFX, phase_id=f"{_PFX}::ph", name="ph", config=CFG)
        with drv.session(database=CFG.neo4j_db) as s:
            s.run("MATCH (p:OrchProject {id:$i}) SET p.status='active'", i=_PFX)
        T = f"{_PFX}::t"
        create_task(phase_id=f"{_PFX}::ph", task_id=T, description="x",
                    owner=f"{_PFX}-alice", wake_owner_if_ready=False, config=CFG)
        _check("created with owner alice", owner_of(T) == f"{_PFX}-alice", owner_of(T))

        # status-only update (NO owner) must PRESERVE the owner
        update_task_status(T, "in_progress", config=CFG)
        _check("status->in_progress without owner PRESERVES owner (the bug)", owner_of(T) == f"{_PFX}-alice", owner_of(T))

        # back to pending without owner -> still alice -> still surfaces to alice (readiness)
        update_task_status(T, "pending", config=CFG)
        _check("status->pending without owner PRESERVES owner", owner_of(T) == f"{_PFX}-alice", owner_of(T))
        nr = get_session_next_ready(f"{_PFX}-alice", project_id=_PFX, config=CFG)
        _check("task still surfaces to its owner after toggle (not orphaned)", bool(nr) and nr["task_id"] == T, str(nr))

        # whitespace-only owner is treated as empty -> PRESERVE (grok V4)
        update_task_status(T, "in_progress", owner="   ", config=CFG)
        _check("whitespace-only owner PRESERVES owner (not wiped to spaces)", owner_of(T) == f"{_PFX}-alice", owner_of(T))
        update_task_status(T, "pending", config=CFG)

        # explicit owner DOES reassign
        update_task_status(T, "pending", owner=f"{_PFX}-bob", config=CFG)
        _check("explicit owner reassigns to bob", owner_of(T) == f"{_PFX}-bob", owner_of(T))

        # result-given branch (branch 2): status update WITH result, no owner -> preserve (grok V1)
        update_task_status(T, "in_progress", result="some result text", config=CFG)
        _check("result-given branch preserves owner without owner arg", owner_of(T) == f"{_PFX}-bob", owner_of(T))

        # completed without owner preserves bob
        update_task_status(T, "completed",
                           completion_evidence={"production_observation": "owner preserve acceptance"}, config=CFG)
        _check("status->completed without owner PRESERVES owner", owner_of(T) == f"{_PFX}-bob", owner_of(T))
    finally:
        with drv.session(database=CFG.neo4j_db) as s:
            s.run("MATCH (n) WHERE n.id STARTS WITH $p DETACH DELETE n", p=_PFX)
    if _FAILURES:
        print(f"\nFAIL — {len(_FAILURES)}: {_FAILURES}")
        return 1
    print("\nPASS — owner preserved on status-only updates; explicit owner still reassigns.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
