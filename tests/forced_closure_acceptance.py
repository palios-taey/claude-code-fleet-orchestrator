"""Acceptance: forced project closure is persisted and reason-gated."""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

BASE_NAMESPACE = os.environ.get("ORCH_TEST_NAMESPACE") or "forced-closure-ci"
PREFIX = f"{BASE_NAMESPACE}-forced-closure-{uuid.uuid4().hex[:8]}"
os.environ["NOTIFY_KEY_PREFIX"] = PREFIX
from fleet_orchestrator.test_isolation import assert_acceptance_redis_isolated  # noqa: E402

assert_acceptance_redis_isolated()

from fleet_orchestrator.config import OrchConfig  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    create_phase,
    create_project,
    create_task,
    get_neo4j_driver,
    init_schema,
    update_task_status,
)
from fleet_orchestrator.tasks_api import app  # noqa: E402

CFG = OrchConfig()
CLIENT = TestClient(app)
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _cleanup() -> None:
    driver = get_neo4j_driver(CFG)
    with driver.session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE coalesce(n.id, '') STARTS WITH $prefix DETACH DELETE n", prefix=PREFIX)


def _seed_project(label: str, *, completed_task: bool = False) -> Dict[str, str]:
    project_id = f"{PREFIX}-{label}-project"
    phase_id = f"{PREFIX}-{label}-phase"
    task_id = f"{PREFIX}-{label}-task"
    create_project(project_id, f"{label} project", supervisor=f"{PREFIX}-supervisor", priority=1, config=CFG)
    create_phase(project_id, phase_id, "Main", config=CFG)
    create_task(
        phase_id,
        task_id,
        f"{label} task",
        owner=f"{PREFIX}-worker",
        priority=1,
        wake_owner_if_ready=False,
        config=CFG,
    )
    if completed_task:
        update_task_status(
            task_id,
            "completed",
            owner=f"{PREFIX}-worker",
            completion_evidence={"production_observation": "forced closure acceptance completed task"},
            completed_by=f"{PREFIX}-worker",
            config=CFG,
        )
    return {"project_id": project_id, "task_id": task_id}


def _list_row(project_id: str) -> Optional[Dict[str, Any]]:
    response = CLIENT.get("/api/projects")
    if response.status_code != 200:
        return None
    for row in response.json().get("projects", []):
        if row.get("id") == project_id:
            return row
    return None


def _assert_forced_surfaces(project_id: str, reason: str) -> None:
    detail = CLIENT.get(f"/api/projects/{project_id}")
    body = detail.json()
    nested = body.get("project") or {}
    row = _list_row(project_id) or {}
    _check("forced detail returns 200", detail.status_code == 200, body)
    _check("forced detail top-level flag", body.get("forced_closure") is True, body)
    _check("forced detail top-level reason", body.get("closure_reason") == reason, body)
    _check("forced detail top-level completed_by", body.get("completed_by") == "conductor", body)
    _check("forced detail nested flag", nested.get("forced_closure") is True, nested)
    _check("forced detail nested reason", nested.get("closure_reason") == reason, nested)
    _check("forced detail nested completed_by", nested.get("completed_by") == "conductor", nested)
    _check("forced list row flag", row.get("forced_closure") is True, row)
    _check("forced list row reason", row.get("closure_reason") == reason, row)
    _check("forced list row completed_by", row.get("completed_by") == "conductor", row)


def _assert_natural_surfaces(project_id: str) -> None:
    detail = CLIENT.get(f"/api/projects/{project_id}")
    body = detail.json()
    nested = body.get("project") or {}
    row = _list_row(project_id) or {}
    _check("natural detail returns 200", detail.status_code == 200, body)
    _check("natural detail top-level flag", body.get("forced_closure") is False, body)
    _check("natural detail top-level no reason", body.get("closure_reason") is None, body)
    _check("natural detail top-level completed_by", body.get("completed_by") == "conductor", body)
    _check("natural detail nested flag", nested.get("forced_closure") is False, nested)
    _check("natural detail nested no reason", nested.get("closure_reason") is None, nested)
    _check("natural detail nested completed_by", nested.get("completed_by") == "conductor", nested)
    _check("natural list row flag", row.get("forced_closure") is False, row)
    _check("natural list row no reason", row.get("closure_reason") is None, row)
    _check("natural list row completed_by", row.get("completed_by") == "conductor", row)


def main() -> int:
    init_schema(config=CFG)
    _cleanup()
    try:
        forced = _seed_project("forced")
        missing_reason = CLIENT.post(f"/api/projects/{forced['project_id']}/complete", json={"force": True})
        _check("force without reason is 422", missing_reason.status_code == 422, missing_reason.text)

        reason = "operator force-closed with incomplete work after audit"
        forced_resp = CLIENT.post(
            f"/api/projects/{forced['project_id']}/complete",
            json={"force": True, "reason": reason, "from": "conductor"},
        )
        forced_body = forced_resp.json()
        _check("force with reason completes", forced_resp.status_code == 200, forced_body)
        _check("force response keeps status completed", forced_body.get("status") == "completed", forced_body)
        _check("force response reports forced closure", forced_body.get("forced_closure") is True, forced_body)
        _check("force response reports reason", forced_body.get("closure_reason") == reason, forced_body)
        _check("force response reports completed_by", forced_body.get("completed_by") == "conductor", forced_body)
        _assert_forced_surfaces(forced["project_id"], reason)

        natural = _seed_project("natural", completed_task=True)
        natural_resp = CLIENT.post(f"/api/projects/{natural['project_id']}/complete", json={"from": "conductor"})
        natural_body = natural_resp.json()
        _check("natural completion succeeds", natural_resp.status_code == 200, natural_body)
        _check("natural response keeps status completed", natural_body.get("status") == "completed", natural_body)
        _check("natural response is not forced", natural_body.get("forced_closure") is False, natural_body)
        _check("natural response has no reason", natural_body.get("closure_reason") is None, natural_body)
        _check("natural response reports completed_by", natural_body.get("completed_by") == "conductor", natural_body)
        _assert_natural_surfaces(natural["project_id"])
    finally:
        _cleanup()
    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - forced project closure is persisted and natural completion remains distinct.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
