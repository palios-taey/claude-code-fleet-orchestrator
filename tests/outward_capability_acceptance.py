#!/usr/bin/env python3
"""Acceptance: one shared outward boundary gates GitHub + taey-notify.

Proves task-f396305d rework without live Redis/Neo4j or real GitHub/notify:
bound executor may mutate status, comment, and notify sinks; after clearing
current_task (unbind), the same still-running actor is denied on all three;
all three paths call the same ``authorize_outward_capability`` function object.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fleet_orchestrator import outward_capability as oc  # noqa: E402
from fleet_orchestrator.outward_capability import (  # noqa: E402
    OutwardAuthorizationError,
    post_commit_status,
    post_issue_comment,
    send_outward_notify,
)
from fleet_orchestrator.notify_state import state_key  # noqa: E402


FAILURES: list[str] = []
AUTH_CALLS: list[str] = []


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
    gh_sink: List[Dict[str, Any]] = []
    notify_sink_log: List[Dict[str, Any]] = []

    real_authorize = oc.authorize_outward_capability

    def tracking_authorize(session_id: str, **kwargs: Any):
        AUTH_CALLS.append(str(kwargs.get("channel") or ""))
        return real_authorize(session_id, **kwargs)

    # Prove helpers close over / call the module attribute (shared boundary).
    oc.authorize_outward_capability = tracking_authorize  # type: ignore[assignment]
    try:
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
            event = {"args": list(args), "ts": time.time()}
            gh_sink.append(event)
            if args[:2] == ["-X", "POST"] and "/statuses/" in args[2]:
                return {"id": len(gh_sink), "context": "audit/grok", "state": "success"}
            if args[:2] == ["-X", "POST"] and "/comments" in args[2]:
                return {"id": 9000 + len(gh_sink), "body": "ok"}
            raise RuntimeError(f"unexpected fake gh api args: {args}")

        def fake_notify(target: str, body: str, msg_type: str, from_node: str) -> Dict[str, Any]:
            event = {
                "target": target,
                "body": body,
                "type": msg_type,
                "from": from_node,
                "ts": time.time(),
            }
            notify_sink_log.append(event)
            return {"ok": True, "id": len(notify_sink_log)}

        _check(
            "github authorize alias points at shared boundary",
            oc.authorize_outward_github_mutation is real_authorize,
            oc.authorize_outward_github_mutation,
        )
        _check(
            "github require alias points at shared require",
            oc.require_outward_github_mutation is oc.require_outward_capability,
            oc.require_outward_github_mutation,
        )

        # --- bound: allow all three channels ---
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
        AUTH_CALLS.clear()
        decision = oc.authorize_outward_capability(
            session,
            channel="probe",
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
        _check("bound status mutation recorded", len(gh_sink) == 1 and status.get("id") == 1, gh_sink)

        comment = post_issue_comment(
            session_id=session,
            repo="palios-taey/palios-training",
            issue_number=32,
            body="fixture comment",
            redis_client=redis,
            task_loader=task_loader,
            gh_api=fake_gh_api,
        )
        _check("bound comment mutation recorded", len(gh_sink) == 2 and comment.get("id") == 9002, gh_sink)

        notify = send_outward_notify(
            session_id=session,
            target=supervisor,
            body="RESPONSE_READY: fixture",
            msg_type="response_ready",
            redis_client=redis,
            task_loader=task_loader,
            notify_sink=fake_notify,
        )
        _check("bound notify mutation recorded", len(notify_sink_log) == 1 and notify.get("id") == 1, notify_sink_log)

        _check(
            "shared boundary saw github_status+github_comment+taey_notify",
            set(AUTH_CALLS) >= {"github_status", "github_comment", "taey_notify"},
            AUTH_CALLS,
        )

        # --- unbind/revocation while process still running ---
        redis.delete(state_key(session, "current_task"))
        denied = oc.authorize_outward_capability(
            session,
            channel="probe",
            redis_client=redis,
            task_loader=task_loader,
        )
        _check(
            "unbound actor denied despite live task graph",
            (not denied.allowed) and "no live current_task binding" in denied.reason,
            denied,
        )

        before_gh = len(gh_sink)
        before_notify = len(notify_sink_log)
        for label, fn in (
            (
                "status",
                lambda: post_commit_status(
                    session_id=session,
                    repo="palios-taey/palios-training",
                    sha="abc123",
                    state="success",
                    context="audit/grok",
                    description="stale after unbind",
                    redis_client=redis,
                    task_loader=task_loader,
                    gh_api=fake_gh_api,
                ),
            ),
            (
                "comment",
                lambda: post_issue_comment(
                    session_id=session,
                    repo="palios-taey/palios-training",
                    issue_number=32,
                    body="stale comment after unbind",
                    redis_client=redis,
                    task_loader=task_loader,
                    gh_api=fake_gh_api,
                ),
            ),
            (
                "notify",
                lambda: send_outward_notify(
                    session_id=session,
                    target=supervisor,
                    body="stale RESPONSE_READY",
                    msg_type="response_ready",
                    redis_client=redis,
                    task_loader=task_loader,
                    notify_sink=fake_notify,
                ),
            ),
        ):
            try:
                fn()
                _check(f"unbound {label} raises", False, "expected OutwardAuthorizationError")
            except OutwardAuthorizationError as exc:
                _check(f"unbound {label} raises", "no live current_task binding" in str(exc), exc)

        _check("unbound did not mutate github sink", len(gh_sink) == before_gh, gh_sink)
        _check(
            "unbound did not mutate notify sink",
            len(notify_sink_log) == before_notify,
            notify_sink_log,
        )

        # --- wrong executor ---
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

        wrong = oc.authorize_outward_capability(
            session,
            redis_client=redis,
            task_loader=wrong_executor_loader,
        )
        _check("wrong executor denied", (not wrong.allowed) and "not the live executor" in wrong.reason, wrong)

    finally:
        oc.authorize_outward_capability = real_authorize  # type: ignore[assignment]

    if FAILURES:
        print(f"FAIL: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("PASS outward_capability_acceptance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
