"""Acceptance: generic chat replies cannot complete formal human-review gates."""
from __future__ import annotations

import json
import os
import re
import sys
import uuid

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet_orchestrator.config import OrchConfig, get_redis_sync  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    add_dependency,
    create_human_review_gate,
    create_phase,
    create_project,
    create_question,
    create_task,
    get_neo4j_driver,
    get_session_next_ready,
    get_task,
    init_schema,
)
from fleet_orchestrator.tasks_api import app  # noqa: E402


def _require_test_namespace() -> str:
    raw = (os.environ.get("ORCH_TEST_NAMESPACE") or "").strip()
    if not raw:
        raise SystemExit("ORCH_TEST_NAMESPACE is required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{5,80}", raw):
        raise SystemExit("ORCH_TEST_NAMESPACE must be 6-80 chars of letters/digits/._:-")
    if not any(marker in raw.lower() for marker in ("test", "ci", "acceptance")):
        raise SystemExit("ORCH_TEST_NAMESPACE must include test, ci, or acceptance")
    return raw


CFG = OrchConfig()
PFX = f"{_require_test_namespace()}-hrg-unbound-{uuid.uuid4().hex[:8]}"
SUP = f"{PFX}-sup"
REVIEWER = f"{PFX}-human"
PROJECT = f"{PFX}-project"
PHASE = f"{PROJECT}::phase"
GATE_ONE = f"{PROJECT}::human-review-one"
QUESTION_ONE = f"{PFX}-question-one"
GATE_TWO = f"{PROJECT}::human-review-two"
QUESTION_TWO = f"{PFX}-question-two"
ORDINARY_TASK = f"{PROJECT}::ordinary-question-task"
ORDINARY_QUESTION = f"{PFX}-ordinary-question"
DOWNSTREAM = f"{PROJECT}::downstream"
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _redis_key(kind: str) -> str:
    prefix = os.environ.get("NOTIFY_KEY_PREFIX", "taey")
    return f"{prefix}:{kind}:{REVIEWER}"


def _cleanup() -> None:
    r = get_redis_sync(CFG)
    r.delete(_redis_key("openq"), _redis_key("needs_you"), _redis_key("chat"))
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=PFX)


def _question_row(question_id: str, task_id: str) -> dict:
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        row = session.run(
            """
            MATCH (q:OrchQuestion {id: $id})-[:CONCERNS_TASK]->(t:OrchTask {id: $task})
            WITH q, t, properties(q) AS props
            RETURN props.status AS status, props.answered_by AS answered_by,
                   props.answer AS answer, props.question_type AS question_type,
                   props.gate_task_id AS gate_task_id, props.verified AS verified,
                   t.id AS task_id
            """,
            id=question_id,
            task=task_id,
        ).single()
    return dict(row) if row else {}


def _open_question_ids() -> set[str]:
    ids: set[str] = set()
    for raw in get_redis_sync(CFG).lrange(_redis_key("openq"), 0, -1):
        try:
            record = json.loads(raw)
        except Exception:
            continue
        if isinstance(record, dict):
            question_id = str(record.get("id") or record.get("question_id") or "").strip()
            if question_id:
                ids.add(question_id)
    return ids


def main() -> int:
    _cleanup()
    try:
        init_schema(config=CFG)
        create_project(project_id=PROJECT, name=PROJECT, supervisor=SUP, priority=1, config=CFG)
        create_phase(project_id=PROJECT, phase_id=PHASE, name="phase", config=CFG)
        create_human_review_gate(
            phase_id=PHASE,
            task_id=GATE_ONE,
            question_id=QUESTION_ONE,
            prompt="Approve deleting the production database?",
            reviewer=REVIEWER,
            requested_by=SUP,
            notify=False,
            config=CFG,
        )
        create_human_review_gate(
            phase_id=PHASE,
            task_id=GATE_TWO,
            question_id=QUESTION_TWO,
            prompt="Approve the 50k refund?",
            reviewer=REVIEWER,
            requested_by=SUP,
            notify=False,
            config=CFG,
        )
        create_task(phase_id=PHASE, task_id=DOWNSTREAM, description="blocked on human verdict", owner=SUP, wake_owner_if_ready=False, config=CFG)
        add_dependency(DOWNSTREAM, GATE_ONE, config=CFG)
        create_task(phase_id=PHASE, task_id=ORDINARY_TASK, description="ordinary informational question", owner=SUP, wake_owner_if_ready=False, config=CFG)
        create_question(
            question_id=ORDINARY_QUESTION,
            text="Which non-gate note should clear?",
            task_id=ORDINARY_TASK,
            asked_by=SUP,
            reviewer=REVIEWER,
            lineage=REVIEWER,
            surface=True,
            config=CFG,
        )
        _check("precondition has both formal gates and ordinary question open", {QUESTION_ONE, QUESTION_TWO, ORDINARY_QUESTION}.issubset(_open_question_ids()), _open_question_ids())

        reply = TestClient(app).post(
            f"/api/chat/{REVIEWER}",
            json={"sender": REVIEWER, "role": "user", "text": "looks good ship the other thing"},
            headers={"origin": "http://testserver"},
        )
        payload = reply.json() if reply.status_code == 200 else {}
        auto_answered = payload.get("auto_answered_open_questions") or {}
        _check("generic unbound reply returns ok", reply.status_code == 200, reply.text)
        _check("generic unbound reply answers ordinary non-gate question", auto_answered.get("answered_count") == 1, auto_answered)
        _check("generic unbound reply reports skipped formal gates", set(auto_answered.get("skipped_question_ids") or []) == {QUESTION_ONE, QUESTION_TWO}, auto_answered)
        _check("ordinary question stores the generic answer", _question_row(ORDINARY_QUESTION, ORDINARY_TASK).get("status") == "answered", _question_row(ORDINARY_QUESTION, ORDINARY_TASK))
        _check("first formal gate remains open", _question_row(QUESTION_ONE, GATE_ONE).get("status") == "open" and get_task(GATE_ONE, config=CFG).get("status") != "completed", _question_row(QUESTION_ONE, GATE_ONE))
        _check("second formal gate remains open", _question_row(QUESTION_TWO, GATE_TWO).get("status") == "open" and get_task(GATE_TWO, config=CFG).get("status") != "completed", _question_row(QUESTION_TWO, GATE_TWO))
        ready = get_session_next_ready(SUP, project_id=PROJECT, config=CFG)
        _check("gated downstream remains blocked", not ready or ready.get("task_id") != DOWNSTREAM, ready)
        remaining = _open_question_ids()
        _check("chat keeps both formal gates and removes ordinary question", ORDINARY_QUESTION not in remaining and {QUESTION_ONE, QUESTION_TWO}.issubset(remaining), remaining)
        needs = get_redis_sync(CFG).get(_redis_key("needs_you")) or ""
        _check("needs_you remains on a formal gate", QUESTION_ONE in needs or QUESTION_TWO in needs, needs)
    finally:
        _cleanup()

    if FAILURES:
        print(f"\nFAIL -- {len(FAILURES)}: {FAILURES}")
        return 1
    print("\nPASS -- generic unbound chat replies answer ordinary questions but never formal gates.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
