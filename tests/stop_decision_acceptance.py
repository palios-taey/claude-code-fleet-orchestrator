#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PREFIX = f"hvstop-{uuid.uuid4().hex[:8]}"
SUPERVISOR = f"{PREFIX}-sup"
WORKER = f"{SUPERVISOR}-codex"
os.environ["NOTIFY_KEY_PREFIX"] = PREFIX
if "ORCH_DOTENV" not in os.environ:
    for candidate in (
        ROOT / ".env",
        Path.home() / "claude-code-fleet-orchestrator/.env",
    ):
        if candidate.is_file():
            os.environ["ORCH_DOTENV"] = str(candidate)
            break
from fleet_orchestrator.test_isolation import assert_acceptance_redis_isolated  # noqa: E402

assert_acceptance_redis_isolated()

from fleet_orchestrator.config import OrchConfig, get_neo4j_driver, get_redis_sync  # noqa: E402
import fleet_orchestrator.dispatch as dispatch_mod  # noqa: E402
from fleet_orchestrator.notify_state import redis_connect as notify_redis_connect  # noqa: E402
from fleet_orchestrator.notify_state import state_key  # noqa: E402
from fleet_orchestrator.orch_schema import create_phase, create_project, create_task, get_session_next_ready, get_session_stop_decision, update_task_status  # noqa: E402
from fleet_orchestrator.dispatch import record_outcome  # noqa: E402

CFG = OrchConfig()


def _cleanup(prefix: str) -> None:
    driver = get_neo4j_driver(CFG)
    with driver.session(database=CFG.neo4j_db) as session:
        session.run("MATCH (t:OrchTask) WHERE t.id STARTS WITH $prefix DETACH DELETE t", prefix=prefix)
        session.run("MATCH (ph:OrchPhase) WHERE ph.id STARTS WITH $prefix DETACH DELETE ph", prefix=prefix)
        session.run("MATCH (p:OrchProject) WHERE p.id STARTS WITH $prefix DETACH DELETE p", prefix=prefix)
    for r in (get_redis_sync(CFG), notify_redis_connect()):
        cursor = 0
        while True:
            cursor, keys = r.scan(cursor=cursor, match=f"{prefix}:*", count=100)
            if keys:
                r.delete(*keys)
            if cursor == 0:
                break


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


def _bind_current_task(worker: str, task_id: str) -> None:
    notify_redis_connect().set(
        state_key(worker, "current_task"),
        json.dumps({
            "task_id": task_id,
            "description": "owned in-progress task",
            "supervisor": SUPERVISOR,
            "dispatcher": SUPERVISOR,
            "started_at": time.time() - 60,
        }, separators=(",", ":")),
    )


def main() -> int:
    _cleanup(PREFIX)
    try:
        with mock.patch("fleet_orchestrator.orch_schema._raw_stop_decision", return_value={"block": False, "reason": None, "wake_type": "ALLOW_STOP", "task_id": None}):
            with mock.patch("fleet_orchestrator.orch_schema.validate_stop_handoff", side_effect=AssertionError("handoff validation must not gate stop"), create=True):
                handoff_not_gated = get_session_stop_decision(SUPERVISOR, config=CFG)
        print(
            "PASS handoff-validation-not-stop-gate"
            if handoff_not_gated.get("block") is False and "handoff_state" not in handoff_not_gated and "hv_fail_closed" not in handoff_not_gated
            else f"FAIL handoff-validation-not-stop-gate {handoff_not_gated}"
        )

        with mock.patch("fleet_orchestrator.orch_schema._raw_stop_decision", side_effect=RuntimeError("neo4j-boom")):
            raw_fail_closed = get_session_stop_decision(SUPERVISOR, config=CFG)
        print("PASS raw-stop-fail-closed" if raw_fail_closed.get("block") is True and raw_fail_closed.get("keystone_fail_closed") else f"FAIL raw-stop-fail-closed {raw_fail_closed}")

        _make_priority_fixture()
        next_ready = get_session_next_ready(SUPERVISOR, config=CFG)
        print("PASS next-ready-priority-ascending" if next_ready and next_ready.get("task_id") == f"{PREFIX}-task-2" else f"FAIL next-ready-priority-ascending {next_ready}")

        _cleanup(PREFIX)
        task_id = _make_in_progress_fixture(blocked_on=None)
        in_progress_gate = get_session_stop_decision(SUPERVISOR, config=CFG)
        print(
            "PASS peer-in-progress-gates-supervisor"
            if in_progress_gate.get("block") is True and in_progress_gate.get("task_id") == task_id and in_progress_gate.get("gate_for") == WORKER
            else f"FAIL peer-in-progress-gates-supervisor {in_progress_gate}"
        )

        _cleanup(PREFIX)
        task_id = _make_in_progress_fixture(owner=SUPERVISOR, blocked_on=None)
        in_progress_block_no_flags = get_session_stop_decision(SUPERVISOR, config=CFG)
        print(
            "PASS in-progress-blocks-without-flags"
            if in_progress_block_no_flags.get("block") is True and in_progress_block_no_flags.get("task_id") == task_id and in_progress_block_no_flags.get("wake_type") == "WAKE_WITH_QUEUE"
            else f"FAIL in-progress-blocks-without-flags {in_progress_block_no_flags}"
        )

        _cleanup(PREFIX)
        task_id = _make_in_progress_fixture(blocked_on=None)
        in_progress_block = get_session_stop_decision(SUPERVISOR, config=CFG)
        print(
            "PASS peer-in-progress-blocks"
            if in_progress_block.get("block") is True and in_progress_block.get("task_id") == task_id and in_progress_block.get("gate_for") == WORKER and in_progress_block.get("wake_type") == "WAKE_WITH_QUEUE"
            else f"FAIL peer-in-progress-blocks {in_progress_block}"
        )

        _cleanup(PREFIX)
        task_id = _make_in_progress_fixture(blocked_on=None)
        _bind_current_task(WORKER, task_id)
        with mock.patch.object(dispatch_mod, "_notify_supervisor_response_ready", return_value=None):
            record_outcome(WORKER, "done", "ready for r5 gate")
        peer_done_decision = get_session_stop_decision(WORKER, config=CFG)
        print(
            "PASS dispatched-peer-done-outcome-allows-stop"
            if peer_done_decision.get("block") is False
            and peer_done_decision.get("wake_type") == "ALLOW_STOP"
            and peer_done_decision.get("completed_current_outcome", {}).get("task_id") == task_id
            else f"FAIL dispatched-peer-done-outcome-allows-stop {peer_done_decision}"
        )
        supervisor_gate = get_session_stop_decision(SUPERVISOR, config=CFG)
        print(
            "PASS supervisor-still-gates-peer-done-outcome"
            if supervisor_gate.get("block") is True
            and supervisor_gate.get("task_id") == task_id
            and supervisor_gate.get("gate_for") == WORKER
            else f"FAIL supervisor-still-gates-peer-done-outcome {supervisor_gate}"
        )

        _cleanup(PREFIX)
        task_id = _make_in_progress_fixture(owner=SUPERVISOR, blocked_on=WORKER)
        blocked_on_rejected = get_session_stop_decision(SUPERVISOR, config=CFG)
        print(
            "PASS in-progress-stale-blocked-on-fail-closed"
            if blocked_on_rejected.get("block") is True and blocked_on_rejected.get("task_id") == task_id and blocked_on_rejected.get("blocked_on_rejected") == WORKER and blocked_on_rejected.get("non_convergable") is True
            else f"FAIL in-progress-stale-blocked-on-fail-closed {blocked_on_rejected}"
        )

        _cleanup(PREFIX)
        await_marker = "AWAIT:family-consent:weaver standalone consent"
        task_id = _make_in_progress_fixture(owner=SUPERVISOR, blocked_on=await_marker)
        await_decision = get_session_stop_decision(SUPERVISOR, config=CFG)
        print(
            "PASS in-progress-declared-await-signal-allows-stop"
            if await_decision.get("block") is False and await_decision.get("wake_type") == "ALLOW_STOP" and await_decision.get("awaiting_signal", {}).get("kind") == "family-consent"
            else f"FAIL in-progress-declared-await-signal-allows-stop {await_decision}"
        )
        update_task_status(task_id, "in_progress", owner=SUPERVISOR, blocked_on="", config=CFG)
        resolved_decision = get_session_stop_decision(SUPERVISOR, config=CFG)
        print(
            "PASS declared-await-clear-wakes-with-queue"
            if resolved_decision.get("block") is True and resolved_decision.get("wake_type") == "WAKE_WITH_QUEUE" and resolved_decision.get("task_id") == task_id
            else f"FAIL declared-await-clear-wakes-with-queue {resolved_decision}"
        )

        _cleanup(PREFIX)
        _make_priority_fixture()
        _make_in_progress_fixture(owner=SUPERVISOR, blocked_on=await_marker)
        ready_still_blocks = get_session_stop_decision(SUPERVISOR, config=CFG)
        print(
            "PASS ready-work-still-beats-declared-await"
            if ready_still_blocks.get("block") is True and ready_still_blocks.get("wake_type") == "WAKE_WITH_QUEUE" and ready_still_blocks.get("task_id") == f"{PREFIX}-task-2"
            else f"FAIL ready-work-still-beats-declared-await {ready_still_blocks}"
        )

        with mock.patch("fleet_orchestrator.orch_schema._raw_stop_decision", return_value={"block": True, "wake_type": "WAKE_WITH_QUEUE", "task_id": "base-task", "reason": "base"}):
            base_block_wins = get_session_stop_decision(SUPERVISOR, config=CFG)
        print(
            "PASS base-stop-block-still-wins"
            if base_block_wins.get("block") is True and base_block_wins.get("task_id") == "base-task"
            else f"FAIL base-stop-block-still-wins {base_block_wins}"
        )

        _cleanup(PREFIX)
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

        with mock.patch("fleet_orchestrator.orch_schema._fleet_state_redis", return_value=HangingMarkerRedis()):
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
