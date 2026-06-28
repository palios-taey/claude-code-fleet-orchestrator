"""Ship-gate e2e — human-review gate surfaces and requires the UI path.

The durable Neo4j question and dashboard needs-you surface must be one gate:
creating a human-review gate writes both. A peer/loopback caller may record an
unverified answer, but normal agent completion paths must not complete the gate
task; the dashboard UI path records the verdict and releases dependents.
"""
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
    CompletionEvidenceError,
    add_dependency,
    create_phase,
    create_project,
    create_task,
    get_neo4j_driver,
    get_session_next_ready,
    init_schema,
    get_task,
    _surface_question_to_chat,
    update_task_status,
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
GATE_TWO = f"{PROJECT}::human-review-two"
QUESTION_TWO = f"{PFX}-question-two"
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


def _question_row(question_id: str = QUESTION, task_id: str = GATE) -> dict:
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        row = session.run(
            """
            MATCH (q:OrchQuestion {id: $id})-[:CONCERNS_TASK]->(t:OrchTask {id: $task})
            WITH q, t, properties(q) AS props
            RETURN props.status AS status, props.answered_by AS answered_by, props.answer AS answer,
                   props.question_type AS question_type, props.gate_task_id AS gate_task_id,
                   props.verified AS verified,
                   props.unverified_answer AS unverified_answer,
                   props.unverified_answered_by AS unverified_answered_by,
                   t.id AS task_id
            """,
            id=question_id,
            task=task_id,
        ).single()
    return dict(row) if row else {}


def _chat_messages() -> list[dict]:
    records: list[dict] = []
    for raw in get_redis_sync(CFG).lrange(_redis_key("chat"), 0, -1):
        try:
            records.append(json.loads(raw))
        except Exception:
            records.append({"raw": raw})
    return records


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


def _chat_messages_for_question(question_id: str) -> list[dict]:
    matches: list[dict] = []
    for message in _chat_messages():
        metadata = message.get("metadata") if isinstance(message.get("metadata"), dict) else {}
        if question_id in {
            str(metadata.get("open_question_id") or ""),
            str(metadata.get("question_id") or ""),
            str(metadata.get("reply_to_question_id") or ""),
        }:
            matches.append(message)
    return matches


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
        _check("dashboard open_questions contains durable question", QUESTION in _open_question_ids(), r.lrange(_redis_key("openq"), 0, -1))
        gate_messages = _chat_messages_for_question(QUESTION)
        _check("NEEDS-YOU chat message auto-posted once", len(gate_messages) == 1, _chat_messages())
        gate_metadata = gate_messages[0].get("metadata", {}) if gate_messages else {}
        _check(
            "NEEDS-YOU chat message carries exact gate binding",
            gate_metadata.get("question_id") == QUESTION
            and gate_metadata.get("reply_to_question_id") == QUESTION
            and gate_metadata.get("gate_task_id") == GATE
            and gate_metadata.get("reply_endpoint") == f"/api/ui/questions/{QUESTION}/answer",
            gate_metadata,
        )
        _surface_question_to_chat(
            {
                "id": QUESTION,
                "text": "Which artifact should ship?",
                "task_id": GATE,
                "gate_task_id": GATE,
                "reviewer": REVIEWER,
                "lineage": REVIEWER,
            },
            config=CFG,
        )
        _check("NEEDS-YOU chat message dedupes per gate", len(_chat_messages_for_question(QUESTION)) == 1, _chat_messages())

        create_task(phase_id=PHASE, task_id=DOWNSTREAM, description="released after human verdict", owner=SUP, wake_owner_if_ready=False, config=CFG)
        add_dependency(DOWNSTREAM, GATE, config=CFG)
        before = get_session_next_ready(SUP, project_id=PROJECT, config=CFG)
        _check("downstream blocked before human answer", not before or before.get("task_id") != DOWNSTREAM, before)

        terminal_attempts = {
            "completed": {"production_observation": "agent normal completion attempt"},
            "failed": {"reason": "agent attempted to bypass the human-review gate"},
            "interrupted": {"reason": "agent attempted to interrupt the human-review gate"},
        }
        for status, evidence in terminal_attempts.items():
            try:
                update_task_status(
                    GATE,
                    status,
                    owner=SUP,
                    completion_evidence=evidence,
                    completed_by=SUP,
                    config=CFG,
                )
                direct_error = ""
            except CompletionEvidenceError as exc:
                direct_error = str(exc)
            _check(
                f"direct update_task_status rejects human-review {status}",
                "dashboard UI review endpoint" in direct_error and "terminal status" in direct_error,
                direct_error or get_task(GATE, config=CFG),
            )
            _check(
                f"direct update_task_status leaves gate open after {status}",
                get_task(GATE, config=CFG).get("status") not in terminal_attempts,
                get_task(GATE, config=CFG),
            )

        for status, evidence in terminal_attempts.items():
            patch_response = client.patch(
                f"/api/task/{GATE}",
                json={
                    "status": status,
                    "from": SUP,
                    "evidence": evidence,
                },
            )
            patch_error = patch_response.text
            try:
                patch_error = patch_response.json().get("error") or patch_error
            except Exception:
                pass
            _check(
                f"PATCH /api/task rejects human-review {status}",
                patch_response.status_code == 400
                and "dashboard UI review endpoint" in patch_error
                and "terminal status" in patch_error,
                patch_response.text,
            )
            _check(
                f"PATCH / taey-task path leaves gate open after {status}",
                get_task(GATE, config=CFG).get("status") not in terminal_attempts,
                get_task(GATE, config=CFG),
            )

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
        _check("dashboard open question remains for real human", QUESTION in _open_question_ids(), r.lrange(_redis_key("openq"), 0, -1))
        after = get_session_next_ready(SUP, project_id=PROJECT, config=CFG)
        _check("downstream remains blocked after normal answer endpoint", not after or after.get("task_id") != DOWNSTREAM, after)

        create_response_two = client.post(
            "/api/human-review-gates",
            json={
                "phase_id": PHASE,
                "task_id": GATE_TWO,
                "question_id": QUESTION_TWO,
                "prompt": "Should artifact C ship?",
                "reviewer": REVIEWER,
                "from": SUP,
                "notify": False,
            },
        )
        _check("second human-review gate endpoint returns ok", create_response_two.status_code == 200 and create_response_two.json().get("ok"), create_response_two.text)
        _check("second NEEDS-YOU chat message auto-posted once", len(_chat_messages_for_question(QUESTION_TWO)) == 1, _chat_messages())

        chat_reply = client.post(
            f"/api/chat/{REVIEWER}",
            json={
                "answer": "ignored alias",
                "sender": REVIEWER,
                "role": "user",
                "text": "Ship artifact B",
                "reply_to_question_id": QUESTION,
            },
            headers={"origin": "http://testserver"},
        )
        chat_payload = chat_reply.json() if chat_reply.status_code == 200 else {}
        bound_reply = chat_payload.get("bound_gate_reply") or {}
        _check("UI chat bound reply records verified answer", chat_reply.status_code == 200 and bound_reply.get("verified") is True, chat_reply.text)
        _check("UI chat bound reply completes exact gate", bound_reply.get("gate_completed") is True and bound_reply.get("gate_task_id") == GATE, chat_payload)
        _check("UI chat reply message carries reply binding", (chat_payload.get("message", {}).get("metadata") or {}).get("reply_to_question_id") == QUESTION, chat_payload)
        final_question = _question_row()
        _check("UI chat answer closes durable question as verified", final_question.get("status") == "answered" and final_question.get("verified") is True, final_question)
        final_gate = get_task(GATE, config=CFG)
        _check("UI chat answer completes human-review gate task", final_gate.get("status") == "completed", final_gate)
        final_ready = get_session_next_ready(SUP, project_id=PROJECT, config=CFG)
        _check("downstream releases after chat-bound UI verdict", final_ready and final_ready.get("task_id") == DOWNSTREAM, final_ready)
        _check("bound chat reply leaves other gate open", _question_row(QUESTION_TWO, GATE_TWO).get("status") == "open" and get_task(GATE_TWO, config=CFG).get("status") != "completed", _question_row(QUESTION_TWO, GATE_TWO))
        _check("dashboard needs_you remains for unanswered gate", QUESTION_TWO in (r.get(_redis_key("needs_you")) or ""), r.get(_redis_key("needs_you")))
        _check("dashboard open question clears answered gate", QUESTION not in _open_question_ids(), r.lrange(_redis_key("openq"), 0, -1))
        _check("dashboard open question retains unanswered gate", QUESTION_TWO in _open_question_ids(), r.lrange(_redis_key("openq"), 0, -1))

        ui_response = client.post(
            f"/api/ui/questions/{QUESTION_TWO}/answer",
            json={"answer": "Hold artifact C", "answered_by": REVIEWER},
            headers={"origin": "http://testserver"},
        )
        _check("open-question UI endpoint still completes remaining gate", ui_response.status_code == 200 and ui_response.json().get("gate_completed") is True, ui_response.text)
        _check("dashboard needs_you clears after all UI verdicts", not r.get(_redis_key("needs_you")), r.get(_redis_key("needs_you")))
        _check("dashboard open questions clear after all UI verdicts", not r.lrange(_redis_key("openq"), 0, -1), r.lrange(_redis_key("openq"), 0, -1))
    finally:
        _cleanup()

    if FAILURES:
        print(f"\nFAIL — {len(FAILURES)}: {FAILURES}")
        return 1
    print("\nPASS — human-review gate surfaces, agent paths cannot complete it, and the UI path can.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
