#!/usr/bin/env python3
"""Acceptance: legacy [status:] plan tokens warn and do not abort ingest."""
from __future__ import annotations

import os
import re
import sys
import uuid
from pathlib import Path

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


PFX = f"{_require_test_namespace()}-status-token-{uuid.uuid4().hex[:8]}"
os.environ["NOTIFY_KEY_PREFIX"] = PFX
os.environ.setdefault("ORCH_SESSION_IDS", "conductor")

from fleet_orchestrator.config import OrchConfig, get_neo4j_driver, get_redis_sync  # noqa: E402
from fleet_orchestrator.notify_state import redis_connect as notify_redis_connect  # noqa: E402
from fleet_orchestrator.orch_schema import get_task, init_schema  # noqa: E402
from fleet_orchestrator.tasks_api import app  # noqa: E402


CFG = OrchConfig()
PROJECT = f"{PFX}-project"
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _delete_matching(r, pattern: str) -> None:
    cursor = 0
    while True:
        cursor, keys = r.scan(cursor=cursor, match=pattern, count=100)
        if keys:
            r.delete(*keys)
        if cursor == 0:
            break


def _cleanup() -> None:
    _delete_matching(get_redis_sync(CFG), f"{PFX}:*")
    _delete_matching(notify_redis_connect(), f"{PFX}:*")
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=PFX)


def _plan() -> str:
    return f"""# Project: {PROJECT} - Status Token Reingest
> legacy status tokens should not abort plan ingest.

## Phase: main - Main

### Task: completed-task - Completed token [owner: conductor-codex] [status:completed]
- body remains part of the description

### Task: failed-task - Failed token [status:failed] [owner: conductor-grok]
- this should still be pending

### Task: active-task - Active token [owner: conductor-gemini] [status:in_progress] [status:completed]
- multiple status tokens on one line are all ignored
"""


def main() -> int:
    _cleanup()
    try:
        init_schema(config=CFG)
        response = TestClient(app).post(
            "/api/projects/load-md",
            json={
                "md_text": _plan(),
                "source_kind": "markdown",
                "ingested_by": "status-token-acceptance",
                "supervisor": "conductor",
            },
        )
        body = response.json()
        warnings = [str(item) for item in body.get("warnings", [])]
        _check("plan ingest with status tokens returns HTTP 200", response.status_code == 200, body)
        _check("plan ingest reports no loader errors", body.get("errors") == [], body)
        _check("plan creates all three tasks", body.get("tasks_created") == 3, body)

        expected = {
            6: "completed",
            9: "failed",
            12: "in_progress",
        }
        for line_no, token in expected.items():
            _check(
                f"warning includes line {line_no} [{token}]",
                any(f"line {line_no}:" in warning and f"status:{token}" in warning for warning in warnings),
                warnings,
            )
        _check(
            "warning on line 12 includes both status tokens",
            any("line 12:" in warning and "status:in_progress" in warning and "status:completed" in warning for warning in warnings),
            warnings,
        )

        for bare_id in ("completed-task", "failed-task", "active-task"):
            task_id = f"{PROJECT}::{bare_id}"
            task = get_task(task_id, config=CFG)
            _check(f"{bare_id} created as pending", task.get("status") == "pending", task)
            _check(f"{bare_id} description does not retain status token", "status:" not in str(task.get("description") or ""), task)
    finally:
        _cleanup()

    if FAILURES:
        print(f"\nFAIL -- {len(FAILURES)}: {FAILURES}")
        return 1
    print("\nPASS -- [status:] tokens are ignored with warnings and tasks remain evidence-gated pending.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
