#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
import uuid
from pathlib import Path


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


PFX = f"{_require_test_namespace()}-pause-{uuid.uuid4().hex[:8]}"
SUPERVISOR = f"{PFX}-sup"
os.environ["NOTIFY_KEY_PREFIX"] = PFX
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
os.environ.setdefault("ORCH_NEO4J_URI", "bolt://localhost:7687")
os.environ.setdefault("ORCH_NEO4J_DB", "neo4j")

from fleet_orchestrator.config import OrchConfig, get_neo4j_driver  # noqa: E402
from fleet_orchestrator.notify_state import redis_connect as notify_redis_connect  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    create_phase,
    create_project,
    create_task,
    get_session_stop_decision,
    set_session_pause,
)


CFG = OrchConfig()
FAILURES: list[str] = []


def _check(label: str, condition: bool, detail: object = "") -> None:
    print(("PASS " if condition else "FAIL ") + label)
    if not condition:
        FAILURES.append(f"{label}: {detail}")


def _pause_key(suffix: str) -> str:
    return f"{PFX}:{SUPERVISOR}:{suffix}"


def _cleanup() -> None:
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=PFX)
    r = notify_redis_connect()
    cursor = 0
    while True:
        cursor, keys = r.scan(cursor=cursor, match=f"{PFX}:*", count=100)
        if keys:
            r.delete(*keys)
        if cursor == 0:
            break


def _make_ready_project(label: str) -> str:
    project_id = f"{PFX}-{label}-project"
    phase_id = f"{project_id}::phase"
    task_id = f"{project_id}::task"
    create_project(project_id, f"{label} pause project", supervisor=SUPERVISOR, priority=1, config=CFG)
    create_phase(project_id, phase_id, "Main", config=CFG)
    create_task(phase_id, task_id, "ready work", owner=SUPERVISOR, priority=1, wake_owner_if_ready=False, config=CFG)
    return task_id


def _future_iso(seconds: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=seconds)).isoformat()


def _past_iso(seconds: float) -> str:
    return (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=seconds)).isoformat()


def main() -> int:
    _cleanup()
    try:
        ready_task = _make_ready_project("timed")
        set_session_pause(
            SUPERVISOR,
            pause_source="api",
            pause_reason="short maintenance",
            pause_expires_at=_future_iso(1),
            paused_by="acceptance",
            config=CFG,
        )
        active = get_session_stop_decision(SUPERVISOR, config=CFG)
        _check(
            "timed pause temporarily allows stop",
            active.get("block") is False and active.get("wake_type") == "ALLOW_STOP",
            active,
        )
        time.sleep(1.3)
        expired = get_session_stop_decision(SUPERVISOR, config=CFG)
        _check(
            "timed pause expires and stop engine resumes",
            expired.get("block") is True and expired.get("task_id") == ready_task,
            expired,
        )

        _cleanup()
        _make_ready_project("indefinite")
        set_session_pause(
            SUPERVISOR,
            pause_source="api",
            pause_reason="operator indefinite pause",
            paused_by="acceptance",
            config=CFG,
        )
        time.sleep(1.1)
        r = notify_redis_connect()
        indefinite = get_session_stop_decision(SUPERVISOR, config=CFG)
        _check(
            "indefinite pause persists without ttl",
            indefinite.get("block") is False
            and indefinite.get("wake_type") == "ALLOW_STOP"
            and r.ttl(_pause_key("pause")) == -1,
            {"decision": indefinite, "ttl": r.ttl(_pause_key("pause"))},
        )

        _cleanup()
        ready_task = _make_ready_project("stale")
        r = notify_redis_connect()
        r.set(_pause_key("pause"), "1")
        r.set(_pause_key("pause_meta"), json.dumps({"pause_expires_at": _past_iso(10)}))
        stale = get_session_stop_decision(SUPERVISOR, config=CFG)
        _check(
            "stale expired pause metadata does not short-circuit",
            stale.get("block") is True
            and stale.get("task_id") == ready_task
            and not r.exists(_pause_key("pause"))
            and not r.exists(_pause_key("pause_meta")),
            {
                "decision": stale,
                "pause_exists": r.exists(_pause_key("pause")),
                "meta_exists": r.exists(_pause_key("pause_meta")),
            },
        )
    finally:
        _cleanup()

    if FAILURES:
        print(f"FAIL - {len(FAILURES)} failure(s): {FAILURES}")
        return 1
    print("PASS - session pause expiry is fail-closed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
