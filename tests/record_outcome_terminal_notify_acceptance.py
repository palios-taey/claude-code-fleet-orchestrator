"""Acceptance: record_outcome wakes supervisors for every terminal outcome.

Issue #195: the done branch sent response_ready immediately, but error and
interrupted only wrote last_outcome and reverted the task to pending. The
supervisor could stay asleep until a later Stop-hook or liveness path happened.

This test proves record_outcome itself is sufficient:
  - bind_current_task registers worker-liveness at bind time;
  - done/error/interrupted each emit response_ready to the binding supervisor;
  - error/interrupted revert the OrchTask claim to pending and clear current_task.

Env: ORCH_NEO4J_URI, ORCH_NEO4J_DB, ORCH_REDIS_HOST/PORT,
     ORCH_TEST_NAMESPACE (required; must include test/ci/acceptance).
"""
from __future__ import annotations

import json
import os
import re
import sys
import uuid
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fleet_orchestrator.dispatch as dispatch_module  # noqa: E402
from fleet_orchestrator.config import OrchConfig  # noqa: E402
from fleet_orchestrator.dispatch import (  # noqa: E402
    _redis_connect,
    _state_key,
    bind_current_task,
    record_outcome,
)
from fleet_orchestrator.orch_schema import (  # noqa: E402
    create_phase,
    create_project,
    create_task,
    get_neo4j_driver,
    init_schema,
)
from fleet_orchestrator.worker_liveness import worker_task_liveness_key  # noqa: E402


def _require_test_namespace() -> str:
    raw = (os.environ.get("ORCH_TEST_NAMESPACE") or "").strip()
    if not raw:
        raise SystemExit("ORCH_TEST_NAMESPACE is required for issue #195 acceptance")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{5,80}", raw):
        raise SystemExit("ORCH_TEST_NAMESPACE must be 6-80 chars of letters/digits/._:-")
    lowered = raw.lower()
    if not any(marker in lowered for marker in ("test", "ci", "acceptance")):
        raise SystemExit("ORCH_TEST_NAMESPACE must include test, ci, or acceptance")
    if lowered in {"prod", "production", "neo4j", "default"}:
        raise SystemExit("ORCH_TEST_NAMESPACE must not name a production/default namespace")
    return raw


CFG = OrchConfig()
NAMESPACE = _require_test_namespace()
PFX = f"{NAMESPACE}-outcome195-{uuid.uuid4().hex[:8]}"
SUP = f"{PFX}-sup"
OWNER = f"{PFX}-worker"
PEER = f"{OWNER}-grok"
PROJECT = f"{PFX}-project"
PHASE = f"{PROJECT}::phase"
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _driver():
    return get_neo4j_driver(CFG)


def _cleanup() -> None:
    redis_client = _redis_connect()
    for suffix in ("current_task", "last_outcome", "parent", "idle", "last_tool_activity"):
        redis_client.delete(_state_key(PEER, suffix))
    prefix = os.environ.get("NOTIFY_KEY_PREFIX", "taey")
    for pattern in (
        f"{prefix}:worker-task-liveness:{PFX}*",
        f"{prefix}:worker-task-liveness-escalated:{PFX}*",
    ):
        cursor = 0
        while True:
            cursor, keys = redis_client.scan(cursor=cursor, match=pattern, count=100)
            if keys:
                redis_client.delete(*keys)
            if cursor == 0:
                break
    with _driver().session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=PFX)


def _task_row(task_id: str) -> dict:
    with _driver().session(database=CFG.neo4j_db) as session:
        record = session.run(
            """
            MATCH (t:OrchTask {id: $task_id})
            RETURN t.status AS status,
                   t.dispatched_to AS dispatched_to,
                   t.worker_liveness_worker AS worker_liveness_worker,
                   t.worker_liveness_supervisor AS worker_liveness_supervisor,
                   t.worker_liveness_started_at AS worker_liveness_started_at,
                   t.worker_liveness_heartbeat_at AS worker_liveness_heartbeat_at,
                   t.worker_liveness_ttl_secs AS worker_liveness_ttl_secs
            """,
            task_id=task_id,
        ).single()
    return dict(record) if record else {}


def _response_ready_call(call_args: list[str], outcome: str, task_id: str) -> bool:
    return (
        len(call_args) >= 2
        and call_args[1] == SUP
        and "--from" in call_args
        and call_args[call_args.index("--from") + 1] == PEER
        and "--type" in call_args
        and call_args[call_args.index("--type") + 1] == "response_ready"
        and "--priority" in call_args
        and call_args[call_args.index("--priority") + 1] == "high"
        and outcome in call_args[2]
        and task_id in call_args[2]
    )


def main() -> int:
    init_schema(config=CFG)
    _cleanup()
    try:
        create_project(project_id=PROJECT, name=PROJECT, supervisor=SUP, config=CFG)
        create_phase(project_id=PROJECT, phase_id=PHASE, name="phase", config=CFG)
        ok = SimpleNamespace(returncode=0, stdout="OK", stderr="")

        for outcome in ("done", "error", "interrupted"):
            task_id = f"{PFX}::{outcome}"
            create_task(
                phase_id=PHASE,
                task_id=task_id,
                description=f"{outcome} terminal outcome",
                owner=OWNER,
                wake_owner_if_ready=False,
                config=CFG,
            )
            started_at = bind_current_task(
                PEER,
                task_id,
                f"{outcome} terminal outcome",
                supervisor=SUP,
                set_parent=False,
            )
            row = _task_row(task_id)
            _check(f"{outcome}: bind marks task in_progress", row.get("status") == "in_progress", row)
            _check(f"{outcome}: bind records liveness worker", row.get("worker_liveness_worker") == PEER, row)
            _check(f"{outcome}: bind records liveness supervisor", row.get("worker_liveness_supervisor") == SUP, row)
            _check(
                f"{outcome}: bind records liveness heartbeat",
                float(row.get("worker_liveness_heartbeat_at") or 0.0) == float(started_at),
                row,
            )
            sidecar_raw = _redis_connect().get(worker_task_liveness_key(task_id))
            sidecar = json.loads(sidecar_raw) if sidecar_raw else {}
            _check(f"{outcome}: bind writes liveness sidecar", sidecar.get("task_id") == task_id, sidecar)

            notify_calls: list[list[str]] = []

            def fake_run(args, **_kwargs):
                notify_calls.append(list(args))
                return ok

            with mock.patch.object(dispatch_module.subprocess, "run", side_effect=fake_run):
                record_outcome(PEER, outcome, f"{outcome} acceptance verdict")

            last_outcome_raw = _redis_connect().get(_state_key(PEER, "last_outcome"))
            last_outcome = json.loads(last_outcome_raw) if last_outcome_raw else {}
            _check(f"{outcome}: last_outcome stored", last_outcome.get("outcome") == outcome, last_outcome)
            _check(f"{outcome}: last_outcome carries task id", last_outcome.get("task_id") == task_id, last_outcome)
            _check(
                f"{outcome}: record_outcome sends supervisor response_ready immediately",
                any(_response_ready_call(call, outcome, task_id) for call in notify_calls),
                notify_calls,
            )
            after = _task_row(task_id)
            current_after = _redis_connect().get(_state_key(PEER, "current_task"))
            if outcome == "done":
                _check("done: record_outcome does not self-complete task", after.get("status") == "in_progress", after)
                _check("done: current_task remains for Stop hook cleanup", bool(current_after), current_after)
            else:
                _check(f"{outcome}: record_outcome reverts claim to pending", after.get("status") == "pending", after)
                _check(f"{outcome}: record_outcome clears dispatched_to", after.get("dispatched_to") is None, after)
                _check(f"{outcome}: record_outcome clears current_task", not current_after, current_after)
    finally:
        _cleanup()

    if FAILURES:
        print(f"\nFAIL -- {len(FAILURES)}: {FAILURES}")
        return 1
    print("\nPASS -- record_outcome wakes supervisors for done/error/interrupted and bind registers liveness.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
