#!/usr/bin/env python3
"""Acceptance: audited AI-native gap fixes teach the real next step in-band."""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import fleet_orchestrator.orch_schema as orch_schema  # noqa: E402
import fleet_orchestrator.chat_layer as chat_layer  # noqa: E402
import fleet_orchestrator.tasks_api as tasks_api  # noqa: E402

FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


class _MemoryRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def get(self, key: str):
        return self.values.get(key)

    def set(self, key: str, value: str, *_args) -> bool:
        self.values[key] = str(value)
        return True

    def delete(self, *keys: str) -> int:
        removed = 0
        for key in keys:
            removed += int(key in self.values)
            self.values.pop(key, None)
        return removed


class _NoRecordResult:
    def single(self):
        return None


class _NoRecordSession:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def run(self, *_args, **_kwargs):
        return _NoRecordResult()


class _NoRecordDriver:
    def session(self, **_kwargs):
        return _NoRecordSession()


class _SingleRecordResult:
    def __init__(self, record=None) -> None:
        self.record = record

    def single(self):
        return self.record


class _HumanReviewAnswerSession:
    def __init__(self) -> None:
        self.calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def run(self, *_args, **_kwargs):
        self.calls += 1
        if self.calls == 1:
            return _SingleRecordResult({
                "id": "q1",
                "question_type": "human_review_gate",
                "gate_task_id": "task-1",
                "lineage": "session-1",
                "reviewer": "reviewer-1",
                "props": {},
            })
        return _SingleRecordResult()


class _HumanReviewAnswerDriver:
    def session(self, **_kwargs):
        return _HumanReviewAnswerSession()


def _client() -> TestClient:
    return TestClient(tasks_api.app, raise_server_exceptions=False)


def _detail(response) -> str:
    try:
        body = response.json()
    except json.JSONDecodeError:
        return response.text
    return str(body.get("detail") or body.get("error") or body)


def _assert_detail(label: str, response, expected_status: int, needles: list[str]) -> None:
    detail = _detail(response)
    _check(f"{label} status", response.status_code == expected_status, response.text)
    for needle in needles:
        _check(f"{label} names {needle}", needle in detail, detail)


def _stop_decision_contract() -> None:
    reason = orch_schema._queue_block_reason("task-123", "do the work")
    _check("queue stop reason names real cli", "`taey-plan next`" in reason, reason)
    _check("queue stop reason does not name removed helper", "taey-queue" not in reason, reason)

    redis = _MemoryRedis()
    base_decision = {
        "block": True,
        "reason": "Finish in-progress task task-123: do the work.",
        "wake_type": "WAKE_WITH_QUEUE",
        "task_id": "task-123",
    }
    cfg = SimpleNamespace()
    with mock.patch.object(orch_schema, "_fleet_state_redis", return_value=redis), \
         mock.patch.object(orch_schema, "_raw_stop_decision", return_value=dict(base_decision)):
        decisions = [
            orch_schema.get_session_stop_decision("session-1-codex", stop_hook_active=True, config=cfg)
            for _ in range(3)
        ]
    converged = decisions[-1]
    _check("convergence still force-allows", converged.get("block") is False and converged.get("converged_allow") is True, decisions)
    _check("convergence teaches why it released", "Original block reason" in str(converged.get("converged_reason") or ""), converged)
    _check("convergence preserves original reason", converged.get("converged_original", {}).get("reason") == base_decision["reason"], converged)


def _stop_condition_contract(client: TestClient) -> None:
    cfg = SimpleNamespace(neo4j_db="neo4j")
    with mock.patch.object(orch_schema, "get_neo4j_driver", return_value=_NoRecordDriver()), \
         mock.patch.object(tasks_api, "_cfg", return_value=cfg):
        response = client.post("/api/projects/missing/user-stop-conditions", json={"conditions": ["done"]})
    _assert_detail("missing project stop-conditions write", response, 404, ["taey-plan list", "GET /api/projects"])

    active_conditions = [
        {
            "id": "cond-active",
            "label": "release only after r5",
            "version": 2,
            "created_at": "2026-06-19T00:00:00Z",
            "created_by": "session-1",
            "deprecated_at": None,
            "replaces_id": None,
        }
    ]
    project = {"id": "proj-1", "user_stop_conditions": active_conditions}
    with mock.patch.object(orch_schema, "_project_record", return_value=project), \
         mock.patch.object(tasks_api, "_cfg", return_value=cfg):
        response = client.patch("/api/projects/proj-1/conditions/missing", json={"label": "replacement"})
    _assert_detail(
        "missing active condition edit",
        response,
        400,
        ["cond-active v2", "taey-plan stop-conditions proj-1 get", "GET /api/projects/proj-1"],
    )


def _api_auth_contract(client: TestClient) -> None:
    old_token = os.environ.get("ORCH_AUTH_TOKEN")
    os.environ["ORCH_AUTH_TOKEN"] = "secret-token"
    try:
        response = client.post("/api/task/create", json={"description": "blocked by auth"})
    finally:
        if old_token is None:
            os.environ.pop("ORCH_AUTH_TOKEN", None)
        else:
            os.environ["ORCH_AUTH_TOKEN"] = old_token
    _assert_detail("mutable auth failure", response, 401, ["Authorization: Bearer", "X-API-Key", "loopback"])


def _api_required_field_contract(client: TestClient) -> None:
    cases = [
        ("task create description", client.post("/api/task/create", json={}), 400, ["Minimal accepted JSON body", "POST /api/task/create", "taey-task"]),
        ("human gate phase_id", client.post("/api/human-review-gates", json={}), 422, ["phase_id", "task_id", "prompt", "POST /api/human-review-gates"]),
        ("human gate task_id", client.post("/api/human-review-gates", json={"phase_id": "phase"}), 422, ["task_id", "prompt", "POST /api/human-review-gates"]),
        ("human gate prompt", client.post("/api/human-review-gates", json={"phase_id": "phase", "task_id": "task"}), 422, ["prompt", "POST /api/human-review-gates"]),
        ("question answer", client.post("/api/questions/q1/answer", json={}), 422, ["answer", "POST /api/questions/q1/answer"]),
        ("project id", client.post("/api/projects", json={}), 400, ["id", "supervisor", "POST /api/projects", "taey-plan ingest"]),
        ("phase id", client.post("/api/projects/p1/phases", json={}), 400, ["id", "POST /api/projects/p1/phases"]),
        ("plan md_text", client.post("/api/projects/load-md", json={}), 400, ["md_text", "POST /api/projects/load-md", "taey-plan ingest"]),
        ("project priority", client.patch("/api/projects/p1", json={}), 400, ["priority", "PATCH /api/projects/p1"]),
        ("condition label add", client.post("/api/projects/p1/conditions", json={}), 400, ["label", "POST /api/projects/p1/conditions"]),
        ("condition label edit", client.patch("/api/projects/p1/conditions/c1", json={}), 400, ["label", "PATCH /api/projects/p1/conditions/c1", "taey-plan stop-conditions p1 get"]),
        ("notify target", client.post("/api/sessions/%20/notify", json={}), 400, ["target", "POST /api/sessions/{target}/notify"]),
        ("loop advance step", client.post("/api/loops/l1/advance", json={}), 400, ["step", "POST /api/loops/l1/advance"]),
        ("wake packet session_id", client.get("/api/sessions/%20/wake-packet?cli=codex"), 400, ["session_id", "GET /api/sessions/{session_id}/wake-packet"]),
    ]
    with mock.patch.object(tasks_api, "_origin_allowed_for_ui", return_value=True):
        cases.append(
            ("ui answer", client.post("/api/ui/questions/q1/answer", json={}), 422, ["answer", "POST /api/ui/questions/q1/answer"])
        )
    with mock.patch.object(tasks_api, "_ensure_registered_session", return_value=None):
        cases.append(
        ("notify message", client.post("/api/sessions/session-1/notify", json={}), 400, ["message", "POST /api/sessions/session-1/notify", "taey-notify"])
        )
    for label, response, status, needles in cases:
        _assert_detail(label, response, status, needles)


def _api_not_found_contract(client: TestClient) -> None:
    cfg = SimpleNamespace()
    with mock.patch.object(tasks_api, "_cfg", return_value=cfg), \
         mock.patch.object(tasks_api, "resolve_task_id", return_value="task-missing"), \
         mock.patch.object(tasks_api, "load_task_record", return_value=None):
        response = client.get("/api/tasks/task-missing")
    _assert_detail(
        "missing task",
        response,
        404,
        ["taey-task list", "GET /api/tasks", "taey-task status task-missing", "taey-plan show <project-id>"],
    )

    with mock.patch.object(tasks_api, "_cfg", return_value=cfg), \
         mock.patch.object(tasks_api, "get_project_summary", return_value=None):
        response = client.get("/api/projects/proj-missing")
    _assert_detail("missing project", response, 404, ["taey-plan list", "GET /api/projects", "taey-plan show proj-missing"])

    with mock.patch.object(tasks_api, "_cfg", return_value=cfg), \
         mock.patch.object(tasks_api, "answer_question", return_value={"ok": False}):
        response = client.post("/api/questions/q-missing/answer", json={"answer": "done"})
    _assert_detail(
        "missing question",
        response,
        404,
        ["GET /api/projects", "taey-plan show <project-id>", "POST /api/questions/q-missing/answer"],
    )

    with mock.patch.object(tasks_api, "_cfg", return_value=cfg), \
         mock.patch.object(tasks_api, "_origin_allowed_for_ui", return_value=True), \
         mock.patch.object(tasks_api, "complete_human_review_gate", return_value={"ok": False}):
        response = client.post("/api/ui/questions/q-missing/answer", json={"answer": "done"})
    _assert_detail("missing ui question", response, 404, ["/ui/", "POST /api/ui/questions/q-missing/answer"])


def _wake_packet_no_next_step_contract(client: TestClient) -> None:
    old_wake = os.environ.get("ORCH_WAKE_PACKET_ENDPOINT_ENABLED")
    try:
        os.environ["ORCH_WAKE_PACKET_ENDPOINT_ENABLED"] = "0"
        with mock.patch.object(tasks_api, "select_wake_context", side_effect=RuntimeError("should not assemble")):
            disabled = client.get("/api/sessions/session-1-codex/wake-packet?cli=codex")
        disabled_body = disabled.json()
        _check(
            "disabled wake packet names enable flag",
            disabled.status_code == 200
            and disabled_body.get("ok") is True
            and disabled_body.get("enabled") is False
            and disabled_body.get("reason") == "wake packet endpoint disabled"
            and disabled_body.get("enable_with") == "ORCH_WAKE_PACKET_ENDPOINT_ENABLED=1"
            and "GET /api/sessions/{session_id}/wake-packet" in disabled_body.get("next_step", ""),
            disabled_body,
        )

        os.environ["ORCH_WAKE_PACKET_ENDPOINT_ENABLED"] = "1"
        with mock.patch.object(tasks_api, "_cfg", return_value=SimpleNamespace(session_ids=["session-1-codex"])), \
             mock.patch.object(tasks_api, "select_wake_context", side_effect=RuntimeError("assembler boom")):
            failed = client.get("/api/sessions/session-1-codex/wake-packet?cli=codex")
        failed_body = failed.json()
        _check(
            "wake assembler failure names operation and next step",
            failed.status_code == 200
            and failed_body.get("ok") is False
            and failed_body.get("operation") == "wake_packet_assembly"
            and "assembler boom" in failed_body.get("error", "")
            and "Wake continues without a packet" in failed_body.get("next_step", ""),
            failed_body,
        )
    finally:
        if old_wake is None:
            os.environ.pop("ORCH_WAKE_PACKET_ENDPOINT_ENABLED", None)
        else:
            os.environ["ORCH_WAKE_PACKET_ENDPOINT_ENABLED"] = old_wake


def _health_failure_contract(client: TestClient) -> None:
    with mock.patch.object(tasks_api, "get_ready_tasks", side_effect=RuntimeError("neo4j down")):
        response = client.get("/health")
    body = response.json()
    _check(
        "health failure names dependency and diagnostic",
        response.status_code == 503
        and body.get("ok") is False
        and body.get("dependency") == "neo4j"
        and body.get("operation") == "get_ready_tasks"
        and "ORCH_NEO4J_URI" in body.get("next_step", "")
        and "GET /health" in body.get("next_step", ""),
        body,
    )


def _partial_surface_contracts(client: TestClient) -> None:
    direct_cases = [
        (
            "completed evidence object",
            lambda: orch_schema._normalize_completion_evidence("bad"),
            ["taey-task update <task-id> completed", "PATCH /api/task/<task-id>", "commit_sha"],
        ),
        (
            "completed missing evidence",
            lambda: orch_schema._validate_terminal_status_write("completed", None),
            ["taey-task update <task-id> completed", "production_observation"],
        ),
        (
            "failed evidence next step",
            lambda: orch_schema._normalize_non_success_terminal_evidence("failed", None),
            ["taey-task update <task-id> failed", '{"reason":"<why>"}'],
        ),
        (
            "chat lineage pattern",
            lambda: chat_layer._normalize_lineage("bad/lineage"),
            ["[A-Za-z0-9._-]+", "POST /api/chat/<lineage>", '"role":"user"'],
        ),
    ]
    for label, fn, needles in direct_cases:
        try:
            fn()
        except (orch_schema.CompletionEvidenceError, ValueError) as exc:
            detail = str(exc)
        else:
            detail = ""
        for needle in needles:
            _check(f"{label} includes {needle}", needle in detail, detail)

    in_progress = orch_schema._in_progress_block_reason("task-123", "ship the fix")
    _check("in-progress reason names record_outcome", "record_outcome('<session>', 'done'" in in_progress, in_progress)
    _check("in-progress reason names taey-task evidence", "taey-task update task-123 completed" in in_progress, in_progress)

    human_wait = orch_schema._awaiting_human_review_stop_reason([
        {"task_id": "task-1", "question_id": "q1", "reviewer": "reviewer-1"}
    ])
    _check("human review stop names ui answer endpoint", "POST /api/ui/questions/q1/answer" in human_wait, human_wait)

    with mock.patch.object(orch_schema, "get_neo4j_driver", return_value=_HumanReviewAnswerDriver()):
        answer = orch_schema.answer_question("q1", "looks good", "session-1-codex", config=SimpleNamespace(neo4j_db="neo4j"))
    _check("human-review API answer teaches UI verification", "POST /api/ui/questions/q1/answer" in answer.get("next_step", ""), answer)

    marker_decision = {"block": True, "reason": "still blocked", "wake_type": "WAKE_WITH_QUEUE", "task_id": "task-1"}
    with mock.patch.object(orch_schema, "_raw_stop_decision", return_value=dict(marker_decision)), \
         mock.patch.object(orch_schema, "_fleet_state_redis", side_effect=RuntimeError("redis down")):
        marker = orch_schema.get_session_stop_decision("session-1-codex", stop_hook_active=True, config=SimpleNamespace())
    marker_detail = str(marker.get("convergence_marker_fail_open") or {})
    _check("marker failure explains redis repeat", "Redis convergence marker" in marker_detail and "retry GET" in marker_detail, marker)

    cfg = SimpleNamespace()
    ready_task = {"id": "task-1", "description": "ready"}
    with mock.patch.object(orch_schema, "get_session_supervised_projects", return_value=[{"id": "proj-1", "status": "active", "priority": 1}]), \
         mock.patch.object(orch_schema, "ready_work", return_value=[ready_task]):
        status = orch_schema.get_session_stop_status("session-1-codex", config=cfg)
    _check("stop-status ready work names taey-plan next", "taey-plan next session-1-codex" in status["decision"].get("next_action", ""), status)

    with mock.patch.object(tasks_api, "_cfg", return_value=cfg), \
         mock.patch.object(tasks_api, "resolve_task_id", return_value="task-1"), \
         mock.patch.object(tasks_api, "_load_task", return_value={"owner": "session-1-codex", "description": "demo"}), \
         mock.patch("fleet_orchestrator.completion_guard.peer_self_completion_rejection", return_value=None), \
         mock.patch.object(tasks_api, "update_task_status", side_effect=orch_schema.CompletionEvidenceError("missing evidence")):
        response = client.patch("/api/task/task-1", json={"status": "completed", "from": "session-1-codex"})
    body = response.json()
    _check("task update evidence has next_step", response.status_code == 400 and "next_step" in body, body)
    _check("task update next_step names command", "taey-task update task-1 completed" in str(body), body)

    _assert_detail(
        "project complete object body",
        client.post("/api/projects/proj-1/complete", json=[]),
        422,
        ["force", "closure_reason", "completed_by"],
    )
    _assert_detail(
        "project force type body",
        client.post("/api/projects/proj-1/complete", json={"force": "yes"}),
        422,
        ["JSON boolean", "closure_reason"],
    )

    with mock.patch.object(tasks_api, "_cfg", return_value=cfg), \
         mock.patch.object(tasks_api, "get_session_liveness", return_value={"status": "idle"}), \
         mock.patch.object(tasks_api, "get_session_current_work", return_value=None):
        current = client.get("/api/sessions/session-1-codex/current")
    _check("empty current has next_action", "taey-plan next session-1-codex" in str(current.json()), current.text)

    with mock.patch.object(tasks_api, "_cfg", return_value=cfg), \
         mock.patch.object(tasks_api, "get_session_next_ready", return_value=None):
        next_ready = client.get("/api/sessions/session-1-codex/next-ready")
    _check("empty next-ready has next_action", "stop-status" in str(next_ready.json()), next_ready.text)

    _assert_detail(
        "notify type gives command",
        client.post("/api/sessions/session-1/notify", json={"message": "hello", "type": "bad"}),
        400,
        ["taey-notify session-1", "response_ready"],
    )
    with mock.patch.object(tasks_api, "_cfg", return_value=cfg), \
         mock.patch.object(tasks_api, "_ensure_registered_session", return_value=None), \
         mock.patch.object(tasks_api.subprocess, "run", return_value=SimpleNamespace(returncode=1, stderr="notify exploded")):
        notify = client.post("/api/sessions/session-1/notify", json={"message": "hello", "type": "standard"})
    _check("notify failure preserves stderr", notify.status_code == 502 and "notify_stderr" in str(notify.json()), notify.text)

    with mock.patch.object(tasks_api, "_cfg", return_value=cfg):
        loop = client.post("/api/loops/declare", json={})
    _check(
        "loop declaration has required fields",
        loop.status_code == 400
        and all(token in str(loop.json()) for token in ["step_bundle", "trigger", "cycle_state", "stop_condition"]),
        loop.text,
    )

    chat = client.post("/api/chat/session-1-codex", json={"text": "hello", "role": "system"})
    _check("chat role error names allowed body", chat.status_code == 400 and "POST /api/chat/<lineage>" in str(chat.json()), chat.text)


def main() -> int:
    old_auth = os.environ.get("ORCH_AUTH_TOKEN")
    old_loops = os.environ.get("ORCH_LOOPS_ENABLED")
    old_wake = os.environ.get("ORCH_WAKE_PACKET_ENDPOINT_ENABLED")
    os.environ.pop("ORCH_AUTH_TOKEN", None)
    os.environ["ORCH_LOOPS_ENABLED"] = "1"
    os.environ["ORCH_WAKE_PACKET_ENDPOINT_ENABLED"] = "1"
    try:
        client = _client()
        _stop_decision_contract()
        _stop_condition_contract(client)
        _api_auth_contract(client)
        _api_required_field_contract(client)
        _api_not_found_contract(client)
        _wake_packet_no_next_step_contract(client)
        _health_failure_contract(client)
        _partial_surface_contracts(client)
    finally:
        if old_auth is None:
            os.environ.pop("ORCH_AUTH_TOKEN", None)
        else:
            os.environ["ORCH_AUTH_TOKEN"] = old_auth
        if old_loops is None:
            os.environ.pop("ORCH_LOOPS_ENABLED", None)
        else:
            os.environ["ORCH_LOOPS_ENABLED"] = old_loops
        if old_wake is None:
            os.environ.pop("ORCH_WAKE_PACKET_ENDPOINT_ENABLED", None)
        else:
            os.environ["ORCH_WAKE_PACKET_ENDPOINT_ENABLED"] = old_wake
    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - AI-native real gap messages teach the next step.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
