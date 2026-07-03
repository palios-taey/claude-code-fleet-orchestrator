#!/usr/bin/env python3
"""Acceptance: taey-task terminal evidence can avoid shell-quoted JSON."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fleet_orchestrator import cli_taey_task  # noqa: E402

FAILURES: list[str] = []


def _check(label: str, condition: bool, detail: object = "") -> None:
    print(("PASS " if condition else "FAIL ") + label + ("" if condition else f" -> {detail}"))
    if not condition:
        FAILURES.append(label)


def _run_cli(argv: list[str]) -> tuple[int, str, str, list[dict]]:
    calls: list[dict] = []

    def fake_api_call(method: str, endpoint: str, data=None):
        calls.append({"method": method, "endpoint": endpoint, "data": data})
        return {"ok": True}

    stdout = io.StringIO()
    stderr = io.StringIO()
    code = 0
    with mock.patch.object(sys, "argv", argv), \
         mock.patch.object(cli_taey_task, "detect_from_node", return_value="test-codex"), \
         mock.patch.object(cli_taey_task, "api_call", side_effect=fake_api_call), \
         contextlib.redirect_stdout(stdout), \
         contextlib.redirect_stderr(stderr):
        try:
            cli_taey_task.main()
        except SystemExit as exc:
            code = int(exc.code or 0) if isinstance(exc.code, int) else 1
    return code, stdout.getvalue(), stderr.getvalue(), calls


def main() -> int:
    observation = "verified \"quote\"\noperator's shell did not hand-escape JSON"
    code, _out, err, calls = _run_cli(
        [
            "taey-task",
            "update",
            "task-shell-fragility",
            "completed",
            "--evidence-observation",
            observation,
        ]
    )
    _check("plain observation exits cleanly", code == 0, err)
    _check("plain observation calls PATCH once", len(calls) == 1, calls)
    payload = calls[0]["data"] if calls else {}
    _check("plain observation wraps production_observation", payload.get("evidence", {}).get("production_observation") == observation, payload)
    _check("plain observation keeps sender", payload.get("from") == "test-codex", payload)

    with tempfile.NamedTemporaryFile("w", encoding="utf-8", delete=False) as handle:
        evidence_path = handle.name
        json.dump(
            {
                "commit_sha": "abc123",
                "repo": "OWNER/REPO",
                "production_observation": observation,
            },
            handle,
        )
    try:
        code, _out, err, calls = _run_cli(
            [
                "taey-task",
                "update",
                "task-file-evidence",
                "completed",
                "--evidence-file",
                evidence_path,
            ]
        )
    finally:
        os.unlink(evidence_path)
    _check("evidence file exits cleanly", code == 0, err)
    payload = calls[0]["data"] if calls else {}
    _check("evidence file sends parsed object", payload.get("evidence", {}).get("commit_sha") == "abc123", payload)
    _check("evidence file preserves prose", payload.get("evidence", {}).get("production_observation") == observation, payload)

    code, _out, err, calls = _run_cli(
        [
            "taey-task",
            "update",
            "task-bad-json",
            "completed",
            "--evidence",
            '{"production_observation": "unterminated}',
        ]
    )
    _check("malformed inline evidence fails before API", code == 1, (code, err, calls))
    _check("malformed inline evidence does not call API", calls == [], calls)
    _check("malformed inline evidence names parse position", "line 1, column" in err and "char" in err, err)
    _check("malformed inline evidence suggests safer flags", "--evidence-file" in err and "--evidence-observation" in err, err)

    code, _out, err, calls = _run_cli(
        [
            "taey-task",
            "update",
            "task-nonterminal",
            "in_progress",
            "--evidence-observation",
            observation,
        ]
    )
    _check("non-terminal evidence still rejected before API", code == 1, (code, err, calls))
    _check("non-terminal evidence rejection does not call API", calls == [], calls)
    _check("non-terminal evidence error teaches terminal retry", "completed --evidence-observation" in err, err)

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - taey-task evidence file/plain observation modes avoid shell-quoted JSON fragility.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
