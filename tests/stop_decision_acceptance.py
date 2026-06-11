#!/usr/bin/env python3
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PREFIX = f"hvstop-{uuid.uuid4().hex[:8]}"
SUPERVISOR = f"{PREFIX}-sup"
WORKER = f"{SUPERVISOR}-codex"
UNENFORCED = f"{PREFIX}-free"
os.environ["NOTIFY_KEY_PREFIX"] = PREFIX
if "ORCH_DOTENV" not in os.environ:
    for candidate in (
        ROOT / ".env",
        Path.home() / "claude-code-fleet-orchestrator/.env",
    ):
        if candidate.is_file():
            os.environ["ORCH_DOTENV"] = str(candidate)
            break
os.environ.setdefault("ORCH_REDIS_HOST", "127.0.0.1")
os.environ.setdefault("ORCH_REDIS_PORT", "6379")
os.environ.pop("CF_STOP_INPROGRESS", None)
os.environ.pop("CF_STOP_INPROGRESS_SESSIONS", None)

from fleet_orchestrator.config import OrchConfig, get_neo4j_driver, get_redis_sync  # noqa: E402
from fleet_orchestrator.orch_schema import create_phase, create_project, create_task, get_session_next_ready, get_session_stop_decision, update_task_status, _stop_inprogress_enabled  # noqa: E402

CFG = OrchConfig()


def _cleanup(prefix: str) -> None:
    driver = get_neo4j_driver(CFG)
    with driver.session(database=CFG.neo4j_db) as session:
        session.run("MATCH (t:OrchTask) WHERE t.id STARTS WITH $prefix DETACH DELETE t", prefix=prefix)
        session.run("MATCH (ph:OrchPhase) WHERE ph.id STARTS WITH $prefix DETACH DELETE ph", prefix=prefix)
        session.run("MATCH (p:OrchProject) WHERE p.id STARTS WITH $prefix DETACH DELETE p", prefix=prefix)
    r = get_redis_sync(CFG)
    cursor = 0
    while True:
        cursor, keys = r.scan(cursor=cursor, match=f"{prefix}:*", count=100)
        if keys:
            r.delete(*keys)
        if cursor == 0:
            break


def _write_flag_file(payload: str) -> str:
    handle = tempfile.NamedTemporaryFile("w", delete=False, suffix=".json")
    handle.write(payload)
    handle.flush()
    handle.close()
    return handle.name


def _make_priority_fixture() -> None:
    project_id = f"{PREFIX}-project"
    phase_id = f"{PREFIX}-phase"
    create_project(project_id, "hv ordering", supervisor=SUPERVISOR, priority=1, config=CFG)
    create_phase(project_id, phase_id, "Main", config=CFG)
    create_task(phase_id, f"{PREFIX}-task-10", "priority 10", owner=SUPERVISOR, priority=10, wake_owner_if_ready=False, config=CFG)
    create_task(phase_id, f"{PREFIX}-task-2", "priority 2", owner=SUPERVISOR, priority=2, wake_owner_if_ready=False, config=CFG)


def _make_in_progress_fixture(*, owner: str = WORKER, blocked_on: str | None = None) -> str:
    project_id = f"{PREFIX}-ip-project-{uuid.uuid4().hex[:6]}"
    phase_id = f"{PREFIX}-ip-phase-{uuid.uuid4().hex[:6]}"
    task_id = f"{PREFIX}-ip-task-{uuid.uuid4().hex[:6]}"
    create_project(project_id, "hv in-progress", supervisor=SUPERVISOR, priority=1, config=CFG)
    create_phase(project_id, phase_id, "Main", config=CFG)
    create_task(phase_id, task_id, "owned in-progress task", owner=owner, priority=5, wake_owner_if_ready=False, config=CFG)
    update_task_status(task_id, "in_progress", owner=owner, blocked_on=blocked_on, config=CFG)
    return task_id


def main() -> int:
    _cleanup(PREFIX)
    try:
        flag_file = _write_flag_file(f'{{"{SUPERVISOR}":{{"enforce":true}},"{UNENFORCED}":{{"enforce":false}}}}')
        os.environ["CF_HANDOFF_SESSION_FLAGS_FILE"] = flag_file

        off = get_session_stop_decision(UNENFORCED, config=CFG)
        print("PASS conductor-only-enforce" if off.get("block") is False and off.get("wake_type") == "ALLOW_STOP" else f"FAIL conductor-only-enforce {off}")

        with mock.patch("fleet_orchestrator.orch_schema.flags_for_session", side_effect=RuntimeError("flag-boom")):
            flag_fail_open = get_session_stop_decision(UNENFORCED, config=CFG)
        print("PASS flags-for-session-fail-open" if flag_fail_open.get("wake_type") == off.get("wake_type") and flag_fail_open.get("block") == off.get("block") else f"FAIL flags-for-session-fail-open {flag_fail_open}")

        with mock.patch("fleet_orchestrator.orch_schema.validate_stop_handoff", side_effect=TimeoutError("boom")):
            fail_closed = get_session_stop_decision(SUPERVISOR, config=CFG)
        print("PASS handoff-redis-down-fail-closed" if fail_closed.get("block") is True and fail_closed.get("hv_fail_closed") else f"FAIL handoff-redis-down-fail-closed {fail_closed}")

        with mock.patch("fleet_orchestrator.orch_schema._raw_stop_decision", side_effect=RuntimeError("neo4j-boom")):
            raw_fail_closed = get_session_stop_decision(SUPERVISOR, config=CFG)
        print("PASS raw-stop-fail-closed" if raw_fail_closed.get("block") is True and raw_fail_closed.get("keystone_fail_closed") else f"FAIL raw-stop-fail-closed {raw_fail_closed}")

        _make_priority_fixture()
        next_ready = get_session_next_ready(SUPERVISOR, config=CFG)
        print("PASS next-ready-priority-ascending" if next_ready and next_ready.get("task_id") == f"{PREFIX}-task-2" else f"FAIL next-ready-priority-ascending {next_ready}")

        class HangingRedis:
            def sismember(self, *_args, **_kwargs):
                time.sleep(0.5)
                return True

        with mock.patch("fleet_orchestrator.config.get_redis_sync", return_value=HangingRedis()):
            timeout_flag = _stop_inprogress_enabled(SUPERVISOR, config=CFG)
        print("PASS stop-inprogress-redis-timeout-fail-open" if timeout_flag is False else f"FAIL stop-inprogress-redis-timeout-fail-open {timeout_flag}")

        _cleanup(PREFIX)
        flag_file = _write_flag_file(f'{{"{SUPERVISOR}":{{"enforce":true}}}}')
        os.environ["CF_HANDOFF_SESSION_FLAGS_FILE"] = flag_file
        os.environ.pop("CF_STOP_INPROGRESS", None)
        os.environ.pop("CF_STOP_INPROGRESS_SESSIONS", None)
        task_id = _make_in_progress_fixture(blocked_on=None)
        in_progress_gate = get_session_stop_decision(SUPERVISOR, config=CFG)
        print(
            "PASS peer-in-progress-gates-supervisor"
            if in_progress_gate.get("block") is True and in_progress_gate.get("task_id") == task_id and in_progress_gate.get("gate_for") == WORKER
            else f"FAIL peer-in-progress-gates-supervisor {in_progress_gate}"
        )

        _cleanup(PREFIX)
        flag_file = _write_flag_file('{}')
        os.environ["CF_HANDOFF_SESSION_FLAGS_FILE"] = flag_file
        os.environ["CF_STOP_INPROGRESS"] = "1"
        os.environ["CF_STOP_INPROGRESS_SESSIONS"] = SUPERVISOR
        task_id = _make_in_progress_fixture(owner=SUPERVISOR, blocked_on=None)
        in_progress_block_no_enforce = get_session_stop_decision(SUPERVISOR, config=CFG)
        print(
            "PASS in-progress-blocks-with-enforce-off"
            if in_progress_block_no_enforce.get("block") is True and in_progress_block_no_enforce.get("task_id") == task_id and in_progress_block_no_enforce.get("wake_type") == "WAKE_WITH_QUEUE"
            else f"FAIL in-progress-blocks-with-enforce-off {in_progress_block_no_enforce}"
        )

        _cleanup(PREFIX)
        flag_file = _write_flag_file(f'{{"{SUPERVISOR}":{{"enforce":true}}}}')
        os.environ["CF_HANDOFF_SESSION_FLAGS_FILE"] = flag_file
        os.environ["CF_STOP_INPROGRESS"] = "1"
        os.environ["CF_STOP_INPROGRESS_SESSIONS"] = SUPERVISOR
        task_id = _make_in_progress_fixture(blocked_on=None)
        in_progress_block = get_session_stop_decision(SUPERVISOR, config=CFG)
        print(
            "PASS in-progress-allowlisted-blocks"
            if in_progress_block.get("block") is True and in_progress_block.get("task_id") == task_id and in_progress_block.get("gate_for") == WORKER and in_progress_block.get("wake_type") == "WAKE_WITH_QUEUE"
            else f"FAIL in-progress-allowlisted-blocks {in_progress_block}"
        )

        _cleanup(PREFIX)
        flag_file = _write_flag_file(f'{{"{SUPERVISOR}":{{"enforce":true}}}}')
        os.environ["CF_HANDOFF_SESSION_FLAGS_FILE"] = flag_file
        os.environ["CF_STOP_INPROGRESS"] = "1"
        os.environ["CF_STOP_INPROGRESS_SESSIONS"] = SUPERVISOR
        task_id = _make_in_progress_fixture(owner=SUPERVISOR, blocked_on=WORKER)
        blocked_on_rejected = get_session_stop_decision(SUPERVISOR, config=CFG)
        print(
            "PASS in-progress-stale-blocked-on-fail-closed"
            if blocked_on_rejected.get("block") is True and blocked_on_rejected.get("task_id") == task_id and blocked_on_rejected.get("blocked_on_rejected") == WORKER and blocked_on_rejected.get("non_convergable") is True
            else f"FAIL in-progress-stale-blocked-on-fail-closed {blocked_on_rejected}"
        )

        with mock.patch("fleet_orchestrator.orch_schema._raw_stop_decision", return_value={"block": True, "wake_type": "WAKE_WITH_QUEUE", "task_id": "base-task", "reason": "base"}):
            with mock.patch("fleet_orchestrator.orch_schema.validate_stop_handoff", return_value={"state": "dead", "record": {"dispatcher_task_id": "hv-task"}}):
                base_block_wins = get_session_stop_decision(SUPERVISOR, config=CFG)
        print(
            "PASS handoff-dead-does-not-override-base-block"
            if base_block_wins.get("block") is True and base_block_wins.get("task_id") == "base-task"
            else f"FAIL handoff-dead-does-not-override-base-block {base_block_wins}"
        )

        _cleanup(PREFIX)
        flag_file = _write_flag_file(f'{{"{SUPERVISOR}":{{"enforce":true}}}}')
        os.environ["CF_HANDOFF_SESSION_FLAGS_FILE"] = flag_file
        os.environ["CF_STOP_INPROGRESS"] = "1"
        os.environ["CF_STOP_INPROGRESS_SESSIONS"] = SUPERVISOR
        _make_in_progress_fixture(owner=SUPERVISOR, blocked_on=None)
        convergence_results = [
            get_session_stop_decision(SUPERVISOR, stop_hook_active=True, config=CFG)
            for _ in range(3)
        ]
        converged = convergence_results[-1]
        print(
            "PASS convergence-limit-force-allows"
            if converged.get("block") is False and converged.get("converged_allow") is True and converged.get("wake_type") == "ALLOW_STOP"
            else f"FAIL convergence-limit-force-allows {convergence_results}"
        )

        class HangingMarkerRedis:
            def delete(self, *_args, **_kwargs):
                time.sleep(0.5)
                return 0

            def get(self, *_args, **_kwargs):
                time.sleep(0.5)
                return None

            def set(self, *_args, **_kwargs):
                time.sleep(0.5)
                return True

        with mock.patch("fleet_orchestrator.config.get_redis_sync", return_value=HangingMarkerRedis()):
            with mock.patch("fleet_orchestrator.orch_schema._raw_stop_decision", return_value={"block": True, "wake_type": "WAKE_WITH_QUEUE", "task_id": "task-marker", "reason": "marker"}):
                marker_fail_open = get_session_stop_decision(SUPERVISOR, stop_hook_active=True, config=CFG)
        print(
            "PASS convergence-marker-fail-open"
            if marker_fail_open.get("block") is True and marker_fail_open.get("convergence_marker_fail_open")
            else f"FAIL convergence-marker-fail-open {marker_fail_open}"
        )
        return 0
    finally:
        _cleanup(PREFIX)


if __name__ == "__main__":
    raise SystemExit(main())
