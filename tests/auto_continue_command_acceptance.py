#!/usr/bin/env python3
"""Acceptance: AUTO_CONTINUE wakes include a resolvable task command."""
from __future__ import annotations

import os
import re
import sys
import uuid
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _require_test_namespace() -> str:
    raw = (os.environ.get("ORCH_TEST_NAMESPACE") or "").strip()
    if not raw:
        raise SystemExit("ORCH_TEST_NAMESPACE is required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{5,80}", raw):
        raise SystemExit("ORCH_TEST_NAMESPACE must be 6-80 chars of letters/digits/._:-")
    if not any(marker in raw.lower() for marker in ("test", "ci", "acceptance")):
        raise SystemExit("ORCH_TEST_NAMESPACE must include test, ci, or acceptance")
    return raw


PFX = f"{_require_test_namespace()}-autocontinue-{uuid.uuid4().hex[:8]}"
SESSION = f"{PFX}-conductor"
CURRENT_TASK = f"{PFX}-project::current"
NEXT_TASK = f"{PFX}-project::next-ready"
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def main() -> int:
    from fleet_orchestrator import cli_orch_watch as watch

    sent: list[dict[str, object]] = []

    def fake_send(_r: object, target: str, body: str, **kwargs: object) -> bool:
        sent.append({"target": target, "body": body, "kwargs": kwargs})
        return True

    with mock.patch.object(
        watch,
        "_load_task_state",
        return_value={"id": CURRENT_TASK, "status": "in_progress", "owner": SESSION, "blocked_on": ""},
    ), mock.patch.object(
        watch,
        "_task_project_context",
        return_value={"project_id": f"{PFX}-project", "user_stop_conditions": []},
    ), mock.patch.object(
        watch,
        "_evaluate_user_stop_conditions",
        return_value=(None, {"task_id": NEXT_TASK, "description": "next ready acceptance task"}),
    ), mock.patch.object(
        watch,
        "_stop_gate_dedup",
        return_value=True,
    ), mock.patch.object(
        watch,
        "_send_wake",
        side_effect=fake_send,
    ):
        handled = watch._handle_user_stop_gate(object(), SESSION, {"task_id": CURRENT_TASK})

    body = str(sent[0]["body"]) if sent else ""
    _check("AUTO_CONTINUE branch handled the stop gate", handled is True, handled)
    _check("AUTO_CONTINUE sent one wake to the stopping session", len(sent) == 1 and sent[0]["target"] == SESSION, sent)
    _check("wake body names the next task id", NEXT_TASK in body, body)
    _check("wake body includes taey-task status command", f"taey-task status {NEXT_TASK}" in body, body)
    _check("wake body includes taey-plan current fallback", "taey-plan current" in body, body)

    if FAILURES:
        print(f"\nFAIL -- {len(FAILURES)}: {FAILURES}")
        return 1
    print("\nPASS -- AUTO_CONTINUE wakes point workers at resolvable task commands.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
