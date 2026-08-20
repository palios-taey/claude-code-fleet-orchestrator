#!/usr/bin/env python3
"""Acceptance: executor-owned loops are visible without widening supervisor authority."""
from __future__ import annotations

import os
import re
import sys
import uuid


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


def _require_test_namespace() -> str:
    raw = (os.environ.get("ORCH_TEST_NAMESPACE") or "").strip()
    if not raw:
        raise SystemExit("ORCH_TEST_NAMESPACE is required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{5,80}", raw):
        raise SystemExit("ORCH_TEST_NAMESPACE must be 6-80 chars of letters/digits/._:-")
    lowered = raw.lower()
    if not any(marker in lowered for marker in ("test", "ci", "acceptance")):
        raise SystemExit("ORCH_TEST_NAMESPACE must include test, ci, or acceptance")
    if lowered in {"prod", "production", "neo4j", "default"}:
        raise SystemExit("ORCH_TEST_NAMESPACE must not name a production/default namespace")
    return raw


_NAMESPACE = _require_test_namespace()
_PFX = f"{_NAMESPACE}-executor-display-{uuid.uuid4().hex[:8]}"
TREASURER = f"{_PFX}-treasurer"
LINKEDIN = f"{_PFX}-linkedin"
CAREERS_SUPERVISOR = f"{_PFX}-careers-supervisor"
CAREERS_EXECUTOR = f"{_PFX}-careers-executor"
BOTH = f"{_PFX}-both"
IDLE = f"{_PFX}-idle"
PEER_SUPERVISOR = f"{_PFX}-peer-sup"
PEER = f"{PEER_SUPERVISOR}-codex"
os.environ["ORCH_SESSION_IDS"] = ",".join([
    TREASURER,
    LINKEDIN,
    CAREERS_SUPERVISOR,
    CAREERS_EXECUTOR,
    BOTH,
    IDLE,
    PEER_SUPERVISOR,
])
os.environ["ORCH_PUBLIC_SHOW_SESSIONS"] = os.environ["ORCH_SESSION_IDS"]
os.environ["ORCH_PUBLIC_HIDE_SESSIONS"] = ""
os.environ["ORCH_PUBLIC_HIDE_PROJECT_IDS"] = ""
from fleet_orchestrator.test_isolation import assert_acceptance_redis_isolated  # noqa: E402

assert_acceptance_redis_isolated()
os.environ.setdefault("ORCH_NEO4J_URI", "bolt://127.0.0.1:7687")
os.environ.setdefault("ORCH_NEO4J_DB", "neo4j")

from fastapi.testclient import TestClient  # noqa: E402

from fleet_orchestrator.config import OrchConfig, get_neo4j_driver  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    _raw_stop_decision,
    create_phase,
    create_project,
    create_task,
    get_session_dashboard_projects,
    get_session_supervised_projects,
    get_supervisor_dispatchable_peer_task,
    init_schema,
)
from fleet_orchestrator.public_readonly import app as public_app  # noqa: E402
from fleet_orchestrator.tasks_api import app  # noqa: E402


CFG = OrchConfig()
CLIENT = TestClient(app)
PUBLIC_CLIENT = TestClient(public_app)
FAILURES: list[str] = []


def _check(label: str, condition: bool, detail: object = "") -> None:
    print(("PASS " if condition else "FAIL ") + label + ("" if condition else f" -> {detail}"))
    if not condition:
        FAILURES.append(label)


def _cleanup() -> None:
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=_PFX)


def _set_project_status(project_id: str, status: str) -> None:
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run("MATCH (p:OrchProject {id:$project_id}) SET p.status=$status", project_id=project_id, status=status)


def _create_loop(project_id: str, name: str, supervisor: str, owner: str, task_count: int = 3) -> list[str]:
    create_project(project_id, name, supervisor=supervisor, priority=10, config=CFG)
    phase_id = f"{project_id}::cycle"
    create_phase(project_id, phase_id, "cycle", config=CFG)
    tasks: list[str] = []
    for idx in range(1, task_count + 1):
        task_id = f"{project_id}::step-{idx}"
        create_task(
            phase_id,
            task_id,
            f"{name} step {idx}",
            owner=owner,
            priority=idx,
            wake_owner_if_ready=False,
            config=CFG,
        )
        tasks.append(task_id)
    return tasks


def _project_rows(session_id: str) -> list[dict]:
    response = CLIENT.get(f"/api/sessions/{session_id}/projects")
    _check(f"{session_id} projects endpoint returns 200", response.status_code == 200, response.text)
    return response.json().get("projects", [])


def _public_project_rows(session_id: str) -> list[dict]:
    response = PUBLIC_CLIENT.get(f"/api/sessions/{session_id}/projects")
    _check(f"{session_id} public projects endpoint returns 200", response.status_code == 200, response.text)
    return response.json().get("projects", [])


def _row(rows: list[dict], project_id: str) -> dict | None:
    return next((row for row in rows if row.get("id") == project_id), None)


def _fixture_task_owners() -> set[str]:
    configured = [part.strip() for part in os.environ["ORCH_SESSION_IDS"].split(",") if part.strip()]
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        rows = session.run(
            """
            MATCH (t:OrchTask)
            WHERE t.id STARTS WITH $prefix
              AND t.owner IN $configured
            RETURN DISTINCT t.owner AS owner
            """,
            prefix=_PFX,
            configured=configured,
        )
        return {str(record["owner"]) for record in rows}


def main() -> int:
    _cleanup()
    init_schema(config=CFG)
    try:
        linkedin_project = f"{_PFX}-hourly-linkedin-loop"
        careers_project = f"{_PFX}-careers-execution-loop"
        both_project = f"{_PFX}-both-loop"
        completed_project = f"{_PFX}-completed-history"
        peer_project = f"{_PFX}-peer-tripwire"

        _create_loop(linkedin_project, "Hourly LinkedIn Engagement Cycle", TREASURER, LINKEDIN, task_count=6)
        _create_loop(careers_project, "careers-execution-loop", CAREERS_SUPERVISOR, CAREERS_EXECUTOR, task_count=2)
        _create_loop(both_project, "both relation loop", BOTH, BOTH, task_count=1)
        _create_loop(completed_project, "completed history", TREASURER, LINKEDIN, task_count=1)
        _set_project_status(completed_project, "completed")
        _create_loop(peer_project, "peer authority tripwire", PEER_SUPERVISOR, PEER, task_count=1)

        treasurer_rows = _project_rows(TREASURER)
        linkedin_rows = _project_rows(LINKEDIN)
        careers_rows = _project_rows(CAREERS_EXECUTOR)
        both_rows = _project_rows(BOTH)
        public_linkedin_rows = _public_project_rows(LINKEDIN)

        _check("supervisor display includes supervised executor loop",
               _row(treasurer_rows, linkedin_project) is not None,
               treasurer_rows)
        _check("executor display includes owned executor loop",
               _row(linkedin_rows, linkedin_project) is not None,
               linkedin_rows)
        _check("supervisor relation is tagged",
               (_row(treasurer_rows, linkedin_project) or {}).get("session_relation") == "supervises",
               _row(treasurer_rows, linkedin_project))
        _check("executor relation is tagged",
               (_row(linkedin_rows, linkedin_project) or {}).get("session_relation") == "executes",
               _row(linkedin_rows, linkedin_project))
        _check("public readonly executor display uses the same union",
               (_row(public_linkedin_rows, linkedin_project) or {}).get("session_relation") == "executes",
               public_linkedin_rows)
        _check("completed owner-history project is excluded",
               _row(linkedin_rows, completed_project) is None,
               linkedin_rows)
        _check("general executor loop shape is not LinkedIn-special-cased",
               (_row(careers_rows, careers_project) or {}).get("session_relation") == "executes",
               careers_rows)
        _check("both-role session gets one deduped project row",
               sum(1 for row in both_rows if row.get("id") == both_project) == 1
               and (_row(both_rows, both_project) or {}).get("session_relation") == "both",
               both_rows)

        for session_id in sorted(_fixture_task_owners()):
            rows = _project_rows(session_id)
            _check(f"ORCH_SESSION_IDS owner has non-empty dashboard view: {session_id}", bool(rows), rows)

        supervised_for_executor = get_session_supervised_projects(LINKEDIN, config=CFG)
        dashboard_for_executor = get_session_dashboard_projects(LINKEDIN, config=CFG)
        _check("#194 tripwire: supervised authority remains strict",
               all(row.get("id") != linkedin_project for row in supervised_for_executor)
               and any(row.get("id") == linkedin_project for row in dashboard_for_executor),
               {"supervised": supervised_for_executor, "dashboard": dashboard_for_executor})
        _check("#194 tripwire: executor is not treated as peer supervisor",
               get_supervisor_dispatchable_peer_task(LINKEDIN, linkedin_project, config=CFG) is None,
               get_supervisor_dispatchable_peer_task(LINKEDIN, linkedin_project, config=CFG))
        treasurer_decision = _raw_stop_decision(PEER_SUPERVISOR, config=CFG)
        _check("#194 tripwire: true supervisor still dispatches peer work",
               treasurer_decision.get("task_id") == f"{peer_project}::step-1"
               and treasurer_decision.get("dispatch_to") == PEER,
               treasurer_decision)
    finally:
        _cleanup()

    if FAILURES:
        print(f"\nFAIL -- {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS -- executor loop display is unioned while supervisor authority stays scoped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
