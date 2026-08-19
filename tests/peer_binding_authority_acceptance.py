"""Acceptance: peer sessions cannot execute parent-owned recurring tasks unbound.

Regression: a peer session could self-select a parent-owned recurring/plan task
from tracker state and write in_progress/completed without an explicit dispatch
binding, racing the parent on the same work.

Env: ORCH_NEO4J_URI, ORCH_NEO4J_DB, ORCH_REDIS_HOST/PORT,
     REDIS_HOST/PORT, ORCH_TEST_NAMESPACE (required; must include test/ci/acceptance).
"""
from __future__ import annotations

import concurrent.futures
import json
import os
import re
import sys
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
if "ORCH_DOTENV" not in os.environ:
    for candidate in (ROOT / ".env", Path.home() / "claude-code-fleet-orchestrator/.env"):
        if candidate.is_file():
            os.environ["ORCH_DOTENV"] = str(candidate)
            break


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
os.environ["NOTIFY_KEY_PREFIX"] = f"{_NAMESPACE}:peer-binding:{uuid.uuid4().hex[:8]}"

from fleet_orchestrator.config import OrchConfig, get_neo4j_driver  # noqa: E402
from fleet_orchestrator.dispatch import OrchTaskNotReady, _claim_ready_orch_task, _state_key  # noqa: E402
from fleet_orchestrator.notify_state import redis_connect as notify_redis_connect  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    create_phase,
    create_project,
    create_task,
    get_session_next_ready,
    get_task,
    init_schema,
    update_task_status,
)
from fleet_orchestrator.tasks_api import app  # noqa: E402

CFG = OrchConfig()
R = notify_redis_connect()
PFX = f"{_NAMESPACE}-peer-binding-{uuid.uuid4().hex[:8]}"
SUP = f"{PFX}-jd-reader"
PEER = f"{SUP}-gemini"
PEER2 = f"{SUP}-grok"
SELF_PEER = f"{SUP}-codex"
PROJECT = f"{PFX}-project"
PHASE = f"{PROJECT}::phase"
PARENT_RECUR = f"{PROJECT}::parent-recurring"
EXPLICIT = f"{PROJECT}::explicit-peer"
CLAIM_RECUR = f"{PROJECT}::claim-recurring"
SELF_OWNED = f"{PROJECT}::self-owned"
SUP_OWNED = f"{PROJECT}::supervisor-owned"
DELEGATED = f"{PROJECT}::delegated-control"
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _driver():
    return get_neo4j_driver(CFG)


def _cleanup() -> None:
    R.delete(*[_state_key(peer, suffix) for peer in (SUP, PEER, PEER2, SELF_PEER) for suffix in ("current_task", "last_outcome", "parent")])
    with _driver().session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=PFX)


def _mark_recurring(*task_ids: str) -> None:
    with _driver().session(database=CFG.neo4j_db) as session:
        session.run("MATCH (t:OrchTask) WHERE t.id IN $ids SET t.recurring = true", ids=list(task_ids))


def _set_dispatched(task_id: str, peer: str) -> None:
    with _driver().session(database=CFG.neo4j_db) as session:
        session.run("MATCH (t:OrchTask {id: $task_id}) SET t.dispatched_to = $peer", task_id=task_id, peer=peer)


def _bind(peer: str, task_id: str) -> None:
    R.set(
        _state_key(peer, "current_task"),
        json.dumps({"task_id": task_id, "description": task_id, "supervisor": SUP, "started_at": 123.0}),
    )


def _current_task_id(peer: str) -> str:
    raw = R.get(_state_key(peer, "current_task"))
    if not raw:
        return ""
    return str(json.loads(raw).get("task_id") or "")


def _setup() -> None:
    init_schema(config=CFG)
    create_project(project_id=PROJECT, name=PROJECT, supervisor=SUP, priority=1, config=CFG)
    create_phase(project_id=PROJECT, phase_id=PHASE, name="phase", config=CFG)
    for task_id in (PARENT_RECUR, EXPLICIT, CLAIM_RECUR, SUP_OWNED, DELEGATED):
        create_task(
            phase_id=PHASE,
            task_id=task_id,
            description=task_id,
            priority=10,
            owner=SUP,
            wake_owner_if_ready=False,
            config=CFG,
        )
    create_task(
        phase_id=PHASE,
        task_id=SELF_OWNED,
        description=SELF_OWNED,
        priority=20,
        owner=SELF_PEER,
        wake_owner_if_ready=False,
        config=CFG,
    )
    _mark_recurring(PARENT_RECUR, CLAIM_RECUR)


def _patch(client: TestClient, task_id: str, body: dict) -> tuple[int, dict]:
    response = client.patch(f"/api/task/{task_id}", json=body)
    try:
        payload = response.json()
    except Exception:
        payload = {"text": response.text}
    return response.status_code, payload


def _claim(worker: str) -> tuple[str, str]:
    try:
        _claim_ready_orch_task(CLAIM_RECUR, worker)
        return ("won", worker)
    except OrchTaskNotReady:
        return ("lost", worker)


def main() -> int:
    _cleanup()
    try:
        _setup()
        client = TestClient(app)

        _check(
            "peer next-ready does not surface parent-owned recurring task",
            get_session_next_ready(PEER, project_id=PROJECT, config=CFG) is None,
            get_session_next_ready(PEER, project_id=PROJECT, config=CFG),
        )

        code, body = _patch(client, PARENT_RECUR, {"status": "in_progress", "from": PEER})
        _check("unbound peer in_progress is rejected", code == 409 and body.get("ok") is False, body)
        _check("rejected in_progress leaves parent task pending", get_task(PARENT_RECUR, CFG).get("status") == "pending", get_task(PARENT_RECUR, CFG))

        code, body = _patch(
            client,
            PARENT_RECUR,
            {
                "status": "completed",
                "from": PEER,
                "evidence": {"production_observation": "unbound peer completion must be rejected"},
            },
        )
        _check("unbound peer completion is rejected", code == 409 and body.get("ok") is False, body)
        _check("rejected completion leaves parent task pending", get_task(PARENT_RECUR, CFG).get("status") == "pending", get_task(PARENT_RECUR, CFG))

        code, body = _patch(client, SUP_OWNED, {"status": "in_progress", "from": SUP, "blocked_on": "AWAIT:external-signal:acceptance"})
        _check("supervisor in_progress update is allowed", code == 200 and body.get("ok") is True, body)
        _check("supervisor in_progress update does not bind current_task", _current_task_id(SUP) == "", _current_task_id(SUP))

        code, body = _patch(client, SELF_OWNED, {"status": "in_progress", "from": SELF_PEER})
        _check("self-owned peer in_progress is allowed", code == 200 and body.get("ok") is True, body)
        _check("self-owned peer in_progress binds its own current_task", _current_task_id(SELF_PEER) == SELF_OWNED, _current_task_id(SELF_PEER))

        _set_dispatched(EXPLICIT, PEER)
        ready = get_session_next_ready(PEER, project_id=PROJECT, config=CFG)
        _check("explicit dispatched_to task is peer next-ready", (ready or {}).get("task_id") == EXPLICIT, ready)

        code, body = _patch(client, EXPLICIT, {"status": "in_progress", "from": PEER})
        _check("explicit dispatch without current_task binding still rejects start", code == 409 and body.get("ok") is False, body)
        _check("unbound explicit task remains pending", get_task(EXPLICIT, CFG).get("status") == "pending", get_task(EXPLICIT, CFG))

        _bind(PEER, EXPLICIT)
        code, body = _patch(client, EXPLICIT, {"status": "in_progress", "from": PEER})
        _check("explicit dispatch with current_task binding can start", code == 200 and body.get("ok") is True, body)
        explicit = get_task(EXPLICIT, CFG)
        _check("bound explicit task is in_progress", explicit.get("status") == "in_progress", explicit)

        _claim_ready_orch_task(DELEGATED, PEER2, supervisor=SELF_PEER)
        delegated = get_task(DELEGATED, CFG)
        _check("distinct dispatch supervisor becomes task owner", delegated.get("owner") == SELF_PEER, delegated)
        _check("delegated worker remains the executor", delegated.get("dispatched_to") == PEER2, delegated)

        _bind(PEER2, DELEGATED)
        code, body = _patch(
            client,
            DELEGATED,
            {
                "status": "completed",
                "from": PEER2,
                "evidence": {"production_observation": "delegated worker cannot self-close"},
            },
        )
        _check("delegated worker self-completion is rejected", code == 409 and body.get("ok") is False, body)
        _check("rejected worker completion leaves delegated task in_progress", get_task(DELEGATED, CFG).get("status") == "in_progress", get_task(DELEGATED, CFG))

        code, body = _patch(
            client,
            DELEGATED,
            {
                "status": "completed",
                "from": SELF_PEER,
                "evidence": {"production_observation": "owning supervisor verified delegated work"},
            },
        )
        _check("owning suffixed supervisor can close delegated work", code == 200 and body.get("ok") is True, body)
        _check("supervisor close completes delegated task", get_task(DELEGATED, CFG).get("status") == "completed", get_task(DELEGATED, CFG))

        update_task_status(
            CLAIM_RECUR,
            "completed",
            owner=SUP,
            completion_evidence={"production_observation": "first recurring cycle completed"},
            completed_by=SUP,
            config=CFG,
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(_claim, [PEER, PEER2]))
        winners = [worker for outcome, worker in results if outcome == "won"]
        claim_task = get_task(CLAIM_RECUR, CFG)
        _check("completed recurring claim has exactly one winner", len(winners) == 1, results)
        _check("recurring claim binds the single winning peer", claim_task.get("dispatched_to") == winners[0], claim_task)
    finally:
        _cleanup()

    if FAILURES:
        print(f"\nFAIL -- {len(FAILURES)} failures: {FAILURES}")
        return 1
    print("\nPASS -- peer execution requires explicit current_task binding.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
