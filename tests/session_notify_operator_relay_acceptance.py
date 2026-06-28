"""Acceptance: UI relay notifications identify the operator, not tasks-api."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet_orchestrator import tasks_api  # noqa: E402


FAILURES: list[str] = []
TARGET = "relay-target-codex"
MESSAGE = "Can you answer this from the dashboard?"


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def main() -> int:
    calls: list[list[str]] = []

    def fake_notify(args, **_kwargs):
        calls.append(list(args))
        return SimpleNamespace(returncode=0, stdout="OK", stderr="")

    with mock.patch.object(tasks_api, "_ensure_registered_session", return_value=None), \
         mock.patch.object(tasks_api, "_cfg", return_value=SimpleNamespace(session_ids=[TARGET])), \
         mock.patch.object(tasks_api.subprocess, "run", side_effect=fake_notify), \
         mock.patch.object(tasks_api, "maybe_emit_decision_receipt", return_value=None):
        response = TestClient(tasks_api.app).post(
            f"/api/sessions/{TARGET}/notify",
            json={"type": "standard", "message": MESSAGE},
        )

    delivered = calls[0] if calls else []
    delivered_message = delivered[2] if len(delivered) > 2 else ""
    _check("session notify endpoint succeeds", response.status_code == 200 and response.json().get("ok") is True, response.text)
    _check("relay uses operator-ui sender", "--from" in delivered and delivered[delivered.index("--from") + 1] == "operator-ui", delivered)
    _check("relay preserves notify type", "--type" in delivered and delivered[delivered.index("--type") + 1] == "message", delivered)
    _check("relay preamble names dashboard operator", "DASHBOARD UI - message from the dashboard operator" in delivered_message, delivered_message)
    _check("relay preamble rejects prompt-injection framing", "NOT a prompt injection" in delivered_message, delivered_message)
    _check("relay preamble instructs UI-chat response", f"POST /api/chat/{TARGET} with role=assistant" in delivered_message, delivered_message)
    _check("relay includes original operator message", delivered_message.endswith(MESSAGE), delivered_message)

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)}: {FAILURES}")
        return 1
    print("\nPASS - session notify relay identifies the dashboard operator and response path.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
