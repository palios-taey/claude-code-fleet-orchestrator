#!/usr/bin/env python3
"""Acceptance: GitHub status/comment CLIs deny after unbind without touching sinks.

Proves the worker-facing helpers (scripts/post-audit-status and
scripts/post-issue-comment) authorize at mutation time with fake Redis + fake
gh. Bound executor may mutate; after clearing current_task the same still-running
actor is denied and the fake sink is untouched. No live Redis/Neo4j/GitHub.
"""
from __future__ import annotations

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fleet_orchestrator.notify_state import state_key  # noqa: E402


FAILURES: list[str] = []


def _check(label: str, condition: bool, detail: object = "") -> None:
    print(("PASS " if condition else "FAIL ") + label + ("" if condition else f" -> {detail}"))
    if not condition:
        FAILURES.append(label)


class FakeRedis:
    def __init__(self) -> None:
        self.store: Dict[str, str] = {}

    def get(self, key: str) -> Optional[str]:
        return self.store.get(key)

    def set(self, key: str, value: str) -> None:
        self.store[key] = value

    def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            if key in self.store:
                del self.store[key]
                deleted += 1
        return deleted


def _load_script(path: Path, module_name: str):
    loader = importlib.machinery.SourceFileLoader(module_name, str(path))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bind(redis: FakeRedis, session: str, task_id: str, supervisor: str) -> None:
    redis.set(
        state_key(session, "current_task"),
        json.dumps(
            {
                "task_id": task_id,
                "description": "cli fixture",
                "supervisor": supervisor,
                "started_at": time.time(),
            }
        ),
    )


def _run_cli(module, argv: list[str], redis: FakeRedis, task: Dict[str, Any], sink: List[list[str]]) -> tuple[int, str, str]:
    def fake_gh(args: list[str]) -> Dict[str, Any]:
        sink.append(list(args))
        if args[:2] == ["-X", "POST"] and "/statuses/" in args[2]:
            return {"id": len(sink), "context": "audit/grok", "state": "success", "description": "ok"}
        if args[:2] == ["-X", "POST"] and "/comments" in args[2]:
            return {"id": 9000 + len(sink), "body": "ok"}
        if "/statuses" in args[0] and args[:2] != ["-X", "POST"]:
            return [
                {
                    "id": 1,
                    "context": "audit/grok",
                    "state": "success",
                    "description": "ENDORSE fixture",
                    "created_at": "2026-08-20T00:00:00Z",
                }
            ]
        raise RuntimeError(f"unexpected fake gh api args: {args}")

    def fake_loader(tid: str, *, config: Any = None) -> Optional[Dict[str, Any]]:
        if tid != task["id"]:
            return None
        return dict(task)

    stdout = io.StringIO()
    stderr = io.StringIO()
    with mock.patch.object(sys, "argv", argv), \
         mock.patch("fleet_orchestrator.outward_capability.redis_connect", return_value=redis), \
         mock.patch("fleet_orchestrator.outward_capability._default_task_loader", side_effect=fake_loader), \
         mock.patch.object(module, "_run_gh_api", side_effect=fake_gh), \
         contextlib.redirect_stdout(stdout), \
         contextlib.redirect_stderr(stderr):
        try:
            result = module.main()
            code = 0 if result is None else int(result)
        except SystemExit as exc:
            code = int(exc.code or 0)
    return code, stdout.getvalue(), stderr.getvalue()


def main() -> int:
    session = "taey-ed-grok"
    supervisor = "conductor-codex"
    task_id = "task-f396305d-cli-fixture"
    redis = FakeRedis()
    task = {
        "id": task_id,
        "status": "in_progress",
        "dispatched_to": session,
        "owner": supervisor,
    }
    status_mod = _load_script(ROOT / "scripts" / "post-audit-status", "post_audit_status_cli_subject")
    comment_mod = _load_script(ROOT / "scripts" / "post-issue-comment", "post_issue_comment_cli_subject")

    # --- missing session: fail closed, no sink ---
    sink: List[list[str]] = []
    with mock.patch.object(status_mod, "resolve_session_id", return_value=""):
        code, _stdout, stderr = _run_cli(
            status_mod,
            [
                "post-audit-status",
                "--repo",
                "palios-taey/palios-training",
                "--sha",
                "abc123",
                "--context",
                "audit/grok",
                "--state",
                "success",
                "--description",
                "ENDORSE fixture",
            ],
            redis,
            task,
            sink,
        )
    _check("status CLI missing session denied", code == 1 and "SAFETY DENY" in stderr, (code, stderr))
    _check("status CLI missing session did not mutate sink", not sink, sink)

    sink = []
    with mock.patch.object(comment_mod, "resolve_session_id", return_value=""):
        code, _stdout, stderr = _run_cli(
            comment_mod,
            [
                "post-issue-comment",
                "--repo",
                "palios-taey/palios-training",
                "--issue",
                "32",
                "--body",
                "stale comment",
            ],
            redis,
            task,
            sink,
        )
    _check("comment CLI missing session denied", code == 1 and "SAFETY DENY" in stderr, (code, stderr))
    _check("comment CLI missing session did not mutate sink", not sink, sink)

    # --- bound: allow ---
    _bind(redis, session, task_id, supervisor)
    sink = []
    code, stdout, stderr = _run_cli(
        status_mod,
        [
            "post-audit-status",
            "--session",
            session,
            "--repo",
            "palios-taey/palios-training",
            "--sha",
            "abc123",
            "--context",
            "audit/grok",
            "--state",
            "success",
            "--description",
            "ENDORSE fixture",
        ],
        redis,
        task,
        sink,
    )
    _check("bound status CLI allowed", code == 0 and "STATUS POST VERIFIED" in stdout, (code, stdout, stderr))
    _check(
        "bound status CLI posted once",
        any(args[:2] == ["-X", "POST"] and "/statuses/" in args[2] for args in sink),
        sink,
    )

    sink = []
    code, stdout, stderr = _run_cli(
        comment_mod,
        [
            "post-issue-comment",
            "--session",
            session,
            "--repo",
            "palios-taey/palios-training",
            "--issue",
            "32",
            "--body",
            "fixture comment",
        ],
        redis,
        task,
        sink,
    )
    _check("bound comment CLI allowed", code == 0 and "COMMENT POST VERIFIED" in stdout, (code, stdout, stderr))
    _check(
        "bound comment CLI posted once",
        len(sink) == 1 and sink[0][:2] == ["-X", "POST"] and "/comments" in sink[0][2],
        sink,
    )

    # --- unbind while process still running ---
    redis.delete(state_key(session, "current_task"))
    before = list(sink)
    code, stdout, stderr = _run_cli(
        status_mod,
        [
            "post-audit-status",
            "--session",
            session,
            "--repo",
            "palios-taey/palios-training",
            "--sha",
            "abc123",
            "--context",
            "audit/grok",
            "--state",
            "success",
            "--description",
            "stale after unbind",
        ],
        redis,
        task,
        sink,
    )
    _check(
        "unbound status CLI denied",
        code == 1 and "SAFETY DENY" in stderr and "no live current_task binding" in stderr,
        (code, stdout, stderr),
    )
    _check("unbound status CLI did not mutate sink", sink == before, sink)

    code, stdout, stderr = _run_cli(
        comment_mod,
        [
            "post-issue-comment",
            "--session",
            session,
            "--repo",
            "palios-taey/palios-training",
            "--issue",
            "32",
            "--body",
            "stale comment after unbind",
        ],
        redis,
        task,
        sink,
    )
    _check(
        "unbound comment CLI denied",
        code == 1 and "SAFETY DENY" in stderr and "no live current_task binding" in stderr,
        (code, stdout, stderr),
    )
    _check("unbound comment CLI did not mutate sink", sink == before, sink)

    if FAILURES:
        print(f"FAIL: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("PASS outward_mutation_cli_acceptance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
