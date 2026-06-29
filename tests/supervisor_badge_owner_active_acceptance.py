#!/usr/bin/env python3
"""Acceptance: owner-executing work keeps that session's supervisor badge active."""
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PFX = f"badge-owner-active-{uuid.uuid4().hex[:8]}"
SUPERVISOR = f"{PFX}-treasurer"
OWNER = f"{PFX}-linkedin"
IDLE = f"{PFX}-idle"
PROJECT = f"{PFX}-project"
PHASE = f"{PROJECT}::phase"
TASK = f"{PROJECT}::connection-growth"

os.environ["NOTIFY_KEY_PREFIX"] = PFX
os.environ["ORCH_SESSION_IDS"] = f"{SUPERVISOR},{OWNER},{IDLE}"

from fleet_orchestrator.config import OrchConfig, get_redis_sync  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    create_phase,
    create_project,
    create_task,
    get_neo4j_driver,
    get_supervisor_badges,
    init_schema,
    update_task_status,
)


CFG = OrchConfig()
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _cleanup() -> None:
    redis_client = get_redis_sync(CFG)
    keys = list(redis_client.scan_iter(f"{PFX}:*"))
    if keys:
        redis_client.delete(*keys)
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run(
            """
            MATCH (n)
            WHERE coalesce(n.id, '') STARTS WITH $prefix
               OR coalesce(n.session, '') STARTS WITH $prefix
            DETACH DELETE n
            """,
            prefix=PFX,
        )


def main() -> int:
    _cleanup()
    try:
        init_schema(config=CFG)
        create_project(project_id=PROJECT, name=PROJECT, supervisor=SUPERVISOR, priority=1, config=CFG)
        create_phase(project_id=PROJECT, phase_id=PHASE, name="phase", config=CFG)
        create_task(
            phase_id=PHASE,
            task_id=TASK,
            description="owner executes work in a differently supervised project",
            owner=OWNER,
            wake_owner_if_ready=False,
            config=CFG,
        )
        update_task_status(TASK, "in_progress", owner=OWNER, config=CFG)

        badges = get_supervisor_badges(config=CFG)
        owner_badge = badges.get(OWNER, {})
        supervisor_badge = badges.get(SUPERVISOR, {})
        idle_badge = badges.get(IDLE, {})

        _check("owner badge exists", bool(owner_badge), badges)
        _check(
            "owner executing another supervisor's task is ACTIVE",
            owner_badge.get("state") == "ACTIVE",
            owner_badge,
        )
        _check(
            "owner badge records owned in-progress task",
            owner_badge.get("own_in_progress_count") == 1,
            owner_badge,
        )
        _check(
            "owner badge does not need supervised open work",
            owner_badge.get("open_task_count") == 0,
            owner_badge,
        )
        _check(
            "owner badge names owned in-progress reason",
            "owned_in_progress" in (owner_badge.get("reasons") or []),
            owner_badge,
        )
        _check(
            "project supervisor still sees supervised open work",
            supervisor_badge.get("state") == "ACTIVE"
            and supervisor_badge.get("open_task_count") == 1
            and supervisor_badge.get("in_progress_count") == 1,
            supervisor_badge,
        )
        _check(
            "session without owned or supervised work stays IDLE",
            idle_badge.get("state") == "IDLE"
            and idle_badge.get("open_task_count") == 0
            and idle_badge.get("own_in_progress_count") == 0,
            idle_badge,
        )
    finally:
        _cleanup()

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)}: {FAILURES}")
        return 1
    print("\nPASS - owner-executing sessions stay active without stealing supervisor badge credit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
