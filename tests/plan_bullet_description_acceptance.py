#!/usr/bin/env python3
"""Acceptance: plan task bullet descriptions retain readable list structure."""
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


PFX = f"{_require_test_namespace()}-bullets-{uuid.uuid4().hex[:8]}"
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
    return f"""# Project: {PROJECT} - Bullet Description Acceptance
> task bullets should stay readable.

## Phase: main - Main

### Task: step-1 - Inbound-first
- Notifications -> triage invites
- For each: draft reply
- Messaging -> answer warm threads
- My Network -> review people
- Record result in the dashboard
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
                "ingested_by": "bullet-description-acceptance",
                "supervisor": "conductor",
            },
        )
        body = response.json()
        _check("plan ingest returns HTTP 200", response.status_code == 200, body)
        _check("plan ingest reports no loader errors", body.get("errors") == [], body)
        _check("plan creates the task", body.get("tasks_created") == 1, body)

        task = get_task(f"{PROJECT}::step-1", config=CFG)
        description = str(task.get("description") or "")
        lines = description.splitlines()
        bullet_lines = [line for line in lines if line.startswith("- ")]
        _check("stored description keeps header line", lines[:1] == ["Inbound-first"], lines)
        _check("stored description has one line per bullet", len(bullet_lines) == 5, lines)
        _check("stored bullet markers are retained", bullet_lines == [
            "- Notifications -> triage invites",
            "- For each: draft reply",
            "- Messaging -> answer warm threads",
            "- My Network -> review people",
            "- Record result in the dashboard",
        ], lines)
        _check("stored description is not a run-on block", "Notifications -> triage invites - For each" not in description, description)

        css = (ROOT / "ui" / "static" / "app.css").read_text(encoding="utf-8")
        _check("dashboard task descriptions preserve newlines", ".task-description" in css and "white-space: pre-wrap" in css, "ui/static/app.css")
    finally:
        _cleanup()

    if FAILURES:
        print(f"\nFAIL -- {len(FAILURES)}: {FAILURES}")
        return 1
    print("\nPASS -- plan task bullets store and render as readable list lines.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
