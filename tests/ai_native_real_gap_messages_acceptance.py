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
            orch_schema.get_session_stop_decision("conductor-codex", stop_hook_active=True, config=cfg)
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
            "created_by": "conductor",
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
            ("notify message", client.post("/api/sessions/conductor/notify", json={}), 400, ["message", "POST /api/sessions/conductor/notify", "taey-notify"])
        )
    for label, response, status, needles in cases:
        _assert_detail(label, response, status, needles)


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
