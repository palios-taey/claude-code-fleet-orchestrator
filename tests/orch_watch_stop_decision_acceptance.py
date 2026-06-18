#!/usr/bin/env python3
"""Acceptance: orch-watch supervisor wakes respect the stop-decision engine."""
from __future__ import annotations

import json
import os
import re
import sys
import time
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


_NAMESPACE = _require_test_namespace()
PFX = f"{_NAMESPACE}-orchwake-{uuid.uuid4().hex[:8]}"
os.environ["NOTIFY_KEY_PREFIX"] = PFX
os.environ["ORCH_WORKER_TASK_LIVENESS"] = "1"
os.environ["ORCH_WORKER_TASK_LIVENESS_TTL_SEC"] = "1"

from fleet_orchestrator.config import OrchConfig, get_neo4j_driver, get_redis_sync  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    _state_key,
    create_phase,
    create_project,
    create_task,
    get_session_stop_decision,
    get_task,
    init_schema,
    update_task_status,
)
from fleet_orchestrator.worker_liveness import register_worker_task_liveness  # noqa: E402
from fleet_orchestrator import cli_orch_watch as watch  # noqa: E402


CFG = OrchConfig()
SUP = f"{PFX}-sup"
PEER = f"{SUP}-codex"
AWAIT_PROJECT = f"{PFX}-await-project"
AWAIT_PHASE = f"{AWAIT_PROJECT}::phase"
AWAIT_TASK = f"{AWAIT_PROJECT}::await-family-consent"
STALE_REDIS_TASK = f"{PFX}-redis-only-current-task"
UNBLOCK_TASK = f"{PFX}-unblock-completed"
LIVENESS_PROJECT = f"{PFX}-liveness-project"
LIVENESS_PHASE = f"{LIVENESS_PROJECT}::phase"
LIVENESS_TASK = f"{LIVENESS_PROJECT}::stale-worker-task"
LIVENESS_WORKER = f"{PFX}-adhoc-worker"
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
    watch._TASK_SNAPSHOTS.clear()


def _setup_stop_allowed_fixture() -> None:
    init_schema(config=CFG)
    create_project(project_id=AWAIT_PROJECT, name=AWAIT_PROJECT, supervisor=SUP, priority=1, config=CFG)
    create_phase(project_id=AWAIT_PROJECT, phase_id=AWAIT_PHASE, name="phase", config=CFG)
    create_task(
        phase_id=AWAIT_PHASE,
        task_id=AWAIT_TASK,
        description="await family consent",
        owner=SUP,
        priority=10,
        wake_owner_if_ready=False,
        config=CFG,
    )
    update_task_status(
        AWAIT_TASK,
        "in_progress",
        owner=SUP,
        blocked_on="AWAIT:family-consent:orch-watch acceptance",
        config=CFG,
    )


def _set_supervisor_current_task(enabled: bool) -> None:
    r = _redis()
    if enabled:
        r.set(
            _state_key(SUP, "current_task"),
            json.dumps({
                "task_id": AWAIT_TASK,
                "description": "await family consent",
                "supervisor": SUP,
                "started_at": time.time() - 60,
            }),
        )
    else:
        r.delete(_state_key(SUP, "current_task"))


def _assert_stop_allowed(label: str) -> None:
    decision = get_session_stop_decision(SUP, config=CFG)
    _check(label, decision.get("block") is False and decision.get("wake_type") == "ALLOW_STOP", decision)


def _bind_stale_peer_current_task() -> None:
    r = _redis()
    now = time.time()
    for suffix in ("last_outcome",):
        r.delete(_state_key(PEER, suffix))
    r.set(
        _state_key(PEER, "current_task"),
        json.dumps({
            "task_id": STALE_REDIS_TASK,
            "description": "stale redis-only peer task",
            "supervisor": SUP,
            "started_at": now - 30,
        }),
    )
    r.set(_state_key(PEER, "idle"), "1")
    r.set(_state_key(PEER, "last_activity"), str(now - 30))


def _run_investigate(event_type: str, *, guard_enabled: bool = True) -> list[tuple[str, str]]:
    sent: list[tuple[str, str]] = []

    def fake_send(_r, target, body, **_kwargs):
        sent.append((target, body))
        return True

    patches = [mock.patch.object(watch, "_send_wake", side_effect=fake_send)]
    if not guard_enabled:
        patches.append(mock.patch.object(watch, "_target_stop_decision_allows_stop", return_value=False))

    with patches[0]:
        if len(patches) > 1:
            with patches[1]:
                watch.investigate(_redis(), PEER, event_type, 1, 1, readiness_checker=None)
        else:
            watch.investigate(_redis(), PEER, event_type, 1, 1, readiness_checker=None)
    return sent


def _run_unblock_wake() -> list[tuple[str, str]]:
    sent: list[tuple[str, str]] = []

    def fake_send(_r, target, body, **_kwargs):
        sent.append((target, body))
        return True

    with mock.patch.object(watch, "_send_wake", side_effect=fake_send):
        result = watch.notify_supervisor_of_unblock(
            _redis(),
            SUP,
            {"task_id": UNBLOCK_TASK, "description": "completed worker task"},
            "[UNBLOCK] ready work changed",
            dedup_ttl_sec=1,
        )
    _check("unblock wake call reports suppressed", result is False, result)
    return sent


def _setup_liveness_fixture() -> None:
    create_project(project_id=LIVENESS_PROJECT, name=LIVENESS_PROJECT, supervisor=SUP, priority=2, config=CFG)
    create_phase(project_id=LIVENESS_PROJECT, phase_id=LIVENESS_PHASE, name="phase", config=CFG)
    create_task(
        phase_id=LIVENESS_PHASE,
        task_id=LIVENESS_TASK,
        description="stale non-peer worker task",
        owner=LIVENESS_WORKER,
        priority=10,
        wake_owner_if_ready=False,
        config=CFG,
    )
    update_task_status(LIVENESS_TASK, "in_progress", owner=LIVENESS_WORKER, config=CFG)
    register_worker_task_liveness(
        LIVENESS_WORKER,
        LIVENESS_TASK,
        "stale non-peer worker task",
        supervisor=SUP,
        started_at=time.time() - 30,
        ttl_secs=1,
        config=CFG,
    )


def _run_liveness_expiration() -> tuple[int, list[tuple[str, str]]]:
    sent: list[tuple[str, str]] = []

    def fake_send(_r, target, body, **_kwargs):
        sent.append((target, body))
        return True

    with mock.patch.object(watch, "_send_wake", side_effect=fake_send):
        count = watch._process_worker_liveness_expirations(
            _redis(),
            dedup_ttl_sec=1,
            task_id_prefix=PFX,
            project_id_prefix=PFX,
        )
    return count, sent


def main() -> int:
    _cleanup()
    try:
        _setup_stop_allowed_fixture()

        for current_task_enabled in (False, True):
            state = "set" if current_task_enabled else "cleared"
            _set_supervisor_current_task(current_task_enabled)
            _assert_stop_allowed(f"supervisor current_task {state} still allows stop")

            for event_type in ("current_task_set", "idle_set", "last_activity_set", "sweep"):
                _bind_stale_peer_current_task()
                sent = _run_investigate(event_type)
                _check(f"{event_type} emits no PEER_IDLE wake when stop decision allows stop", sent == [], sent)

        _set_supervisor_current_task(True)
        _bind_stale_peer_current_task()
        reverted_sent = _run_investigate("sweep", guard_enabled=False)
        _check(
            "reverting stop-decision guard reproduces PEER_IDLE wake",
            len(reverted_sent) == 1 and reverted_sent[0][0] == SUP and "[PEER_IDLE]" in reverted_sent[0][1],
            reverted_sent,
        )

        _set_supervisor_current_task(False)
        sent = _run_unblock_wake()
        _check("unblock path emits no wake when stop decision allows stop", sent == [], sent)

        _setup_liveness_fixture()
        count, sent = _run_liveness_expiration()
        liveness_task = get_task(LIVENESS_TASK, config=CFG)
        _check("worker-liveness task was still escalated", liveness_task.get("status") == "pending", liveness_task)
        _check("worker-liveness path emits no wake when stop decision allows stop", count == 0 and sent == [], {"count": count, "sent": sent})
    finally:
        _cleanup()

    if FAILURES:
        print(f"\nFAIL -- {len(FAILURES)}: {FAILURES}")
        return 1
    print("\nPASS -- orch-watch supervisor wakes honor ALLOW_STOP stop decisions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
