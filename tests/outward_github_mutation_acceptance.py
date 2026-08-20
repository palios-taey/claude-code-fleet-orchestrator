#!/usr/bin/env python3
"""Acceptance: unbind revokes GitHub outward mutation capability at mutation time.

Proves the P0 safety successor for task-f396305d without live Redis/Neo4j or
real GitHub writes: a bound executor may mutate a fake sink; after clearing the
current_task binding (unbind/revocation), the same still-running actor is denied.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fleet_orchestrator.outward_github_mutation import (  # noqa: E402
    OutwardAuthorizationError,
    authorize_outward_github_mutation,
    post_commit_status,
    post_issue_comment,
)
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


def main() -> int:
    session = "conductor-grok"
    supervisor = "taey-ed-codex"
    task_id = "task-f396305d-fixture"
    redis = FakeRedis()
    sink: List[Dict[str, Any]] = []

    def task_loader(tid: str) -> Optional[Dict[str, Any]]:
        if tid != task_id:
            return None
        return {
            "id": task_id,
            "status": "in_progress",
            "dispatched_to": session,
            "owner": supervisor,
        }

    def fake_gh_api(args: list[str]) -> Dict[str, Any]:
        # Record every attempted mutation; never talks to GitHub.
        event = {"args": list(args), "ts": time.time()}
        sink.append(event)
        if args[:2] == ["-X", "POST"] and "/statuses/" in args[2]:
            return {"id": len(sink), "context": "audit/grok", "state": "success"}
        if args[:2] == ["-X", "POST"] and "/comments" in args[2]:
            return {"id": 9000 + len(sink), "body": "ok"}
        raise RuntimeError(f"unexpected fake gh api args: {args}")

    # --- bound: allow ---
    redis.set(
        state_key(session, "current_task"),
        json.dumps(
            {
                "task_id": task_id,
                "description": "fixture",
                "supervisor": supervisor,
                "started_at": time.time(),
            }
        ),
    )
    decision = authorize_outward_github_mutation(
        session,
        redis_client=redis,
        task_loader=task_loader,
    )
    _check("bound actor authorized", decision.allowed, decision.reason)

    status = post_commit_status(
        session_id=session,
        repo="palios-taey/palios-training",
        sha="abc123",
        state="success",
        context="audit/grok",
        description="ENDORSE fixture",
        redis_client=redis,
        task_loader=task_loader,
        gh_api=fake_gh_api,
    )
    _check("bound status mutation recorded", len(sink) == 1 and status.get("id") == 1, sink)

    comment = post_issue_comment(
        session_id=session,
        repo="palios-taey/palios-training",
        issue_number=32,
        body="fixture comment",
        redis_client=redis,
        task_loader=task_loader,
        gh_api=fake_gh_api,
    )
    _check("bound comment mutation recorded", len(sink) == 2 and comment.get("id") == 9002, sink)

    # --- unbind/revocation while "process still running" ---
    redis.delete(state_key(session, "current_task"))
    denied = authorize_outward_github_mutation(
        session,
        redis_client=redis,
        task_loader=task_loader,
    )
    _check(
        "unbound actor denied despite live task graph",
        (not denied.allowed) and "no live current_task binding" in denied.reason,
        denied,
    )

    before = len(sink)
    try:
        post_commit_status(
            session_id=session,
            repo="palios-taey/palios-training",
            sha="abc123",
            state="success",
            context="audit/grok",
            description="stale worker after unbind",
            redis_client=redis,
            task_loader=task_loader,
            gh_api=fake_gh_api,
        )
        _check("unbound status raises", False, "expected OutwardAuthorizationError")
    except OutwardAuthorizationError as exc:
        _check("unbound status raises", "no live current_task binding" in str(exc), exc)
    _check("unbound status did not mutate sink", len(sink) == before, sink)

    try:
        post_issue_comment(
            session_id=session,
            repo="palios-taey/palios-training",
            issue_number=32,
            body="stale comment after unbind",
            redis_client=redis,
            task_loader=task_loader,
            gh_api=fake_gh_api,
        )
        _check("unbound comment raises", False, "expected OutwardAuthorizationError")
    except OutwardAuthorizationError as exc:
        _check("unbound comment raises", "no live current_task binding" in str(exc), exc)
    _check("unbound comment did not mutate sink", len(sink) == before, sink)

    # --- wrong executor still-running ---
    redis.set(
        state_key(session, "current_task"),
        json.dumps(
            {
                "task_id": task_id,
                "description": "fixture",
                "supervisor": supervisor,
                "started_at": time.time(),
            }
        ),
    )

    def wrong_executor_loader(tid: str) -> Optional[Dict[str, Any]]:
        task = task_loader(tid)
        assert task is not None
        task = dict(task)
        task["dispatched_to"] = "other-worker"
        return task

    wrong = authorize_outward_github_mutation(
        session,
        redis_client=redis,
        task_loader=wrong_executor_loader,
    )
    _check("wrong executor denied", (not wrong.allowed) and "not the live executor" in wrong.reason, wrong)

    if FAILURES:
        print(f"FAIL: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("PASS outward_github_mutation_acceptance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
