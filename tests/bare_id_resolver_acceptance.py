"""Ship-gate e2e — bare task-id resolution (the stop-engine "phantom" self-clear fix).

Post-v1.7.0 tasks are namespaced '<project>::<bare>'. A session referencing the BARE id 404'd and
could not self-clear, leaving a phantom in the stop-engine. resolve_task_id (used at the GET + PATCH
entry points) resolves a unique bare id to its canonical node; ambiguous/non-existent are never guessed.

Env: ORCH_NEO4J_URI (default bolt://localhost:7687), ORCH_NEO4J_DB (default neo4j).
"""
from __future__ import annotations

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi.testclient import TestClient  # noqa: E402
from fleet_orchestrator.tasks_api import app  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    create_project, create_phase, create_task, resolve_task_id, init_schema, get_neo4j_driver,
)
from fleet_orchestrator.config import OrchConfig  # noqa: E402

CFG = OrchConfig()
_PFX = f"resolv-ci-{uuid.uuid4().hex[:8]}"
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
    client = TestClient(app)
    try:
        create_project(project_id=_PFX, name=_PFX, config=CFG)
        create_phase(project_id=_PFX, phase_id=f"{_PFX}::p", name="p", config=CFG)
        bare = f"task-{uuid.uuid4().hex[:8]}"
        create_task(phase_id=f"{_PFX}::p", task_id=f"{_PFX}::{bare}", description="x",
                    wake_owner_if_ready=False, config=CFG)
        _check("resolve_task_id(bare) -> canonical namespaced", resolve_task_id(bare, config=CFG) == f"{_PFX}::{bare}")
        _check("GET /api/tasks/<bare> -> 200 (self-clear enabled)", client.get(f"/api/tasks/{bare}").status_code == 200)
        _check("PATCH /api/task/<bare> in_progress -> 200", client.patch(f"/api/task/{bare}", json={"status": "in_progress", "from": "t"}).status_code == 200)
        _check("already-namespaced id still works", client.get(f"/api/tasks/{_PFX}::{bare}").status_code == 200)
        _check("non-existent bare -> honest 404", client.get("/api/tasks/task-nope12345").status_code == 404)
        # ambiguous: same bare under two projects -> resolver must NOT guess
        p2 = f"{_PFX}b"
        create_project(project_id=p2, name=p2, config=CFG)
        create_phase(project_id=p2, phase_id=f"{p2}::p", name="p", config=CFG)
        create_task(phase_id=f"{p2}::p", task_id=f"{p2}::{bare}", description="y", wake_owner_if_ready=False, config=CFG)
        _check("ambiguous bare -> unchanged (no guess) -> 404", client.get(f"/api/tasks/{bare}").status_code == 404)
    finally:
        with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as s:
            s.run("MATCH (n) WHERE n.id STARTS WITH $p DETACH DELETE n", p=_PFX)
            s.run("MATCH (n) WHERE n.id STARTS WITH $p DETACH DELETE n", p=f"{_PFX}b")
    if _FAILURES:
        print(f"\nFAIL — {len(_FAILURES)}: {_FAILURES}")
        return 1
    print("\nPASS — bare-id resolver")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
