#!/usr/bin/env python3
"""Acceptance: applicable UNVERIFIED completed tasks do not satisfy dependencies.

The runtime contract is:
  - production_observation-only completions have no verifier path and keep releasing
    downstream tasks;
  - commit/loop-proof evidence has an applicable verifier, so UNVERIFIED completed
    tasks block downstream readiness and phase completion until VERIFIED.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from pathlib import Path
from unittest import mock

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

from fleet_orchestrator import orch_schema  # noqa: E402
from fleet_orchestrator.config import OrchConfig, get_neo4j_driver  # noqa: E402
from fleet_orchestrator.dispatch import OrchTaskNotReady, _claim_ready_orch_task  # noqa: E402
from fleet_orchestrator.evidence_verification import UNVERIFIED, VERIFIED  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    ReadyWorkConflictError,
    add_dependency,
    check_phase_complete,
    complete_project,
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
from fleet_orchestrator.test_isolation import assert_acceptance_redis_isolated  # noqa: E402


assert_acceptance_redis_isolated()


CFG = OrchConfig()
PREFIX = f"unverified-deps-{uuid.uuid4().hex[:8]}"
OWNER = f"{PREFIX}-owner"
WORKER = f"{OWNER}-codex"
REPO = "palios-taey/claude-code-fleet-orchestrator"
BAD_SHA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
GOOD_SHA = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
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
    create_project(project_id, name, supervisor=OWNER, priority=1, config=CFG)
    create_phase(project_id, phase_id, name, config=CFG)
    return project_id, phase_id


def _task(phase_id: str, name: str, priority: int = 10) -> str:
    task_id = f"{phase_id}::{name}"
    create_task(
        phase_id,
        task_id,
        name,
        owner=OWNER,
        priority=priority,
        wake_owner_if_ready=False,
        config=CFG,
    )
    return task_id


def _verification(status: str, sha: str) -> dict:
    return {
        "status": status,
        "verified": status == VERIFIED,
        "applies": True,
        "source": "acceptance-mock",
        "repo": REPO,
        "commit_sha": sha,
        "required_checks": ["r5-audit-gate"],
        "producer": "acceptance",
        "reason": "mocked acceptance verification",
        "checks": [],
    }


def _complete_with_verification(task_id: str, sha: str, status: str) -> None:
    with mock.patch.object(orch_schema, "verify_completion_evidence", return_value=_verification(status, sha)):
        update_task_status(
            task_id,
            "completed",
            completion_evidence={
                "commit_sha": sha,
                "repo": REPO,
                "production_observation": f"{status.lower()} completion dependency acceptance",
            },
            completed_by="acceptance",
            config=CFG,
        )


def _ready_ids() -> set[str]:
    return {str(row["id"]) for row in get_ready_tasks(CFG) if str(row["id"]).startswith(PREFIX)}


def _phase_status(phase_id: str) -> str:
    with _driver().session(database=CFG.neo4j_db) as session:
        row = session.run(
            "MATCH (ph:OrchPhase {id: $phase_id}) RETURN coalesce(ph.status, '') AS status",
            phase_id=phase_id,
        ).single()
    return str(row["status"] if row else "")


def _seed_dependency_pair(name: str) -> tuple[str, str, str, str]:
    project_id, phase_id = _project(name)
    dep = _task(phase_id, "dep", 5)
    downstream = _task(phase_id, "downstream", 1)
    add_dependency(downstream, dep, config=CFG)
    return project_id, phase_id, dep, downstream


def _assert_not_ready(project_id: str, downstream: str) -> None:
    current_next = get_session_next_ready(OWNER, project_id=project_id, config=CFG)
    project_ready = get_project_ready_tasks(project_id, OWNER, CFG)
    _check("UNVERIFIED applicable dep absent from get_ready_tasks", downstream not in _ready_ids(), _ready_ids())
    _check("UNVERIFIED applicable dep blocks get_session_next_ready", current_next is None, current_next)
    _check("UNVERIFIED applicable dep blocks get_project_ready_tasks", project_ready == [], project_ready)
    try:
        _claim_ready_orch_task(downstream, WORKER)
    except OrchTaskNotReady as exc:
        _check("UNVERIFIED applicable dep blocks atomic dispatch claim", "incomplete_deps=1" in str(exc), str(exc))
    else:
        _check("UNVERIFIED applicable dep blocks atomic dispatch claim", False, get_task(downstream, CFG))


def _assert_ready(project_id: str, downstream: str) -> None:
    current_next = get_session_next_ready(OWNER, project_id=project_id, config=CFG)
    project_ready = get_project_ready_tasks(project_id, OWNER, CFG)
    _check("satisfied dep appears in get_ready_tasks", downstream in _ready_ids(), _ready_ids())
    _check("satisfied dep appears in get_session_next_ready", (current_next or {}).get("task_id") == downstream, current_next)
    _check(
        "satisfied dep appears in get_project_ready_tasks",
        [row.get("id") for row in project_ready] == [downstream],
        project_ready,
    )


def main() -> int:
    init_schema(config=CFG)
    _cleanup()
    try:
        prod_project, _, prod_dep, prod_downstream = _seed_dependency_pair("production-only")
        update_task_status(
            prod_dep,
            "completed",
            completion_evidence={"production_observation": "production-only dependency acceptance"},
            completed_by="acceptance",
            config=CFG,
        )
        prod_payload = get_task(prod_dep, CFG) or {}
        _check(
            "production-only completion is UNVERIFIED but verifier does not apply",
            prod_payload.get("completion_evidence_verification_status") == UNVERIFIED
            and prod_payload.get("completion_evidence_verified") is False
            and prod_payload.get("completion_evidence_verification_applies") is False,
            json.dumps(prod_payload.get("completion_evidence_verification"), sort_keys=True),
        )
        _assert_ready(prod_project, prod_downstream)

        bad_project, _, bad_dep, bad_downstream = _seed_dependency_pair("unverified")
        _complete_with_verification(bad_dep, BAD_SHA, UNVERIFIED)
        bad_payload = get_task(bad_dep, CFG) or {}
        _check(
            "commit-backed UNVERIFIED completion records applicable verifier",
            bad_payload.get("status") == "completed"
            and bad_payload.get("completion_evidence_verification_status") == UNVERIFIED
            and bad_payload.get("completion_evidence_verified") is False
            and bad_payload.get("completion_evidence_verification_applies") is True,
            json.dumps(bad_payload.get("completion_evidence_verification"), sort_keys=True),
        )
        _assert_not_ready(bad_project, bad_downstream)

        phase_project, phase_id = _project("unverified-phase")
        phase_task = _task(phase_id, "only", 1)
        _complete_with_verification(phase_task, BAD_SHA, UNVERIFIED)
        _check("phase with only applicable UNVERIFIED completed task does not complete", check_phase_complete(phase_id, CFG) is False, _phase_status(phase_id))
        _check("applicable UNVERIFIED completed task still counts incomplete for project completion", _project_completion_blocked(phase_project), "")

        good_project, good_phase, good_dep, good_downstream = _seed_dependency_pair("verified")
        _complete_with_verification(good_dep, GOOD_SHA, VERIFIED)
        good_payload = get_task(good_dep, CFG) or {}
        _check(
            "commit-backed VERIFIED completion records applicable verifier",
            good_payload.get("completion_evidence_verification_status") == VERIFIED
            and good_payload.get("completion_evidence_verified") is True
            and good_payload.get("completion_evidence_verification_applies") is True,
            json.dumps(good_payload.get("completion_evidence_verification"), sort_keys=True),
        )
        _assert_ready(good_project, good_downstream)
        _claim_ready_orch_task(good_downstream, WORKER)
        _check("VERIFIED dep allows atomic dispatch claim", (get_task(good_downstream, CFG) or {}).get("status") == "in_progress", get_task(good_downstream, CFG))
        update_task_status(
            good_downstream,
            "completed",
            completion_evidence={"production_observation": "verified downstream completed for phase acceptance"},
            completed_by="acceptance",
            config=CFG,
        )
        _check("phase with VERIFIED dep and no-verifier completion completes", check_phase_complete(good_phase, CFG) is True, _phase_status(good_phase))

        if FAILURES:
            print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
            return 1
        print("\nPASS - UNVERIFIED applicable completions do not satisfy dependency or completion gates")
        return 0
    finally:
        _cleanup()


def _project_completion_blocked(project_id: str) -> bool:
    try:
        complete_project(project_id, force=False, completed_by="acceptance", config=CFG)
    except ReadyWorkConflictError:
        return True
    return False


if __name__ == "__main__":
    raise SystemExit(main())
