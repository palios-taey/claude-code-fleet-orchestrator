"""Acceptance: single-project API keeps project identity at top level and nested summary."""
from __future__ import annotations

import os
import sys
import uuid

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet_orchestrator.config import OrchConfig  # noqa: E402
from fleet_orchestrator.orch_schema import create_phase, create_project, create_task, get_neo4j_driver, get_task, init_schema  # noqa: E402
from fleet_orchestrator.tasks_api import app  # noqa: E402

CFG = OrchConfig()
PFX = f"project-detail-ci-{uuid.uuid4().hex[:8]}"
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def main() -> int:
    init_schema(config=CFG)
    driver = get_neo4j_driver(CFG)
    with driver.session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=PFX)
    try:
        create_project(project_id=PFX, name="Project Detail API", supervisor=f"{PFX}-sup", config=CFG)
        create_phase(project_id=PFX, phase_id=f"{PFX}::phase", name="Phase", config=CFG)
        create_task(
            phase_id=f"{PFX}::phase",
            task_id=f"{PFX}::task",
            description="detail task",
            owner=f"{PFX}-worker",
            delivery_gate=True,
            wake_owner_if_ready=False,
            config=CFG,
        )
        with driver.session(database=CFG.neo4j_db) as session:
            session.run("MATCH (t:OrchTask {id: $task_id}) SET t.recurring = true", task_id=f"{PFX}::task")

        response = TestClient(app).get(f"/api/projects/{PFX}")
        body = response.json()
        _check("detail endpoint returns 200", response.status_code == 200, body)
        _check("top-level id mirrors list endpoint row", body.get("id") == PFX, body)
        _check("top-level name mirrors list endpoint row", body.get("name") == "Project Detail API", body)
        _check("top-level counts are present", body.get("phase_count") == 1 and body.get("task_total") == 1, body)
        _check("nested project id remains populated", (body.get("project") or {}).get("id") == PFX, body)
        _check("nested project name remains populated", (body.get("project") or {}).get("name") == "Project Detail API", body)
        _check("phases/tasks still returned", body.get("phases") and body["phases"][0].get("tasks"), body)
        detail_task = body["phases"][0]["tasks"][0] if body.get("phases") and body["phases"][0].get("tasks") else {}
        task_api = get_task(f"{PFX}::task", config=CFG) or {}
        _check(
            "detail task recurring matches task API",
            detail_task.get("recurring") is True and detail_task.get("recurring") == task_api.get("recurring"),
            {"detail": detail_task, "task_api": task_api},
        )
        _check(
            "detail task delivery_gate matches task API",
            detail_task.get("delivery_gate") is True and detail_task.get("delivery_gate") == task_api.get("delivery_gate"),
            {"detail": detail_task, "task_api": task_api},
        )
    finally:
        with driver.session(database=CFG.neo4j_db) as session:
            session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=PFX)
    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - project detail API exposes project identity at top level and nested summary.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
