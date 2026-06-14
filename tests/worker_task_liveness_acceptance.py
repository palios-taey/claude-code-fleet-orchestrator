#!/usr/bin/env python3
"""Acceptance: task-keyed worker liveness prevents invisible in_progress stalls.

Issue #89 root cause: dispatch liveness was keyed only by one worker
current_task slot. A rapid second dispatch can overwrite the first binding,
leaving the first OrchTask in_progress but unwatched. This gate proves the
task-keyed liveness sweep requeues/notifies the orphan and a stalled current
task, while active current work is not falsely escalated.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import time
import uuid
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace
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


PFX = f"{_require_test_namespace()}-wliv-{uuid.uuid4().hex[:8]}"
os.environ["NOTIFY_KEY_PREFIX"] = PFX
os.environ["ORCH_WORKER_TASK_LIVENESS"] = "1"
os.environ["ORCH_WORKER_TASK_LIVENESS_TTL_SEC"] = "1"

from fleet_orchestrator.config import OrchConfig, get_neo4j_driver, get_redis_sync  # noqa: E402
from fleet_orchestrator.dispatch import _state_key  # noqa: E402
from fleet_orchestrator import dispatch as dispatch_module  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    create_human_review_gate,
    create_phase,
    create_project,
    create_task,
    get_task,
    init_schema,
    update_task_status,
)


CFG = OrchConfig()
SUP = f"{PFX}-sup"
WORKER = f"{SUP}-codex"
PROJECT = f"{PFX}-project"
PHASE = f"{PROJECT}::phase"
FIRST = f"{PROJECT}::first"
SECOND = f"{PROJECT}::second"
STALL = f"{PROJECT}::stall"
AWAIT = f"{PROJECT}::await"
HUMAN_REVIEW = f"{PROJECT}::human-review"
QUESTION = f"{PFX}-question"
REVIEWER = f"{PFX}-reviewer"
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _redis():
    return get_redis_sync(CFG)


def _cleanup() -> None:
    r = _redis()
    cursor = 0
    while True:
        cursor, keys = r.scan(cursor=cursor, match=f"{PFX}:*", count=100)
        if keys:
            r.delete(*keys)
        if cursor == 0:
            break
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=PFX)


def _load_orch_watch():
    path = ROOT / "scripts" / "orch-watch"
    loader = SourceFileLoader("orch_watch_worker_liveness_under_test", str(path))
    spec = importlib.util.spec_from_loader("orch_watch_worker_liveness_under_test", loader)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load scripts/orch-watch")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _setup() -> None:
    init_schema(config=CFG)
    create_project(project_id=PROJECT, name=PROJECT, supervisor=SUP, priority=1, config=CFG)
    create_phase(project_id=PROJECT, phase_id=PHASE, name="phase", config=CFG)
    for task_id in (FIRST, SECOND, STALL, AWAIT):
        create_task(
            phase_id=PHASE,
            task_id=task_id,
            description=task_id,
            owner=SUP,
            priority=10,
            wake_owner_if_ready=False,
            config=CFG,
        )
    create_human_review_gate(
        phase_id=PHASE,
        task_id=HUMAN_REVIEW,
        question_id=QUESTION,
        prompt="Review the liveness wait gate.",
        reviewer=REVIEWER,
        requested_by=SUP,
        notify=False,
        config=CFG,
    )


def _dispatch(task_id: str) -> None:
    ok = SimpleNamespace(returncode=0, stdout="OK", stderr="")
    with mock.patch.object(dispatch_module.subprocess, "run", return_value=ok):
        dispatch_module.dispatch(WORKER, task_id, task_id, supervisor=SUP)


def _current_task_id() -> str:
    raw = _redis().get(_state_key(WORKER, "current_task"))
    if not raw:
        return ""
    return str(json.loads(raw).get("task_id") or "")


def _run_liveness_once(sent: list[tuple[str, str]]) -> int:
    watch = _load_orch_watch()

    def fake_send(_r, target, body, **_kwargs):
        sent.append((target, body))
        return True

    with mock.patch.object(watch, "_send_wake", side_effect=fake_send):
        return watch._process_worker_liveness_expirations(_redis(), dedup_ttl_sec=1)


def _fresh_tool_heartbeat() -> None:
    _redis().set(_state_key(WORKER, "last_tool_activity"), str(time.time()))


def main() -> int:
    _cleanup()
    try:
        _setup()

        _dispatch(FIRST)
        _dispatch(SECOND)
        _check("rapid double dispatch leaves single current_task on second", _current_task_id() == SECOND, _current_task_id())
        _check("first dispatch is still in_progress before TTL", get_task(FIRST, config=CFG).get("status") == "in_progress", get_task(FIRST, config=CFG))
        _check("second dispatch is in_progress before TTL", get_task(SECOND, config=CFG).get("status") == "in_progress", get_task(SECOND, config=CFG))

        time.sleep(1.2)
        _fresh_tool_heartbeat()
        sent: list[tuple[str, str]] = []
        count = _run_liveness_once(sent)
        first = get_task(FIRST, config=CFG)
        second = get_task(SECOND, config=CFG)
        _check("rapid double dispatch orphan requeued", first.get("status") == "pending" and first.get("needs_attention") is True, first)
        _check("rapid double dispatch active second not escalated", second.get("status") == "in_progress", second)
        _check("rapid double dispatch emits supervisor wake", count == 1 and sent and sent[0][0] == SUP and FIRST in sent[0][1], sent)

        time.sleep(1.2)
        _fresh_tool_heartbeat()
        sent.clear()
        count = _run_liveness_once(sent)
        second = get_task(SECOND, config=CFG)
        _check("fresh heartbeat on current task prevents false escalation", count == 0 and second.get("status") == "in_progress", {"count": count, "task": second, "sent": sent})
        update_task_status(
            SECOND,
            "completed",
            completion_evidence={"production_observation": "worker liveness active task completed before stall fixture"},
            config=CFG,
        )

        _dispatch(STALL)
        time.sleep(1.2)
        sent.clear()
        count = _run_liveness_once(sent)
        stalled = get_task(STALL, config=CFG)
        _check("worker stall TTL requeues current task", stalled.get("status") == "pending" and stalled.get("needs_attention") is True, stalled)
        _check("worker stall TTL emits supervisor wake", count == 1 and sent and sent[0][0] == SUP and STALL in sent[0][1], sent)

        _dispatch(AWAIT)
        update_task_status(
            AWAIT,
            "in_progress",
            owner=WORKER,
            blocked_on="AWAIT:family-consent:liveness acceptance",
            config=CFG,
        )
        time.sleep(1.2)
        sent.clear()
        count = _run_liveness_once(sent)
        await_task = get_task(AWAIT, config=CFG)
        _check(
            "AWAIT-gated task past TTL is not escalated",
            count == 0 and await_task.get("status") == "in_progress" and await_task.get("blocked_on") == "AWAIT:family-consent:liveness acceptance",
            {"count": count, "task": await_task, "sent": sent},
        )

        _dispatch(HUMAN_REVIEW)
        time.sleep(1.2)
        sent.clear()
        count = _run_liveness_once(sent)
        human_review_task = get_task(HUMAN_REVIEW, config=CFG)
        _check(
            "human-review gate past TTL is not escalated",
            count == 0 and human_review_task.get("status") == "in_progress",
            {"count": count, "task": human_review_task, "sent": sent},
        )
    finally:
        _cleanup()

    if FAILURES:
        print(f"\nFAIL -- {len(FAILURES)}: {FAILURES}")
        return 1
    print("\nPASS -- task-keyed worker liveness surfaces double-dispatch orphans and stalled workers.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
