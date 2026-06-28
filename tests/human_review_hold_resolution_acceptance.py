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

os.environ["NOTIFY_KEY_PREFIX"] = PFX
os.environ["ORCH_SESSION_IDS"] = SUP

from fleet_orchestrator import orch_schema  # noqa: E402
from fleet_orchestrator.config import OrchConfig, get_redis_sync  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    create_phase,
    create_project,
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
            owner=SUP,
            wake_owner_if_ready=False,
            config=CFG,
        )
        update_task_status(
            HOLD_TASK,
            "in_progress",
            owner=SUP,
            blocked_on="AWAIT:human-review:pin the canonical 35B model",
            config=CFG,
        )
        hold_badge = _badge()
        _check("free-text AWAIT human-review drives NEEDS-YOU badge", hold_badge.get("state") == "NEEDS-YOU" and hold_badge.get("human_review_hold_count") == 1, hold_badge)
        hold_chat = _chat(client)
        holds = _hold_items(hold_chat)
        _check("free-text hold appears as resolvable chat item", len(holds) == 1 and holds[0].get("task_id") == HOLD_TASK, hold_chat)
        _check("free-text hold advertises UI resolve endpoint", holds and holds[0].get("resolve_endpoint") == f"/api/ui/human-review-holds/{HOLD_TASK}/resolve", holds)

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
        resolution_messages = [
            message for message in _chat_messages()
            if (message.get("metadata") or {}).get("source") == "human_review_hold_resolution"
        ]
        _check("supervisor chat receives the verdict", len(resolution_messages) == 1 and "canonical 35B" in resolution_messages[0].get("text", ""), resolution_messages)
        _check("supervisor notify receives the verdict", notify_calls and notify_calls[0][1] == SUP and "canonical 35B" in notify_calls[0][2], notify_calls)
        _check("supervisor notify uses operator-ui provenance", notify_calls and "--from" in notify_calls[0] and notify_calls[0][notify_calls[0].index("--from") + 1] == "operator-ui", notify_calls)

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
