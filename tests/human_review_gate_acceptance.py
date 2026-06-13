"""Ship-gate e2e — human-review gate surfaces and rejects forged completion.

The durable Neo4j question and dashboard needs-you surface must be one gate:
creating a human-review gate writes both. A peer/loopback caller may record an
unverified answer, but it must not forge the human reviewer or auto-complete
the gate task.
"""
from __future__ import annotations

import os
import re
import sys
import uuid

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet_orchestrator.config import OrchConfig, get_redis_sync  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    add_dependency,
    create_phase,
    create_project,
    create_task,
    get_neo4j_driver,
    get_session_next_ready,
    init_schema,
    get_task,
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
PFX = f"{_require_test_namespace()}-hrg-{uuid.uuid4().hex[:8]}"
SUP = f"{PFX}-sup"
REVIEWER = f"{PFX}-human"
PROJECT = f"{PFX}-project"
PHASE = f"{PROJECT}::phase"
GATE = f"{PROJECT}::human-review"
QUESTION = f"{PFX}-question"
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


def _question_row() -> dict:
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        row = session.run(
            """
            MATCH (q:OrchQuestion {id: $id})-[:CONCERNS_TASK]->(t:OrchTask {id: $task})
            WITH q, t, properties(q) AS props
            RETURN props.status AS status, props.answered_by AS answered_by, props.answer AS answer,
                   props.question_type AS question_type, props.gate_task_id AS gate_task_id,
                   props.unverified_answer AS unverified_answer,
                   props.unverified_answered_by AS unverified_answered_by,
                   t.id AS task_id
            """,
            id=QUESTION,
            task=GATE,
        ).single()
    return dict(row) if row else {}


def main() -> int:
    _cleanup()
    client = TestClient(app)
    try:
        init_schema(config=CFG)
        create_project(project_id=PROJECT, name=PROJECT, supervisor=SUP, priority=1, config=CFG)
        create_phase(project_id=PROJECT, phase_id=PHASE, name="phase", config=CFG)

        create_response = client.post(
            "/api/human-review-gates",
            json={
                "phase_id": PHASE,
                "task_id": GATE,
                "question_id": QUESTION,
                "prompt": "Which artifact should ship?",
                "reviewer": REVIEWER,
                "from": SUP,
                "notify": False,
            },
        )
        _check("create human-review gate endpoint returns ok", create_response.status_code == 200 and create_response.json().get("ok"), create_response.text)
        _check("durable question linked to gate task", _question_row().get("gate_task_id") == GATE, _question_row())

        r = get_redis_sync(CFG)
        _check("dashboard needs_you points at durable question", QUESTION in (r.get(_redis_key("needs_you")) or ""), r.get(_redis_key("needs_you")))
        _check("dashboard open_questions contains durable question", any(QUESTION in item for item in r.lrange(_redis_key("openq"), 0, -1)), r.lrange(_redis_key("openq"), 0, -1))

        create_task(phase_id=PHASE, task_id=DOWNSTREAM, description="released after human verdict", owner=SUP, wake_owner_if_ready=False, config=CFG)
        add_dependency(DOWNSTREAM, GATE, config=CFG)
        before = get_session_next_ready(SUP, project_id=PROJECT, config=CFG)
        _check("downstream blocked before human answer", not before or before.get("task_id") != DOWNSTREAM, before)

        answer_response = client.post(
            f"/api/questions/{QUESTION}/answer",
            json={"answer": "Ship artifact B", "from": REVIEWER},
        )
        _check("peer answer endpoint records but does not verify", answer_response.status_code == 200 and answer_response.json().get("ok"), answer_response.text)
        _check("peer answer cannot auto-complete human gate", answer_response.json().get("gate_completed") is False and answer_response.json().get("verified") is False, answer_response.json())
        qrow = _question_row()
        _check("peer answer is stored as unverified and question stays open", qrow.get("status") == "open" and qrow.get("unverified_answered_by") == REVIEWER, qrow)
        gate = get_task(GATE, config=CFG)
        evidence = gate.get("completion_evidence") or {}
        _check("peer answer leaves gate task blocked", gate.get("status") != "completed", gate)
        _check("peer answer writes no completion evidence", not evidence, evidence)
        _check("dashboard needs_you remains for real human", QUESTION in (r.get(_redis_key("needs_you")) or ""), r.get(_redis_key("needs_you")))
        _check("dashboard open question remains for real human", any(QUESTION in item for item in r.lrange(_redis_key("openq"), 0, -1)), r.lrange(_redis_key("openq"), 0, -1))
        after = get_session_next_ready(SUP, project_id=PROJECT, config=CFG)
        _check("downstream remains blocked after forged verdict", not after or after.get("task_id") != DOWNSTREAM, after)
    finally:
        _cleanup()

    if FAILURES:
        print(f"\nFAIL — {len(FAILURES)}: {FAILURES}")
        return 1
    print("\nPASS — human-review gate surfaces and unauthenticated answers cannot forge completion.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
