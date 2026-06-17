#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PREFIX = f"noflag-{uuid.uuid4().hex[:8]}"
SESSION = f"{PREFIX}-codex"
WORKER = f"{PREFIX}-gemini"
os.environ["NOTIFY_KEY_PREFIX"] = PREFIX
os.environ["CF_HANDOFF_ENFORCE"] = "0"
os.environ["CF_HANDOFF_ENFORCE_SESSIONS"] = ""
os.environ["CF_STOP_INPROGRESS"] = "0"
os.environ["CF_STOP_INPROGRESS_SESSIONS"] = ""
os.environ["CF_HANDOFF_VALIDATE_TIMEOUT_S"] = "1.0"
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

from fleet_orchestrator.config import OrchConfig, get_neo4j_driver, get_redis_sync  # noqa: E402
from fleet_orchestrator.handoff_validation import handoff_key  # noqa: E402
from fleet_orchestrator.orch_schema import create_phase, create_project, create_task, get_session_stop_decision, init_schema, update_task_status  # noqa: E402

CFG = OrchConfig()
FAILURES: list[str] = []


def _check(label: str, condition: bool, detail: object = "") -> None:
    print(("PASS " if condition else "FAIL ") + label)
    if not condition:
        FAILURES.append(f"{label}: {detail}")


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


def _set_current_task(session_id: str, task_id: str) -> None:
    r = get_redis_sync(CFG)
    r.set(
        f"{PREFIX}:{session_id}:current_task",
        json.dumps({"task_id": task_id}, separators=(",", ":")),
    )


def _make_task(owner: str, *, blocked_on: str | None = None) -> str:
    project_id = f"{PREFIX}-project-{uuid.uuid4().hex[:6]}"
    phase_id = f"{PREFIX}-phase-{uuid.uuid4().hex[:6]}"
    task_id = f"{PREFIX}-task-{uuid.uuid4().hex[:6]}"
    create_project(project_id, "no flag bypass", supervisor=SESSION, priority=1, config=CFG)
    create_phase(project_id, phase_id, "Main", config=CFG)
    create_task(phase_id, task_id, "no bypass task", owner=owner, priority=5, wake_owner_if_ready=False, config=CFG)
    update_task_status(task_id, "in_progress", owner=owner, blocked_on=blocked_on, config=CFG)
    return task_id


def _write_disabled_handoff_flag_file() -> str:
    handle = tempfile.NamedTemporaryFile("w", delete=False, suffix=".json")
    json.dump({SESSION: {"enforce": False, "ack_passive": False}}, handle)
    handle.flush()
    handle.close()
    return handle.name


def _write_pending_handoff(task_id: str) -> None:
    now = time.time()
    msg_id = str(uuid.uuid4())
    record = {
        "kind": "explicit_handoff",
        "dispatcher_session_id": SESSION,
        "target_session_id": WORKER,
        "dispatcher_task_id": task_id,
        "msg_id": msg_id,
        "message_hash": "hash-1",
        "actionable_inputs": {},
        "created_at": now - 30,
        "ack_deadline_at": now + 300,
        "ack_backstop_at": now + 300,
        "pickup_poll_budget": 5,
        "delivery_state": "queued",
        "delivery_poll_count": 0,
    }
    get_redis_sync(CFG).set(
        handoff_key(PREFIX, SESSION, msg_id),
        json.dumps(record, separators=(",", ":")),
    )


def _assert_runtime_code_has_no_bypass_flags() -> None:
    runtime_text = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "fleet_orchestrator").glob("*.py")
    )
    forbidden = [
        "CF_HANDOFF_ENFORCE",
        "CF_HANDOFF_SESSION_FLAGS_FILE",
        "CF_STOP_INPROGRESS",
        "stop_inprogress_enabled",
        "flags_for_session",
    ]
    present = [needle for needle in forbidden if needle in runtime_text]
    _check("runtime-code-has-no-flag-bypass-surfaces", not present, present)


def main() -> int:
    init_schema(config=CFG)
    _cleanup(PREFIX)
    try:
        flag_file = _write_disabled_handoff_flag_file()
        os.environ["CF_HANDOFF_SESSION_FLAGS_FILE"] = flag_file

        task_id = _make_task(SESSION, blocked_on="AWAIT:external-signal:no flag bypass test")
        _set_current_task(SESSION, task_id)
        _write_pending_handoff(task_id)
        handoff_decision = get_session_stop_decision(SESSION, config=CFG)
        _check(
            "per-session-optout-cannot-disable-handoff-validation",
            handoff_decision.get("block") is True
            and handoff_decision.get("handoff_state") == "pending_unacked"
            and handoff_decision.get("task_id") == task_id,
            handoff_decision,
        )

        _cleanup(PREFIX)
        get_redis_sync(CFG).delete(f"{PREFIX}:stop_inprogress_enabled")
        task_id = _make_task(SESSION, blocked_on=None)
        in_progress_decision = get_session_stop_decision(SESSION, config=CFG)
        _check(
            "per-session-optout-cannot-disable-stop-in-progress",
            in_progress_decision.get("block") is True
            and in_progress_decision.get("task_id") == task_id
            and in_progress_decision.get("wake_type") == "WAKE_WITH_QUEUE",
            in_progress_decision,
        )

        _assert_runtime_code_has_no_bypass_flags()
    finally:
        _cleanup(PREFIX)

    if FAILURES:
        print(f"FAIL — {len(FAILURES)} failure(s): {FAILURES}")
        return 1
    print("PASS — stop discipline has no per-session flag bypass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
