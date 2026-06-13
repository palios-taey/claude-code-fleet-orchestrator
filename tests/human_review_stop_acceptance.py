#!/usr/bin/env python3
"""Ship-gate e2e -- human-review gates are first-class stop states.

A surfaced human-review gate is awaiting a person, not autonomous work. The
Stop hook and orch-watch must therefore agree: ALLOW_STOP with a human-review
reason when that is the only remaining non-terminal work, while normal peer
in-flight gate behavior stays unchanged.
"""
from __future__ import annotations

import importlib.util
import json
import os
import re
import sys
import time
import uuid
from importlib.machinery import SourceFileLoader
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fleet_orchestrator.config import OrchConfig, get_redis_sync  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    _PEER_HEARTBEAT_STALE_SEC,
    _raw_stop_decision,
    _state_key,
    create_human_review_gate,
    create_phase,
    create_project,
    create_task,
    get_neo4j_driver,
    get_project_ready_tasks,
    get_session_next_ready,
    init_schema,
    ready_work,
    set_project_stop_reason,
    set_project_user_stop_conditions,
    update_task_status,
)


def _require_test_namespace() -> str:
    raw = (os.environ.get("ORCH_TEST_NAMESPACE") or "").strip()
    if not raw:
        raise SystemExit("ORCH_TEST_NAMESPACE is required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{5,80}", raw):
        raise SystemExit("ORCH_TEST_NAMESPACE must be 6-80 chars of letters/digits/._:-")
    if not any(marker in raw.lower() for marker in ("test", "ci", "acceptance")):
        raise SystemExit("ORCH_TEST_NAMESPACE must include test, ci, or acceptance")
    return raw


CFG = OrchConfig()
PFX = f"{_require_test_namespace()}-hrstop-{uuid.uuid4().hex[:8]}"
SUP = f"{PFX}-sup"
PEER = f"{SUP}-codex"
REVIEWER = f"{PFX}-reviewer"
PROJECT = f"{PFX}-project"
PHASE = f"{PROJECT}::phase"
GATE = f"{PROJECT}::human-review"
QUESTION = f"{PFX}-question"
BARE_SUP = f"{PFX}-bare-sup"
BARE_PROJECT = f"{PFX}-bare-project"
BARE_PHASE = f"{BARE_PROJECT}::phase"
BARE_TASK = f"{BARE_PROJECT}::human-review-label-only"
ANSWERED_SUP = f"{PFX}-answered-sup"
ANSWERED_PROJECT = f"{PFX}-answered-project"
ANSWERED_PHASE = f"{ANSWERED_PROJECT}::phase"
ANSWERED_GATE = f"{ANSWERED_PROJECT}::human-review-answered"
ANSWERED_QUESTION = f"{PFX}-answered-question"
PEER_PROJECT = f"{PFX}-peer-project"
PEER_PHASE = f"{PEER_PROJECT}::phase"
PEER_TASK = f"{PEER_PROJECT}::peer-work"
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _cleanup() -> None:
    r = get_redis_sync(CFG)
    for owner in (SUP, PEER):
        for suffix in ("current_task", "idle", "last_activity", "last_tool_activity", "last_outcome"):
            r.delete(_state_key(owner, suffix))
    prefix = os.environ.get("NOTIFY_KEY_PREFIX", "taey")
    for kind in ("openq", "needs_you", "chat"):
        r.delete(f"{prefix}:{kind}:{REVIEWER}")
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=PFX)


def _load_orch_watch():
    path = ROOT / "scripts" / "orch-watch"
    loader = SourceFileLoader("orch_watch_under_test", str(path))
    spec = importlib.util.spec_from_loader("orch_watch_under_test", loader)
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load scripts/orch-watch")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _NoopRedis:
    def get(self, _key: str):
        return None


def _set_peer_done() -> None:
    r = get_redis_sync(CFG)
    r.delete(_state_key(PEER, "current_task"))
    r.set(_state_key(PEER, "idle"), "1")
    r.set(_state_key(PEER, "last_tool_activity"), str(time.time() - (_PEER_HEARTBEAT_STALE_SEC + 60)))
    r.set(_state_key(PEER, "last_outcome"), json.dumps({"outcome": "done", "details": f"DONE [{PEER_TASK}]"}))


def main() -> int:
    _cleanup()
    try:
        init_schema(config=CFG)
        create_project(project_id=PROJECT, name=PROJECT, supervisor=SUP, priority=1, config=CFG)
        create_phase(project_id=PROJECT, phase_id=PHASE, name="phase", config=CFG)
        create_human_review_gate(
            phase_id=PHASE,
            task_id=GATE,
            question_id=QUESTION,
            prompt="Review the stop discipline gate.",
            reviewer=REVIEWER,
            requested_by=SUP,
            notify=False,
            config=CFG,
        )
        conditions = set_project_user_stop_conditions(
            PROJECT,
            [{"label": "human-review", "active": True}],
            created_by=SUP,
            config=CFG,
        )

        decision = _raw_stop_decision(SUP, config=CFG)
        _check("human-review-only project allows stop", decision.get("wake_type") == "ALLOW_STOP" and decision.get("block") is False, decision)
        _check("human-review allow reason is explicit", "human review" in str(decision.get("reason") or "").lower(), decision)
        _check("human-review gate is not next ready work", get_session_next_ready(SUP, project_id=PROJECT, config=CFG) is None, get_session_next_ready(SUP, project_id=PROJECT, config=CFG))
        _check("human-review gate is not ready_work", ready_work(PROJECT, session_id=SUP, config=CFG) == [], ready_work(PROJECT, session_id=SUP, config=CFG))
        _check("project ready tasks exclude human-review", get_project_ready_tasks(PROJECT, owner=SUP, config=CFG) == [], get_project_ready_tasks(PROJECT, owner=SUP, config=CFG))

        create_project(project_id=BARE_PROJECT, name=BARE_PROJECT, supervisor=BARE_SUP, priority=1, config=CFG)
        create_phase(project_id=BARE_PROJECT, phase_id=BARE_PHASE, name="phase", config=CFG)
        create_task(
            phase_id=BARE_PHASE,
            task_id=BARE_TASK,
            description="human-review label without surfaced question",
            owner=BARE_SUP,
            task_type="human-review",
            wake_owner_if_ready=False,
            config=CFG,
        )
        update_task_status(BARE_TASK, "in_progress", owner=BARE_SUP, config=CFG)
        bare_decision = _raw_stop_decision(BARE_SUP, config=CFG)
        _check("bare-label human-review without question blocks", bare_decision.get("block") is True and bare_decision.get("task_id") == BARE_TASK, bare_decision)

        create_project(project_id=ANSWERED_PROJECT, name=ANSWERED_PROJECT, supervisor=ANSWERED_SUP, priority=1, config=CFG)
        create_phase(project_id=ANSWERED_PROJECT, phase_id=ANSWERED_PHASE, name="phase", config=CFG)
        create_human_review_gate(
            phase_id=ANSWERED_PHASE,
            task_id=ANSWERED_GATE,
            question_id=ANSWERED_QUESTION,
            prompt="Answered but task is still non-terminal.",
            reviewer=REVIEWER,
            requested_by=ANSWERED_SUP,
            notify=False,
            config=CFG,
        )
        with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
            session.run(
                """
                MATCH (q:OrchQuestion {id: $question_id})-[:CONCERNS_TASK]->(t:OrchTask {id: $task_id})
                SET q.status = 'answered',
                    t.status = 'in_progress'
                """,
                question_id=ANSWERED_QUESTION,
                task_id=ANSWERED_GATE,
            )
        answered_decision = _raw_stop_decision(ANSWERED_SUP, config=CFG)
        _check("answered human-review question with non-terminal task blocks", answered_decision.get("block") is True and answered_decision.get("task_id") == ANSWERED_GATE, answered_decision)

        watch = _load_orch_watch()
        _check(
            "orch-watch suppresses stop-gate wake for awaiting human review",
            watch._handle_user_stop_gate(_NoopRedis(), SUP, {"task_id": GATE}) is True,
            "orch-watch returned false",
        )

        condition = conditions[0]
        try:
            set_project_stop_reason(
                PROJECT,
                condition["id"],
                int(condition["version"]),
                "human review is surfaced",
                set_by=SUP,
                config=CFG,
            )
            stop_reason_ok = True
        except Exception as exc:
            stop_reason_ok = False
            stop_reason_detail = f"{type(exc).__name__}: {exc}"
        else:
            stop_reason_detail = "ok"
        _check("stop-reason set is not blocked by human-review ready_work", stop_reason_ok, stop_reason_detail)

        create_project(project_id=PEER_PROJECT, name=PEER_PROJECT, supervisor=SUP, priority=2, config=CFG)
        create_phase(project_id=PEER_PROJECT, phase_id=PEER_PHASE, name="phase", config=CFG)
        create_task(phase_id=PEER_PHASE, task_id=PEER_TASK, description="normal peer work", owner=PEER, wake_owner_if_ready=False, config=CFG)
        update_task_status(PEER_TASK, "in_progress", owner=PEER, config=CFG)
        _set_peer_done()
        peer_decision = _raw_stop_decision(SUP, config=CFG)
        _check("normal peer in-progress done still blocks for gate", peer_decision.get("block") is True and peer_decision.get("task_id") == PEER_TASK, peer_decision)
        _check("normal peer gate still names gate_for", peer_decision.get("gate_for") == PEER, peer_decision)
    finally:
        _cleanup()

    if FAILURES:
        print(f"\nFAIL -- {len(FAILURES)}: {FAILURES}")
        return 1
    print("\nPASS -- human-review gates are first-class stop states and normal peer gates still block.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
