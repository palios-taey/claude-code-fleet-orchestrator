#!/usr/bin/env python3
"""Acceptance: UI resolves formal human-review questions and free-text holds."""
from __future__ import annotations

import json
import os
import re
import sys
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _require_test_namespace() -> str:
    raw = (os.environ.get("ORCH_TEST_NAMESPACE") or "").strip()
    if not raw:
        raise SystemExit("ORCH_TEST_NAMESPACE is required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{5,80}", raw):
        raise SystemExit("ORCH_TEST_NAMESPACE must be 6-80 chars of letters/digits/._:-")
    if not any(marker in raw.lower() for marker in ("test", "ci", "acceptance")):
        raise SystemExit("ORCH_TEST_NAMESPACE must include test/ci/acceptance")
    return raw


PFX = f"{_require_test_namespace()}-hrresolve-{uuid.uuid4().hex[:8]}"
SUP = f"{PFX}-sup"
PROJECT = f"{PFX}-project"
PHASE = f"{PROJECT}::phase"
QUESTION_GATE = f"{PROJECT}::formal-human-review"
QUESTION = f"{PFX}-question"
HOLD_TASK = f"{PROJECT}::await-human-review"
GENERIC_HOLD_ONE = f"{PROJECT}::generic-await-human-review-one"
GENERIC_HOLD_TWO = f"{PROJECT}::generic-await-human-review-two"
SPECIFIC_HOLD_ONE = f"{PROJECT}::specific-await-human-review-one"
SPECIFIC_HOLD_TWO = f"{PROJECT}::specific-await-human-review-two"
TERMINAL_HOLD_TASK = f"{PROJECT}::terminal-await-human-review"
GENERIC_QUESTION_TASK = f"{PROJECT}::generic-open-question-task"
GENERIC_QUESTION = f"{PFX}-generic-question"
BOUND_QUESTION_ONE_TASK = f"{PROJECT}::bound-question-one-task"
BOUND_QUESTION_TWO_TASK = f"{PROJECT}::bound-question-two-task"
BOUND_QUESTION_ONE = f"{PFX}-bound-question-one"
BOUND_QUESTION_TWO = f"{PFX}-bound-question-two"
RERAISED_QUESTION_TASK = f"{PROJECT}::reraised-open-question-task"
RERAISED_QUESTION = f"{PFX}-reraised-question"
WORKER = f"{PFX}-worker"

os.environ["NOTIFY_KEY_PREFIX"] = PFX
os.environ["ORCH_SESSION_IDS"] = SUP

from fleet_orchestrator import orch_schema  # noqa: E402
from fleet_orchestrator.config import OrchConfig, get_redis_sync  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    create_phase,
    create_project,
    create_question,
    create_task,
    get_neo4j_driver,
    get_supervisor_badges,
    get_task,
    init_schema,
    update_task_status,
)
from fleet_orchestrator.tasks_api import app  # noqa: E402


CFG = OrchConfig()
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _cleanup() -> None:
    r = get_redis_sync(CFG)
    keys = list(r.scan_iter(f"{PFX}:*"))
    if keys:
        r.delete(*keys)
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=PFX)


def _badge() -> dict:
    return get_supervisor_badges(config=CFG).get(SUP, {})


def _chat(client: TestClient) -> dict:
    response = client.get(f"/api/chat/{SUP}")
    if response.status_code != 200:
        return {"status": response.status_code, "text": response.text}
    return response.json()


def _question_ids(payload: dict) -> set[str]:
    ids: set[str] = set()
    for item in payload.get("open_questions") or []:
        question_id = str(item.get("question_id") or item.get("id") or "").strip()
        if question_id:
            ids.add(question_id)
    return ids


def _hold_items(payload: dict) -> list[dict]:
    return [
        item for item in (payload.get("open_questions") or [])
        if item.get("type") == "human_review_hold"
    ]


def _chat_messages() -> list[dict]:
    r = get_redis_sync(CFG)
    records: list[dict] = []
    for raw in r.lrange(f"{PFX}:chat:{SUP}", 0, -1):
        try:
            records.append(json.loads(raw))
        except Exception:
            records.append({"raw": raw})
    return records


def _hold_need_messages(task_id: str) -> list[dict]:
    return [
        message for message in _chat_messages()
        if (message.get("metadata") or {}).get("source") == "human_review_hold"
        and (message.get("metadata") or {}).get("task_id") == task_id
    ]


def _auto_clear_messages() -> list[dict]:
    return [
        message for message in _chat_messages()
        if (message.get("metadata") or {}).get("source") == "human_review_hold_auto_clear"
    ]


def _question(question_id: str) -> dict:
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        row = session.run(
            "MATCH (q:OrchQuestion {id: $id}) RETURN properties(q) AS props",
            id=question_id,
        ).single()
        return dict(row["props"]) if row else {}


def main() -> int:
    _cleanup()
    client = TestClient(app)
    client.__enter__()
    notify_calls: list[list[str]] = []

    def fake_notify(args, **_kwargs):
        notify_calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout="OK", stderr="")

    try:
        init_schema(config=CFG)
        create_project(project_id=PROJECT, name=PROJECT, supervisor=SUP, priority=1, config=CFG)
        create_phase(project_id=PROJECT, phase_id=PHASE, name="phase", config=CFG)

        formal_create = client.post(
            "/api/human-review-gates",
            json={
                "phase_id": PHASE,
                "task_id": QUESTION_GATE,
                "question_id": QUESTION,
                "prompt": "Approve the release?",
                "reviewer": SUP,
                "from": SUP,
                "notify": False,
            },
        )
        _check("formal human-review gate creates", formal_create.status_code == 200 and formal_create.json().get("ok"), formal_create.text)
        formal_badge = _badge()
        _check("formal question drives NEEDS-YOU badge", formal_badge.get("state") == "NEEDS-YOU" and formal_badge.get("open_question_count") == 1, formal_badge)
        formal_chat = _chat(client)
        _check("formal question appears in chat open questions", QUESTION in _question_ids(formal_chat), formal_chat)

        formal_answer = client.post(
            f"/api/ui/questions/{QUESTION}/answer",
            json={"answer": "Approved", "answered_by": "operator"},
            headers={"origin": "http://testserver"},
        )
        _check("formal question resolves through UI endpoint", formal_answer.status_code == 200 and formal_answer.json().get("gate_completed") is True, formal_answer.text)
        after_formal_badge = _badge()
        _check("formal question resolution clears badge question count", after_formal_badge.get("open_question_count") == 0 and after_formal_badge.get("state") != "NEEDS-YOU", after_formal_badge)
        after_formal_chat = _chat(client)
        _check("formal question disappears from chat open questions", QUESTION not in _question_ids(after_formal_chat), after_formal_chat)

        create_task(
            phase_id=PHASE,
            task_id=HOLD_TASK,
            description="Wait for the operator to pin the canonical model",
            owner=WORKER,
            wake_owner_if_ready=False,
            config=CFG,
        )
        update_task_status(
            HOLD_TASK,
            "in_progress",
            owner=WORKER,
            blocked_on="AWAIT:human-review:pin the canonical 35B model",
            config=CFG,
        )
        hold_badge = _badge()
        _check("free-text AWAIT human-review drives NEEDS-YOU badge", hold_badge.get("state") == "NEEDS-YOU" and hold_badge.get("human_review_hold_count") == 1, hold_badge)
        hold_chat = _chat(client)
        holds = _hold_items(hold_chat)
        _check("free-text hold appears as resolvable chat item", len(holds) == 1 and holds[0].get("task_id") == HOLD_TASK, hold_chat)
        _check("free-text hold advertises UI resolve endpoint", holds and holds[0].get("resolve_endpoint") == f"/api/ui/human-review-holds/{HOLD_TASK}/resolve", holds)
        hold_needs_you = hold_chat.get("needs_you") or {}
        _check("free-text hold sets needs_you pointer", hold_needs_you.get("source") == "human_review_hold" and hold_needs_you.get("task_id") == HOLD_TASK, hold_needs_you)
        hold_need_messages = _hold_need_messages(HOLD_TASK)
        _check("free-text hold auto-posts NEEDS-YOU chat note", len(hold_need_messages) == 1, _chat_messages())
        _check("free-text hold chat note carries task id and need", HOLD_TASK in hold_need_messages[0].get("text", "") and "canonical 35B" in hold_need_messages[0].get("text", ""), hold_need_messages)
        hold_need_metadata = hold_need_messages[0].get("metadata", {}) if hold_need_messages else {}
        _check("free-text hold chat note carries exact binding", hold_need_metadata.get("task_id") == HOLD_TASK and hold_need_metadata.get("blocked_on") == "AWAIT:human-review:pin the canonical 35B model", hold_need_metadata)
        update_task_status(
            HOLD_TASK,
            "in_progress",
            owner=WORKER,
            blocked_on="AWAIT:human-review:pin the canonical 35B model",
            config=CFG,
        )
        _check("free-text hold chat note is idempotent", len(_hold_need_messages(HOLD_TASK)) == 1, _chat_messages())

        with mock.patch.object(orch_schema.subprocess, "run", side_effect=fake_notify):
            hold_resolve = client.post(
                f"/api/ui/human-review-holds/{HOLD_TASK}/resolve",
                json={"verdict": "Use the canonical 35B model", "resolved_by": "operator"},
                headers={"origin": "http://testserver"},
            )
        _check("free-text hold resolves through UI endpoint", hold_resolve.status_code == 200 and hold_resolve.json().get("ok") is True, hold_resolve.text)
        resolved_task = get_task(HOLD_TASK, config=CFG)
        _check("free-text hold clears only blocked_on", resolved_task.get("status") == "in_progress" and not resolved_task.get("blocked_on"), resolved_task)
        _check("free-text hold stores verdict on task", "canonical 35B" in str(resolved_task.get("human_review_resolution") or ""), resolved_task)
        after_hold_badge = _badge()
        _check("free-text hold resolution drops NEEDS-YOU badge", after_hold_badge.get("human_review_hold_count") == 0 and after_hold_badge.get("state") == "ACTIVE", after_hold_badge)
        after_hold_chat = _chat(client)
        _check("free-text hold disappears from chat open questions", not _hold_items(after_hold_chat), after_hold_chat)
        _check("free-text hold clears needs_you pointer", not after_hold_chat.get("needs_you"), after_hold_chat.get("needs_you"))
        resolution_messages = [
            message for message in _chat_messages()
            if (message.get("metadata") or {}).get("source") == "human_review_hold_resolution"
        ]
        _check("supervisor chat receives the verdict", len(resolution_messages) == 1 and "canonical 35B" in resolution_messages[0].get("text", ""), resolution_messages)
        _check("supervisor notify receives the verdict", notify_calls and notify_calls[0][1] == SUP and "canonical 35B" in notify_calls[0][2], notify_calls)
        _check("supervisor notify uses operator-ui provenance", notify_calls and "--from" in notify_calls[0] and notify_calls[0][notify_calls[0].index("--from") + 1] == "operator-ui", notify_calls)

        for task_id, detail in (
            (GENERIC_HOLD_ONE, "choose the fallback model"),
            (GENERIC_HOLD_TWO, "approve the runbook"),
        ):
            create_task(
                phase_id=PHASE,
                task_id=task_id,
                description=f"Wait for the operator to {detail}",
                owner=WORKER,
                wake_owner_if_ready=False,
                config=CFG,
            )
            update_task_status(
                task_id,
                "in_progress",
                owner=WORKER,
                blocked_on=f"AWAIT:human-review:{detail}",
                config=CFG,
            )
        generic_badge = _badge()
        _check("two free-text holds drive NEEDS-YOU", generic_badge.get("state") == "NEEDS-YOU" and generic_badge.get("human_review_hold_count") == 2, generic_badge)
        generic_reply = client.post(
            f"/api/chat/{SUP}",
            json={"sender": "operator", "role": "user", "text": "I responded to the pending human-review holds."},
            headers={"origin": "http://testserver"},
        )
        generic_payload = generic_reply.json() if generic_reply.status_code == 200 else {}
        generic_cleared = generic_payload.get("auto_cleared_human_review_holds") or {}
        _check("generic user reply auto-clears all supervisor holds", generic_reply.status_code == 200 and generic_cleared.get("cleared_count") == 2, generic_reply.text)
        _check("generic reply clears first blocked_on", not get_task(GENERIC_HOLD_ONE, config=CFG).get("blocked_on"), get_task(GENERIC_HOLD_ONE, config=CFG))
        _check("generic reply clears second blocked_on", not get_task(GENERIC_HOLD_TWO, config=CFG).get("blocked_on"), get_task(GENERIC_HOLD_TWO, config=CFG))
        after_generic_badge = _badge()
        _check("generic reply drops NEEDS-YOU hold badge", after_generic_badge.get("human_review_hold_count") == 0 and after_generic_badge.get("state") == "ACTIVE", after_generic_badge)
        after_generic_chat = _chat(client)
        _check("generic reply clears hold open questions", not _hold_items(after_generic_chat), after_generic_chat)
        _check("generic reply clears needs_you pointer", not after_generic_chat.get("needs_you"), after_generic_chat.get("needs_you"))
        generic_notes = _auto_clear_messages()
        _check("generic reply posts auto-clear note", generic_notes and set(generic_notes[-1].get("metadata", {}).get("cleared_task_ids") or []) == {GENERIC_HOLD_ONE, GENERIC_HOLD_TWO}, generic_notes)

        update_task_status(
            GENERIC_HOLD_ONE,
            "in_progress",
            owner=WORKER,
            blocked_on="AWAIT:human-review:still need a narrower operator answer",
            config=CFG,
        )
        reraised_badge = _badge()
        _check("supervisor re-set re-raises NEEDS-YOU", reraised_badge.get("state") == "NEEDS-YOU" and reraised_badge.get("human_review_hold_count") == 1, reraised_badge)
        reraised_chat = _chat(client)
        _check("re-raised hold restores needs_you pointer", (reraised_chat.get("needs_you") or {}).get("task_id") == GENERIC_HOLD_ONE, reraised_chat.get("needs_you"))
        clear_reraised = client.post(
            f"/api/chat/{SUP}",
            json={"sender": "operator", "role": "user", "text": "Clearing the re-raised hold."},
            headers={"origin": "http://testserver"},
        )
        _check("generic reply clears re-raised hold", clear_reraised.status_code == 200 and not get_task(GENERIC_HOLD_ONE, config=CFG).get("blocked_on"), clear_reraised.text)

        for task_id, detail in (
            (SPECIFIC_HOLD_ONE, "choose target A"),
            (SPECIFIC_HOLD_TWO, "choose target B"),
        ):
            create_task(
                phase_id=PHASE,
                task_id=task_id,
                description=f"Wait for the operator to {detail}",
                owner=WORKER,
                wake_owner_if_ready=False,
                config=CFG,
            )
            update_task_status(
                task_id,
                "in_progress",
                owner=WORKER,
                blocked_on=f"AWAIT:human-review:{detail}",
                config=CFG,
            )
        specific_reply = client.post(
            f"/api/chat/{SUP}",
            json={
                "sender": "operator",
                "role": "user",
                "text": "Use target A.",
                "reply_to_question_id": SPECIFIC_HOLD_ONE,
            },
            headers={"origin": "http://testserver"},
        )
        specific_payload = specific_reply.json() if specific_reply.status_code == 200 else {}
        specific_cleared = specific_payload.get("auto_cleared_human_review_holds") or {}
        _check("bound hold reply auto-clears exactly one hold", specific_reply.status_code == 200 and specific_cleared.get("cleared_count") == 1, specific_reply.text)
        _check("bound hold reply clears referenced task", not get_task(SPECIFIC_HOLD_ONE, config=CFG).get("blocked_on"), get_task(SPECIFIC_HOLD_ONE, config=CFG))
        _check("bound hold reply leaves sibling hold blocked", get_task(SPECIFIC_HOLD_TWO, config=CFG).get("blocked_on") == "AWAIT:human-review:choose target B", get_task(SPECIFIC_HOLD_TWO, config=CFG))
        after_specific_badge = _badge()
        _check("bound hold reply keeps badge for sibling hold", after_specific_badge.get("state") == "NEEDS-YOU" and after_specific_badge.get("human_review_hold_count") == 1, after_specific_badge)
        after_specific_chat = _chat(client)
        _check("bound hold reply keeps needs_you on sibling", (after_specific_chat.get("needs_you") or {}).get("task_id") == SPECIFIC_HOLD_TWO, after_specific_chat.get("needs_you"))
        clear_remaining_specific = client.post(
            f"/api/chat/{SUP}",
            json={"sender": "operator", "role": "user", "text": "Clearing target B too."},
            headers={"origin": "http://testserver"},
        )
        _check("generic reply clears remaining sibling hold", clear_remaining_specific.status_code == 200 and not get_task(SPECIFIC_HOLD_TWO, config=CFG).get("blocked_on"), clear_remaining_specific.text)

        create_task(
            phase_id=PHASE,
            task_id=GENERIC_QUESTION_TASK,
            description="Wait for operator to choose the router lane",
            owner=WORKER,
            wake_owner_if_ready=False,
            config=CFG,
        )
        create_question(
            question_id=GENERIC_QUESTION,
            text="Which router lane should own this task?",
            task_id=GENERIC_QUESTION_TASK,
            asked_by=WORKER,
            reviewer=SUP,
            lineage=SUP,
            surface=True,
            config=CFG,
        )
        r = get_redis_sync(CFG)
        stale_question = json.dumps({"id": "missing-question", "text": "x", "status": "open"}, separators=(",", ":"))
        r.rpush(f"{PFX}:openq:{SUP}", stale_question)
        r.set(f"{PFX}:needs_you:{SUP}", stale_question)
        question_badge = _badge()
        _check("ordinary question plus stale notice drives NEEDS-YOU", question_badge.get("state") == "NEEDS-YOU" and question_badge.get("open_question_count", 0) >= 2, question_badge)
        generic_question_reply = client.post(
            f"/api/chat/{SUP}",
            json={"sender": "operator", "role": "user", "text": "Use the restless bandit router lane."},
            headers={"origin": "http://testserver"},
        )
        generic_question_payload = generic_question_reply.json() if generic_question_reply.status_code == 200 else {}
        generic_question_clear = generic_question_payload.get("auto_answered_open_questions") or {}
        _check(
            "generic user reply answers open question and purges junk",
            generic_question_reply.status_code == 200
            and generic_question_clear.get("answered_count") == 1
            and "missing-question" in (generic_question_clear.get("purged_question_ids") or []),
            generic_question_reply.text,
        )
        generic_question = _question(GENERIC_QUESTION)
        _check("generic reply stores answer on question", generic_question.get("status") == "answered" and "restless bandit" in generic_question.get("answer", ""), generic_question)
        after_question_badge = _badge()
        _check("generic question reply drops NEEDS-YOU question badge", after_question_badge.get("open_question_count") == 0 and after_question_badge.get("state") == "ACTIVE", after_question_badge)
        after_question_chat = _chat(client)
        _check("generic question reply clears chat open questions", GENERIC_QUESTION not in _question_ids(after_question_chat) and "missing-question" not in _question_ids(after_question_chat), after_question_chat)
        _check("generic question reply clears stale needs_you", not after_question_chat.get("needs_you"), after_question_chat.get("needs_you"))

        for task_id, question_id, prompt in (
            (BOUND_QUESTION_ONE_TASK, BOUND_QUESTION_ONE, "Choose bound option A?"),
            (BOUND_QUESTION_TWO_TASK, BOUND_QUESTION_TWO, "Choose bound option B?"),
        ):
            create_task(
                phase_id=PHASE,
                task_id=task_id,
                description=prompt,
                owner=WORKER,
                wake_owner_if_ready=False,
                config=CFG,
            )
            create_question(
                question_id=question_id,
                text=prompt,
                task_id=task_id,
                asked_by=WORKER,
                reviewer=SUP,
                lineage=SUP,
                surface=True,
                config=CFG,
            )
        bound_question_reply = client.post(
            f"/api/chat/{SUP}",
            json={
                "sender": "operator",
                "role": "user",
                "text": "Use bound option A.",
                "reply_to_question_id": BOUND_QUESTION_ONE,
            },
            headers={"origin": "http://testserver"},
        )
        bound_question_payload = bound_question_reply.json() if bound_question_reply.status_code == 200 else {}
        bound_question_clear = bound_question_payload.get("auto_answered_open_questions") or {}
        _check("bound question reply answers exactly one question", bound_question_reply.status_code == 200 and bound_question_clear.get("answered_count") == 1, bound_question_reply.text)
        _check("bound question reply answers selected question", _question(BOUND_QUESTION_ONE).get("status") == "answered", _question(BOUND_QUESTION_ONE))
        _check("bound question reply leaves sibling open", _question(BOUND_QUESTION_TWO).get("status") == "open", _question(BOUND_QUESTION_TWO))
        after_bound_question_badge = _badge()
        _check("bound question reply keeps NEEDS-YOU for sibling question", after_bound_question_badge.get("state") == "NEEDS-YOU" and after_bound_question_badge.get("open_question_count") == 1, after_bound_question_badge)
        after_bound_question_chat = _chat(client)
        bound_chat_ids = _question_ids(after_bound_question_chat)
        _check("bound question reply keeps only sibling in chat", BOUND_QUESTION_ONE not in bound_chat_ids and BOUND_QUESTION_TWO in bound_chat_ids, after_bound_question_chat)
        clear_bound_sibling = client.post(
            f"/api/chat/{SUP}",
            json={"sender": "operator", "role": "user", "text": "Use bound option B too."},
            headers={"origin": "http://testserver"},
        )
        _check("generic reply clears remaining bound sibling question", clear_bound_sibling.status_code == 200 and _question(BOUND_QUESTION_TWO).get("status") == "answered", clear_bound_sibling.text)

        create_task(
            phase_id=PHASE,
            task_id=RERAISED_QUESTION_TASK,
            description="Re-raised router question",
            owner=WORKER,
            wake_owner_if_ready=False,
            config=CFG,
        )
        create_question(
            question_id=RERAISED_QUESTION,
            text="Still need the operator after re-raise?",
            task_id=RERAISED_QUESTION_TASK,
            asked_by=WORKER,
            reviewer=SUP,
            lineage=SUP,
            surface=True,
            config=CFG,
        )
        reraised_question_badge = _badge()
        _check("supervisor re-raised question re-lights NEEDS-YOU", reraised_question_badge.get("state") == "NEEDS-YOU" and reraised_question_badge.get("open_question_count") == 1, reraised_question_badge)
        clear_reraised_question = client.post(
            f"/api/chat/{SUP}",
            json={"sender": "operator", "role": "user", "text": "No further operator decision needed."},
            headers={"origin": "http://testserver"},
        )
        _check("generic reply clears re-raised question", clear_reraised_question.status_code == 200 and _question(RERAISED_QUESTION).get("status") == "answered", clear_reraised_question.text)

        create_task(
            phase_id=PHASE,
            task_id=TERMINAL_HOLD_TASK,
            description="Wait for the operator to choose the release branch",
            owner=WORKER,
            wake_owner_if_ready=False,
            config=CFG,
        )
        update_task_status(
            TERMINAL_HOLD_TASK,
            "in_progress",
            owner=WORKER,
            blocked_on="AWAIT:human-review:choose the release branch",
            config=CFG,
        )
        terminal_hold_chat = _chat(client)
        _check("terminalized hold sets needs_you pointer", (terminal_hold_chat.get("needs_you") or {}).get("task_id") == TERMINAL_HOLD_TASK, terminal_hold_chat.get("needs_you"))
        update_task_status(
            TERMINAL_HOLD_TASK,
            "failed",
            owner=WORKER,
            completion_evidence={"reason": "terminalization cleanup acceptance"},
            completed_by=WORKER,
            config=CFG,
        )
        after_terminal_chat = _chat(client)
        _check("terminalized hold clears needs_you pointer", not after_terminal_chat.get("needs_you"), after_terminal_chat.get("needs_you"))

        app_js = (ROOT / "ui/static/app.js").read_text(encoding="utf-8")
        _check("dashboard renders free-text hold resolve control", "data-resolve-human-review-hold" in app_js, "app.js")
        _check("dashboard posts to hold resolve endpoint", "UI_HUMAN_REVIEW_HOLD_RESOLVE_ENDPOINT" in app_js, "app.js")
    finally:
        client.__exit__(None, None, None)
        _cleanup()

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)}: {FAILURES}")
        return 1
    print("\nPASS - UI resolves formal questions and free-text AWAIT:human-review holds mechanically.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
