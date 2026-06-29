#!/usr/bin/env python3
"""Acceptance: LinkedIn-style single-owner loop governance stays one-step scoped."""
from __future__ import annotations

import os
import re
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest import mock
from zoneinfo import ZoneInfo


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


PFX = f"{_require_test_namespace()}-linkedin-gov-{uuid.uuid4().hex[:8]}"
os.environ["NOTIFY_KEY_PREFIX"] = PFX
os.environ["ORCH_SESSION_IDS"] = f"{PFX}-treasurer,{PFX}-linkedin,{PFX}-normal"
os.environ.setdefault("ORCH_REDIS_HOST", "127.0.0.1")
os.environ.setdefault("ORCH_REDIS_PORT", "6379")
os.environ.setdefault("ORCH_NEO4J_URI", "bolt://127.0.0.1:7687")
os.environ.setdefault("ORCH_NEO4J_DB", "neo4j")

from fleet_orchestrator import cli_orch_cron as cron  # noqa: E402
import fleet_orchestrator.context_assembler as assembler  # noqa: E402
import fleet_orchestrator.dispatch as dispatch_module  # noqa: E402
from fleet_orchestrator.config import OrchConfig, get_neo4j_driver  # noqa: E402
from fleet_orchestrator.notify_state import redis_connect as notify_redis_connect  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    _auto_heal_stuck_step_blocked_on,
    add_dependency,
    create_phase,
    create_project,
    create_task,
    get_session_next_ready,
    get_session_stop_decision,
    get_task,
    init_schema,
    update_task_status,
)


CFG = OrchConfig()
TREASURER = f"{PFX}-treasurer"
LINKEDIN = f"{PFX}-linkedin"
NORMAL = f"{PFX}-normal"
PROJECT = f"{PFX}-hourly-linkedin-loop"
PHASE = f"{PROJECT}::cycle"
NORMAL_PROJECT = f"{PFX}-normal-project"
NORMAL_PHASE = f"{NORMAL_PROJECT}::phase"
STEPS = [f"{PROJECT}::step-{idx}" for idx in range(1, 7)]
MAX_OPERATING_BYTES = 950
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


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


def _complete(task_id: str) -> None:
    update_task_status(
        task_id,
        "completed",
        owner=LINKEDIN,
        completed_by=LINKEDIN,
        completion_evidence={"production_observation": f"{task_id} completed in acceptance"},
        config=CFG,
    )


def _setup_linkedin_chain() -> None:
    create_project(PROJECT, "hourly-linkedin-loop", supervisor=TREASURER, priority=1, config=CFG)
    create_phase(PROJECT, PHASE, "cycle", config=CFG)
    for idx, task_id in enumerate(STEPS, start=1):
        description = (
            f"LinkedIn loop step {idx}. "
            + ("Use the current step only; route later-surface work. " * (idx + 4))
        )
        create_task(
            PHASE,
            task_id,
            description,
            owner=LINKEDIN,
            priority=idx * 10,
            wake_owner_if_ready=False,
            config=CFG,
        )
        if idx > 1:
            add_dependency(task_id, STEPS[idx - 2], config=CFG)


def _setup_normal_task() -> str:
    task_id = f"{NORMAL_PROJECT}::work"
    create_project(NORMAL_PROJECT, "normal project", supervisor=NORMAL, priority=10, config=CFG)
    create_phase(NORMAL_PROJECT, NORMAL_PHASE, "normal", config=CFG)
    create_task(
        NORMAL_PHASE,
        task_id,
        "normal in-progress task",
        owner=NORMAL,
        priority=1,
        wake_owner_if_ready=False,
        config=CFG,
    )
    update_task_status(task_id, "in_progress", owner=NORMAL, blocked_on="waiting on normal prose", config=CFG)
    return task_id


def _operating_section(rendered: str) -> str:
    if "## Operating" not in rendered or "\n## Identity" not in rendered:
        return ""
    return rendered.split("## Operating", 1)[1].split("\n## Identity", 1)[0]


def _wake_packet_for(session: str) -> str:
    with mock.patch.object(assembler, "get_overall_refs", return_value={"ref_context": {"refs": []}}), \
         mock.patch.object(assembler, "get_supervisor_refs", return_value={"ref_context": {"refs": []}}):
        context = assembler.select_context(session, cli="codex")
    return assembler.assemble(assembler.build_packet(session, context), "codex")


def _ok_run(args: list[str], **_kwargs: object) -> SimpleNamespace:
    if args and args[:3] == ["git", "rev-parse", "HEAD"]:
        return SimpleNamespace(returncode=0, stdout="acceptance-head\n", stderr="")
    return SimpleNamespace(returncode=0, stdout="", stderr="")


def main() -> int:
    _cleanup()
    tmp = Path(tempfile.mkdtemp(prefix="linkedin-gov-"))
    try:
        init_schema(config=CFG)
        _setup_linkedin_chain()

        first = get_session_next_ready(LINKEDIN, project_id=PROJECT, config=CFG)
        _check("one-step dispatch exposes only step 1 first", first and first.get("task_id") == STEPS[0], first)
        _complete(STEPS[0])
        second = get_session_next_ready(LINKEDIN, project_id=PROJECT, config=CFG)
        _check("one-step dispatch advances to step 2 only after step 1 closes", second and second.get("task_id") == STEPS[1], second)
        update_task_status(STEPS[1], "in_progress", owner=LINKEDIN, config=CFG)
        no_downstream = get_session_next_ready(LINKEDIN, project_id=PROJECT, config=CFG)
        _check("downstream stays locked while step 2 in progress", no_downstream is None, no_downstream)

        rendered = _wake_packet_for(LINKEDIN)
        operating = _operating_section(rendered)
        _check("wake packet reinjects current step", f"CURRENT STEP: `{STEPS[1]}`" in operating, operating)
        _check("wake packet locks later surfaces", "Later steps are LOCKED" in operating and "do NOT act" in operating, operating)
        _check("wake packet step guidance stays bounded",
               len(operating.encode("utf-8")) <= MAX_OPERATING_BYTES,
               len(operating.encode("utf-8")))

        update_task_status(STEPS[1], "in_progress", owner=LINKEDIN, blocked_on="stale prose from prior cycle", config=CFG)
        healed = get_session_stop_decision(LINKEDIN, config=CFG)
        healed_task = get_task(STEPS[1], config=CFG)
        _check("auto-heal clears free-text blocked_on for stuck chain step",
               healed.get("auto_healed_blocked_on", {}).get("cleared_blocked_on") == "stale prose from prior cycle"
               and healed_task.get("blocked_on") in (None, ""),
               {"decision": healed, "task": healed_task})

        update_task_status(STEPS[1], "in_progress", owner=LINKEDIN,
                           blocked_on="AWAIT:human-review:operator decision", config=CFG)
        direct_await_heal = _auto_heal_stuck_step_blocked_on(
            STEPS[1],
            "AWAIT:human-review:operator decision",
            config=CFG,
        )
        direct_await_task = get_task(STEPS[1], config=CFG)
        _check("direct auto-heal guard ignores structured AWAIT",
               direct_await_heal is None
               and direct_await_task.get("blocked_on") == "AWAIT:human-review:operator decision",
               {"healed": direct_await_heal, "task": direct_await_task})
        await_decision = get_session_stop_decision(LINKEDIN, config=CFG)
        await_task = get_task(STEPS[1], config=CFG)
        _check("structured AWAIT remains parked and untouched",
               await_decision.get("block") is False
               and await_decision.get("awaiting_signal", {}).get("kind") == "human-review"
               and await_task.get("blocked_on") == "AWAIT:human-review:operator decision",
               {"decision": await_decision, "task": await_task})

        parallel_task = f"{PROJECT}::parallel-ready"
        create_task(
            PHASE,
            parallel_task,
            "Parallel ready task proves stale blocked_on is not cleared when next-ready exists.",
            owner=LINKEDIN,
            priority=5,
            wake_owner_if_ready=False,
            config=CFG,
        )
        update_task_status(STEPS[1], "in_progress", owner=LINKEDIN,
                           blocked_on="stale prose but ready work exists", config=CFG)
        ready_wins = get_session_stop_decision(LINKEDIN, config=CFG)
        still_blocked = get_task(STEPS[1], config=CFG)
        _check("auto-heal does not clear when same-project next-ready exists",
               not ready_wins.get("auto_healed_blocked_on")
               and still_blocked.get("blocked_on") == "stale prose but ready work exists",
               {"decision": ready_wins, "task": still_blocked})
        update_task_status(parallel_task, "completed", owner=LINKEDIN, completed_by=LINKEDIN,
                           completion_evidence={"production_observation": "parallel ready guard satisfied"},
                           config=CFG)
        update_task_status(STEPS[1], "in_progress", owner=LINKEDIN, blocked_on="", config=CFG)
        now = datetime.now(ZoneInfo("UTC")).replace(second=0, microsecond=0)
        trigger = {
            "id": "linkedin-cycle",
            "session": LINKEDIN,
            "supervisor": TREASURER,
            "project": PROJECT,
            "description": "Run LinkedIn loop",
            "tz": "UTC",
            "minute": now.minute,
            "hours": [now.hour],
            "state_file": str(tmp / "state.jsonl"),
            "enabled": True,
        }
        with mock.patch.object(dispatch_module, "hook_installation_status", return_value=SimpleNamespace(ok=True, detail="hooked")), \
             mock.patch.object(dispatch_module.subprocess, "run", side_effect=_ok_run):
            skipped = cron.fire_trigger(notify_redis_connect(), trigger, now)
        _check("cron skips reset while cycle in flight", skipped == "skipped:cycle_in_flight", skipped)
        _check("cron skip does not clobber in-progress step", get_task(STEPS[1], config=CFG).get("status") == "in_progress", get_task(STEPS[1], config=CFG))

        normal_task = _setup_normal_task()
        normal_decision = get_session_stop_decision(NORMAL, config=CFG)
        normal_after = get_task(normal_task, config=CFG)
        _check("normal non-chain session does not auto-heal free-text blocked_on",
               normal_decision.get("blocked_on_rejected") == "waiting on normal prose"
               and normal_after.get("blocked_on") == "waiting on normal prose",
               {"decision": normal_decision, "task": normal_after})
    finally:
        _cleanup()

    if FAILURES:
        print(f"\nFAIL -- {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS -- LinkedIn loop governance is one-step, wake-reinjected, auto-healed, and reset-safe.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
