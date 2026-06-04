#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PREFIX = f"adhoc-{uuid.uuid4().hex[:8]}"
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

from lib.config import OrchConfig, get_neo4j_driver, get_redis_sync  # noqa: E402
from lib.orch_schema import ensure_default_project, create_task, get_session_current_work, get_task, update_task_status  # noqa: E402

CFG = OrchConfig()


def _cleanup(prefix: str) -> None:
    driver = get_neo4j_driver(CFG)
    with driver.session(database=CFG.neo4j_db) as session:
        session.run("MATCH (t:OrchTask) WHERE t.id STARTS WITH $prefix DETACH DELETE t", prefix=prefix)
    r = get_redis_sync(CFG)
    for suffix in ("current_task", "last_outcome"):
        cursor = 0
        while True:
            cursor, keys = r.scan(cursor=cursor, match=f"taey:{prefix}*:{suffix}", count=100)
            if keys:
                r.delete(*keys)
            if cursor == 0:
                break


def _set_current_task(session_id: str, task_id: str, description: str) -> None:
    r = get_redis_sync(CFG)
    r.set(
        f"taey:{session_id}:current_task",
        json.dumps({
            "task_id": task_id,
            "description": description,
            "supervisor": session_id,
            "started_at": time.time(),
        }),
    )
    r.delete(f"taey:{session_id}:last_outcome")


def main() -> int:
    _cleanup(PREFIX)
    try:
        phase_id = ensure_default_project(CFG)
        owner = f"{PREFIX}-codex"

        stale_one = f"{PREFIX}-stale-1"
        stale_two = f"{PREFIX}-stale-2"
        create_task(phase_id, stale_one, "stale one", owner=owner, priority=5, wake_owner_if_ready=False, config=CFG)
        create_task(phase_id, stale_two, "stale two", owner=owner, priority=6, wake_owner_if_ready=False, config=CFG)
        update_task_status(stale_one, "in_progress", owner=owner, config=CFG)
        update_task_status(stale_two, "in_progress", owner=owner, config=CFG)
        current = get_session_current_work(owner, config=CFG)
        stale_one_task = get_task(stale_one, config=CFG)
        stale_two_task = get_task(stale_two, config=CFG)
        print(
            "PASS no-live-current-closes-stale-ad-hoc"
            if current is None and stale_one_task and stale_one_task.get("status") == "interrupted" and stale_two_task and stale_two_task.get("status") == "interrupted"
            else f"FAIL no-live-current-closes-stale-ad-hoc current={current} stale_one={stale_one_task} stale_two={stale_two_task}"
        )

        live_keep = f"{PREFIX}-live-keep"
        stale_sibling = f"{PREFIX}-stale-sibling"
        create_task(phase_id, live_keep, "live keep", owner=owner, priority=4, wake_owner_if_ready=False, config=CFG)
        create_task(phase_id, stale_sibling, "stale sibling", owner=owner, priority=9, wake_owner_if_ready=False, config=CFG)
        update_task_status(live_keep, "in_progress", owner=owner, config=CFG)
        update_task_status(stale_sibling, "in_progress", owner=owner, config=CFG)
        _set_current_task(owner, live_keep, "live keep")
        current = get_session_current_work(owner, config=CFG)
        live_keep_task = get_task(live_keep, config=CFG)
        stale_sibling_task = get_task(stale_sibling, config=CFG)
        print(
            "PASS live-current-preserved-stale-sibling-closed"
            if (
                current
                and current.get("top_task_id") == live_keep
                and live_keep_task and live_keep_task.get("status") == "in_progress"
                and stale_sibling_task and stale_sibling_task.get("status") == "interrupted"
            )
            else f"FAIL live-current-preserved-stale-sibling-closed current={current} live={live_keep_task} stale={stale_sibling_task}"
        )
        return 0
    finally:
        _cleanup(PREFIX)


if __name__ == "__main__":
    raise SystemExit(main())
