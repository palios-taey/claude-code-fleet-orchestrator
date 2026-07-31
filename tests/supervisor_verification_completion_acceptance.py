#!/usr/bin/env python3
"""Acceptance: supervisor verification can satisfy non-production dependency gates."""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if "ORCH_DOTENV" not in os.environ:
    for candidate in (
        ROOT / ".env",
        Path.home() / "claude-code-fleet-orchestrator/.env",
    ):
        if candidate.is_file():
            os.environ["ORCH_DOTENV"] = str(candidate)
            break

from fleet_orchestrator.config import OrchConfig, get_neo4j_driver  # noqa: E402
from fleet_orchestrator.dispatch import OrchTaskNotReady, _claim_ready_orch_task  # noqa: E402
from fleet_orchestrator.evidence_verification import UNVERIFIED, VERIFIED  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    CompletionEvidenceError,
    add_dependency,
    create_phase,
    create_project,
    create_task,
    get_project_ready_tasks,
    get_ready_tasks,
    get_session_next_ready,
    get_task,
    init_schema,
    update_task_status,
)
from fleet_orchestrator.tasks_api import app  # noqa: E402
from fleet_orchestrator.test_isolation import assert_acceptance_redis_isolated  # noqa: E402


assert_acceptance_redis_isolated()


CFG = OrchConfig()
PREFIX = f"supervisor-verify-{uuid.uuid4().hex[:8]}"
SUPERVISOR = f"{PREFIX}-supervisor"
WORKER = f"{SUPERVISOR}-codex"
REPO = "palios-taey/claude-code-fleet-orchestrator"
UNVERIFIED_SHA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _driver():
    return get_neo4j_driver(CFG)


def _cleanup() -> None:
    with _driver().session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=PREFIX)


def _project(name: str) -> tuple[str, str]:
    project_id = f"{PREFIX}-{name}"
    phase_id = f"{project_id}::phase"
    create_project(project_id, name, supervisor=SUPERVISOR, priority=1, config=CFG)
    create_phase(project_id, phase_id, name, config=CFG)
    return project_id, phase_id


def _task(phase_id: str, name: str, priority: int = 10) -> str:
    task_id = f"{phase_id}::{name}"
    create_task(
        phase_id,
        task_id,
        name,
        owner=WORKER,
        priority=priority,
        wake_owner_if_ready=False,
        config=CFG,
    )
    return task_id


def _seed_dependency_pair(name: str) -> tuple[str, str, str, str]:
    project_id, phase_id = _project(name)
    dep = _task(phase_id, "dep", 5)
    downstream = _task(phase_id, "downstream", 1)
    add_dependency(downstream, dep, config=CFG)
    return project_id, phase_id, dep, downstream


def _supervisor_evidence(*, verifier: str = SUPERVISOR, mode: str = "research") -> dict:
    return {
        "supervisor_verification": {
            "mode": mode,
            "verifier": verifier,
            "observation": "supervisor inspected the research artifact and accepted the dependency gate",
        }
    }


def _ready_ids() -> set[str]:
    return {str(row["id"]) for row in get_ready_tasks(CFG) if str(row["id"]).startswith(PREFIX)}


def _assert_ready(project_id: str, downstream: str) -> None:
    current_next = get_session_next_ready(WORKER, project_id=project_id, config=CFG)
    project_ready = get_project_ready_tasks(project_id, WORKER, CFG)
    _check("supervisor-verified dep appears in get_ready_tasks", downstream in _ready_ids(), _ready_ids())
    _check("supervisor-verified dep appears in get_session_next_ready", (current_next or {}).get("task_id") == downstream, current_next)
    _check(
        "supervisor-verified dep appears in get_project_ready_tasks",
        [row.get("id") for row in project_ready] == [downstream],
        project_ready,
    )
    _claim_ready_orch_task(downstream, WORKER)
    _check("supervisor-verified dep allows atomic dispatch claim", (get_task(downstream, CFG) or {}).get("status") == "in_progress", get_task(downstream, CFG))


def _assert_not_ready(project_id: str, downstream: str) -> None:
    current_next = get_session_next_ready(WORKER, project_id=project_id, config=CFG)
    project_ready = get_project_ready_tasks(project_id, WORKER, CFG)
    _check("commit-backed UNVERIFIED dep absent from get_ready_tasks", downstream not in _ready_ids(), _ready_ids())
    _check("commit-backed UNVERIFIED dep blocks get_session_next_ready", current_next is None, current_next)
    _check("commit-backed UNVERIFIED dep blocks get_project_ready_tasks", project_ready == [], project_ready)
    try:
        _claim_ready_orch_task(downstream, WORKER)
    except OrchTaskNotReady as exc:
        _check("commit-backed UNVERIFIED dep blocks atomic dispatch claim", "incomplete_deps=1" in str(exc), str(exc))
    else:
        _check("commit-backed UNVERIFIED dep blocks atomic dispatch claim", False, get_task(downstream, CFG))


def main() -> int:
    init_schema(config=CFG)
    client = TestClient(app)
    previous_allowlist = os.environ.get("ORCH_COMPLETION_ALLOWED_REPOS")
    os.environ["ORCH_COMPLETION_ALLOWED_REPOS"] = ""
    _cleanup()
    try:
        research_project, _, research_dep, research_downstream = _seed_dependency_pair("research")
        update_task_status(
            research_dep,
            "completed",
            completion_evidence=_supervisor_evidence(),
            completed_by=WORKER,
            config=CFG,
        )
        research_payload = get_task(research_dep, CFG) or {}
        verification = research_payload.get("completion_evidence_verification") or {}
        _check(
            "research completion stores supervisor verification as VERIFIED evidence",
            research_payload.get("status") == "completed"
            and research_payload.get("completion_evidence", {}).get("supervisor_verification", {}).get("verifier") == SUPERVISOR
            and research_payload.get("completion_evidence_verification_status") == VERIFIED
            and research_payload.get("completion_evidence_verified") is True
            and research_payload.get("completion_evidence_verification_applies") is True
            and verification.get("source") == "supervisor-verification"
            and verification.get("producer") == WORKER
            and verification.get("verifier") == SUPERVISOR,
            json.dumps(verification, sort_keys=True),
        )
        _assert_ready(research_project, research_downstream)

        api_project, _, api_dep, api_downstream = _seed_dependency_pair("api-supervisor")
        response = client.patch(
            f"/api/task/{api_dep}",
            json={
                "status": "completed",
                "from": SUPERVISOR,
                "evidence": _supervisor_evidence(mode="prototype"),
            },
        )
        api_payload = get_task(api_dep, CFG) or {}
        api_verification = api_payload.get("completion_evidence_verification") or {}
        _check("supervisor API close with supervisor_verification is accepted", response.status_code == 200, response.text)
        _check(
            "supervisor API close attributes producer to peer owner",
            api_payload.get("status") == "completed"
            and api_payload.get("completed_by") == WORKER
            and api_payload.get("completion_evidence_verification_status") == VERIFIED
            and api_payload.get("completion_evidence_verified") is True
            and api_payload.get("completion_evidence_verification_applies") is True
            and api_verification.get("source") == "supervisor-verification"
            and api_verification.get("producer") == WORKER
            and api_verification.get("verifier") == SUPERVISOR,
            json.dumps(api_verification, sort_keys=True),
        )
        _assert_ready(api_project, api_downstream)

        api_production_project, _, api_production_dep, api_production_downstream = _seed_dependency_pair("api-production")
        api_production_response = client.patch(
            f"/api/task/{api_production_dep}",
            json={
                "status": "completed",
                "from": SUPERVISOR,
                "evidence": {
                    "commit_sha": UNVERIFIED_SHA,
                    "repo": REPO,
                    "production_observation": "commit evidence must keep closer attribution through API",
                    **_supervisor_evidence(mode="prototype"),
                },
            },
        )
        api_production_payload = get_task(api_production_dep, CFG) or {}
        api_production_verification = api_production_payload.get("completion_evidence_verification") or {}
        _check("supervisor API close with commit evidence is accepted", api_production_response.status_code == 200, api_production_response.text)
        _check(
            "supervisor API commit evidence keeps closer attribution",
            api_production_payload.get("completed_by") == SUPERVISOR
            and api_production_payload.get("completion_evidence_verification_status") == UNVERIFIED
            and api_production_payload.get("completion_evidence_verified") is False
            and api_production_payload.get("completion_evidence_verification_applies") is True
            and api_production_verification.get("source") == "github-required-checks"
            and api_production_verification.get("producer") == SUPERVISOR,
            json.dumps(api_production_verification, sort_keys=True),
        )
        _assert_not_ready(api_production_project, api_production_downstream)

        production_project, _, production_dep, production_downstream = _seed_dependency_pair("production")
        update_task_status(
            production_dep,
            "completed",
            completion_evidence={
                "commit_sha": UNVERIFIED_SHA,
                "repo": REPO,
                "production_observation": "commit-backed production work must still satisfy GitHub gates",
                **_supervisor_evidence(mode="prototype"),
            },
            completed_by=WORKER,
            config=CFG,
        )
        production_payload = get_task(production_dep, CFG) or {}
        production_verification = production_payload.get("completion_evidence_verification") or {}
        _check(
            "production commit evidence is not rescued by supervisor_verification",
            production_payload.get("completion_evidence", {}).get("supervisor_verification", {}).get("verifier") == SUPERVISOR
            and production_payload.get("completion_evidence_verification_status") == UNVERIFIED
            and production_payload.get("completion_evidence_verified") is False
            and production_payload.get("completion_evidence_verification_applies") is True
            and production_verification.get("source") == "github-required-checks",
            json.dumps(production_verification, sort_keys=True),
        )
        _assert_not_ready(production_project, production_downstream)

        _, _, self_dep, _ = _seed_dependency_pair("self-attestation")
        try:
            update_task_status(
                self_dep,
                "completed",
                completion_evidence=_supervisor_evidence(verifier=WORKER, mode="prototype"),
                completed_by=WORKER,
                config=CFG,
            )
        except CompletionEvidenceError as exc:
            _check(
                "self-attestation is rejected with verifier/producer guidance",
                "self-attestation" in str(exc) and "verifier must differ from producer" in str(exc),
                str(exc),
            )
        else:
            _check("self-attestation is rejected with verifier/producer guidance", False, get_task(self_dep, CFG))
        _check("self-attestation rejection does not complete task", (get_task(self_dep, CFG) or {}).get("status") != "completed", get_task(self_dep, CFG))

        if FAILURES:
            print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
            return 1
        print("\nPASS - supervisor verification satisfies non-production gates without bypassing production gates")
        return 0
    finally:
        if previous_allowlist is None:
            os.environ.pop("ORCH_COMPLETION_ALLOWED_REPOS", None)
        else:
            os.environ["ORCH_COMPLETION_ALLOWED_REPOS"] = previous_allowlist
        _cleanup()


if __name__ == "__main__":
    raise SystemExit(main())
