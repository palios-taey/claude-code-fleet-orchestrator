#!/usr/bin/env python3
"""Acceptance: dispatch claim writes registered *-codex owners, never bare strip.

Regression (task-c648d6fd): claim/redispatch with missing or legacy resolution
rewrote owner to bare family via seat_family suffix strip. CONTROL close then
failed because supervisor-scoped queries key on exact owner == session_id.

Env: ORCH_NEO4J_URI, ORCH_NEO4J_DB, ORCH_TEST_NAMESPACE (required).
"""
from __future__ import annotations

import os
import re
import sys
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
    lowered = raw.lower()
    if not any(marker in lowered for marker in ("test", "ci", "acceptance")):
        raise SystemExit("ORCH_TEST_NAMESPACE must include test, ci, or acceptance")
    if lowered in {"prod", "production", "neo4j", "default"}:
        raise SystemExit("ORCH_TEST_NAMESPACE must not name a production/default namespace")
    return raw


_NAMESPACE = _require_test_namespace()
_PFX = f"{_NAMESPACE}-owner-cp-{uuid.uuid4().hex[:8]}"
SUP = f"{_PFX}-seat-codex"
WORKER = f"{_PFX}-seat-grok"
BARE = f"{_PFX}-seat"
PROJECT = f"{_PFX}-project"
PHASE = f"{PROJECT}::phase"
TASK = f"{PROJECT}::task"
os.environ["ORCH_SESSION_IDS"] = f"{SUP},weaver-codex"

from fleet_orchestrator.config import OrchConfig, get_neo4j_driver  # noqa: E402
from fleet_orchestrator.dispatch import _claim_ready_orch_task  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    create_phase,
    create_project,
    create_task,
    init_schema,
)

CFG = OrchConfig()
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _cleanup() -> None:
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=_PFX)


def _owner() -> str:
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        row = session.run(
            "MATCH (t:OrchTask {id:$id}) RETURN t.owner AS owner, t.dispatched_to AS dispatched_to, t.status AS status",
            id=TASK,
        ).single()
    assert row is not None
    return str(row["owner"] or "")


def main() -> int:
    init_schema(config=CFG)
    _cleanup()
    try:
        create_project(PROJECT, "owner control principal", supervisor=SUP, priority=10, config=CFG)
        create_phase(PROJECT, PHASE, "phase", config=CFG)
        create_task(PHASE, TASK, "claim ownership", owner=SUP, priority=1, config=CFG)

        _claim_ready_orch_task(TASK, WORKER, supervisor=SUP)
        _check("fresh claim owner is registered *-codex control", _owner() == SUP, _owner())

        with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
            session.run(
                "MATCH (t:OrchTask {id:$id}) SET t.owner=$bare, t.dispatched_to=$worker, t.status='in_progress'",
                id=TASK,
                bare=BARE,
                worker=WORKER,
            )
        _check("fixture planted bare-family owner", _owner() == BARE, _owner())

        _claim_ready_orch_task(TASK, WORKER, supervisor=SUP)
        _check(
            "same-worker redispatch repairs bare owner to registered control",
            _owner() == SUP,
            _owner(),
        )
    finally:
        _cleanup()

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - dispatch ownership writes registered control principals only.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
