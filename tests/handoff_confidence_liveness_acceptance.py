#!/usr/bin/env python3
"""Acceptance: current-task status exposes derived peer handoff liveness.

The current-task surface must not report a blind hardcoded ``in_progress``.
It derives confidence at read time from notify Redis ``last_activity`` plus the
task dispatch timestamp. ORCH Redis is deliberately divergent here: notify-state
keys must be read through the notify_state accessor, not ORCH_REDIS.
"""
from __future__ import annotations

import contextlib
import importlib
import io
import os
import re
import sys
import time
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

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


def _require_divergent_redis() -> None:
    orch = (
        (os.environ.get("ORCH_REDIS_HOST") or "").strip(),
        (os.environ.get("ORCH_REDIS_PORT") or "").strip(),
    )
    notify = (
        (os.environ.get("REDIS_HOST") or "127.0.0.1").strip(),
        (os.environ.get("REDIS_PORT") or "6379").strip(),
    )
    if not orch[0] or not orch[1]:
        raise SystemExit("ORCH_REDIS_HOST/ORCH_REDIS_PORT are required")
    if orch == notify:
        raise SystemExit("handoff_confidence_liveness_acceptance requires divergent ORCH_REDIS and REDIS")


NAMESPACE = _require_test_namespace()
_require_divergent_redis()
PFX = f"{NAMESPACE}-handoff-live-{uuid.uuid4().hex[:8]}"
os.environ["NOTIFY_KEY_PREFIX"] = PFX
os.environ["ORCH_WORKER_TASK_LIVENESS"] = "1"
os.environ["ORCH_WORKER_TASK_LIVENESS_TTL_SEC"] = "30"

from fleet_orchestrator import dispatch as dispatch_module  # noqa: E402
from fleet_orchestrator.config import OrchConfig, get_neo4j_driver, get_redis_sync  # noqa: E402
from fleet_orchestrator.notify_state import redis_connect as notify_redis_connect  # noqa: E402
from fleet_orchestrator.notify_state import state_key  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    create_phase,
    create_project,
    create_task,
    init_schema,
)
from fleet_orchestrator.tasks_api import app  # noqa: E402

CFG = OrchConfig()
PROJECT = f"{PFX}-project"
PHASE = f"{PROJECT}::phase"
TASK = f"{PROJECT}::task"
SUPERVISOR = f"{PFX}-sup"
WORKER = f"{SUPERVISOR}-codex"
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _delete_matching(redis_client, *patterns: str) -> None:
    for pattern in patterns:
        cursor = 0
        while True:
            cursor, keys = redis_client.scan(cursor=cursor, match=pattern, count=100)
            if keys:
                redis_client.delete(*keys)
            if cursor == 0:
                break


def _cleanup() -> None:
    notify_r = notify_redis_connect()
    orch_r = get_redis_sync(CFG)
    _delete_matching(notify_r, f"{PFX}:*", f"{PFX}:worker-task-liveness:{PFX}*")
    _delete_matching(orch_r, f"{PFX}:*")
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=PFX)


def _api_current(client: TestClient) -> dict:
    response = client.get(f"/api/sessions/{WORKER}/current")
    if response.status_code >= 400:
        raise AssertionError(f"current endpoint failed HTTP {response.status_code}: {response.text}")
    return response.json()


def _liveness(client: TestClient) -> dict:
    payload = _api_current(client)
    return payload.get("liveness") or {}


def _set_dispatch_boundary(started_at: float, ttl_secs: int = 30) -> None:
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run(
            """
            MATCH (t:OrchTask {id: $task_id})
            SET t.worker_liveness_started_at = $started_at,
                t.worker_liveness_ttl_secs = $ttl_secs
            """,
            task_id=TASK,
            started_at=float(started_at),
            ttl_secs=int(ttl_secs),
        )


def _cli_current_text(client: TestClient) -> str:
    cli = importlib.import_module("fleet_orchestrator.cli_taey_plan")

    def api_call(method: str, endpoint: str, data=None):
        del data
        if method != "GET":
            raise AssertionError(f"unexpected method {method}")
        response = client.get(endpoint)
        if response.status_code >= 400:
            raise AssertionError(f"CLI API call failed HTTP {response.status_code}: {response.text}")
        return response.json()

    out = io.StringIO()
    with mock.patch.object(cli, "api_call", side_effect=api_call), contextlib.redirect_stdout(out):
        cli.cmd_current(SimpleNamespace(session=WORKER))
    return out.getvalue().strip()


def main() -> int:
    _cleanup()
    client = TestClient(app)
    notify_r = notify_redis_connect()
    orch_r = get_redis_sync(CFG)

    try:
        init_schema(config=CFG)
        create_project(project_id=PROJECT, name="handoff liveness acceptance", supervisor=SUPERVISOR, config=CFG)
        create_phase(project_id=PROJECT, phase_id=PHASE, name="phase", config=CFG)
        create_task(
            phase_id=PHASE,
            task_id=TASK,
            description="handoff liveness acceptance",
            owner=WORKER,
            priority=10,
            wake_owner_if_ready=False,
            config=CFG,
        )

        ok = SimpleNamespace(returncode=0, stdout="OK", stderr="")
        with mock.patch.object(dispatch_module.subprocess, "run", return_value=ok):
            dispatch_module.dispatch(WORKER, TASK, "handoff liveness acceptance", supervisor=SUPERVISOR)

        notify_r.delete(state_key(WORKER, "last_activity"))
        notify_r.delete(state_key(WORKER, "idle"))
        orch_r.set(state_key(WORKER, "last_activity"), str(time.time() + 30))
        liveness = _liveness(client)
        _check("no notify last_activity after dispatch => awaiting_start", liveness.get("state") == "awaiting_start", liveness)

        notify_r.set(state_key(WORKER, "last_activity"), str(time.time()))
        notify_r.delete(state_key(WORKER, "idle"))
        liveness = _liveness(client)
        _check("fresh notify last_activity after dispatch => working", liveness.get("state") == "working", liveness)

        notify_r.set(state_key(WORKER, "idle"), "1")
        notify_r.set(state_key(WORKER, "last_activity"), str(time.time()))
        liveness = _liveness(client)
        _check("codex idle flag plus fresh activity still => working", liveness.get("state") == "working", liveness)
        _check("raw idle detail remains surfaced", liveness.get("idle") is True, liveness)

        output = _cli_current_text(client)
        _check("taey-plan current prints derived state", "WORKING" in output, output)
        _check("taey-plan current no longer prints hardcoded in_progress", "(in_progress)" not in output, output)

        os.environ["ORCH_PUBLIC_SHOW_SESSIONS"] = f"{WORKER},{SUPERVISOR}"
        public_readonly = importlib.import_module("fleet_orchestrator.public_readonly")
        public_current = public_readonly._current_visible(WORKER)
        _check("public readonly current mirrors liveness", (public_current.get("liveness") or {}).get("state") == "working", public_current)

        old_start = time.time() - 10
        _set_dispatch_boundary(old_start, ttl_secs=1)
        notify_r.set(state_key(WORKER, "last_activity"), str(time.time() - 5))
        notify_r.set(state_key(WORKER, "idle"), "1")
        liveness = _liveness(client)
        _check("old activity after dispatch and past TTL => stale", liveness.get("state") == "stale", liveness)
    finally:
        _cleanup()

    if FAILURES:
        print(f"\nFAIL -- {len(FAILURES)}: {FAILURES}")
        return 1
    print("\nPASS -- current-task handoff liveness is derived from notify Redis activity.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
