#!/usr/bin/env python3
"""Acceptance: ship is verdict-only and disabled loops are not success-looking."""
from __future__ import annotations

import json
import os
import re
import sys
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


PFX = f"{_require_test_namespace()}-ship-loop-{uuid.uuid4().hex[:8]}"
os.environ["ORCH_SHIP_GATES"] = "audit"

from fleet_orchestrator.config import OrchConfig, get_neo4j_driver  # noqa: E402
from fleet_orchestrator.loop_engine import advance_loop_step, declare_loop  # noqa: E402
from fleet_orchestrator.orch_schema import create_phase, create_project, create_task, init_schema  # noqa: E402
import fleet_orchestrator.tasks_api as tasks_api  # noqa: E402

CFG = OrchConfig()
CLIENT = TestClient(tasks_api.app)
PROJECT = f"{PFX}-project"
PHASE = f"{PROJECT}::phase"
GATE = f"{PROJECT}::audit"
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _cleanup() -> None:
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE coalesce(n.id, '') STARTS WITH $prefix DETACH DELETE n", prefix=PFX)


def _complete_gate() -> None:
    evidence = {"production_observation": "ship loop semantics acceptance"}
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run(
            """
            MATCH (t:OrchTask {id: $task_id})
            SET t.status = 'completed',
                t.completion_evidence = $evidence,
                t.updated_at = datetime()
            """,
            task_id=GATE,
            evidence=json.dumps(evidence, separators=(",", ":"), sort_keys=True),
        )


def _seed_shippable_project() -> None:
    create_project(PROJECT, "ship loop semantics project", supervisor="conductor", priority=1, config=CFG)
    create_phase(PROJECT, PHASE, "Main", config=CFG)
    create_task(
        PHASE,
        GATE,
        "audit ship gate",
        owner="conductor",
        priority=1,
        wake_owner_if_ready=False,
        config=CFG,
    )
    _complete_gate()


def _ship_state_keys() -> tuple[str, list[str]]:
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        record = session.run(
            """
            MATCH (p:OrchProject {id: $project_id})
            RETURN coalesce(p.status, '') AS status,
                   [key IN keys(p) WHERE key IN [
                       'shipped', 'shipped_at', 'shipped_by', 'ship_status', 'ship_action'
                   ]] AS ship_keys
            """,
            project_id=PROJECT,
        ).single()
    if not record:
        return "", ["project missing"]
    return str(record["status"]), list(record["ship_keys"] or [])


def _base_loop() -> dict:
    return {
        "id": f"{PFX}-loop",
        "owner": "conductor",
        "trigger": {"kind": "clock", "clock_signal": "orch-watch-tick"},
        "step_bundle": [{"step": "observe", "writes_state": []}],
        "cycle_state": {},
        "swap_slots": {},
        "stop_condition": {"var": "cycle_state.done", "op": "==", "value": True},
    }


def _ship_contract() -> None:
    _seed_shippable_project()
    response = CLIENT.post(f"/api/projects/{PROJECT}/ship")
    body = response.json()
    _check("ship verdict returns 200", response.status_code == 200, body)
    _check("ship verdict is explicitly non-mutating", body.get("action") == "verdict" and body.get("shipped") is False, body)
    _check("ship verdict keeps shippable verdict", body.get("shippable") is True and body.get("verdict", {}).get("shippable") is True, body)
    status, ship_keys = _ship_state_keys()
    _check("ship does not persist shipped-state keys", ship_keys == [], {"status": status, "ship_keys": ship_keys})
    _check("ship does not complete or mutate project status", status == "active", status)


def _disabled_loop_contract() -> None:
    old_enabled = os.environ.get("ORCH_LOOPS_ENABLED")
    os.environ["ORCH_LOOPS_ENABLED"] = "0"
    expected = {"ok": False, "enabled": False, "reason": "loops disabled"}
    try:
        declare_response = CLIENT.post("/api/loops/declare", json={"loop": _base_loop()})
        advance_response = CLIENT.post(f"/api/loops/{PFX}-loop/advance", json={"step": "observe"})
        stop_response = CLIENT.get(f"/api/loops/{PFX}-loop/should-stop")
        _check("disabled loop declare API is not success-looking", declare_response.status_code == 200 and declare_response.json() == expected, declare_response.text)
        _check("disabled loop advance API is not success-looking", advance_response.status_code == 200 and advance_response.json() == expected, advance_response.text)
        _check("disabled loop should-stop API is not success-looking", stop_response.status_code == 200 and stop_response.json() == expected, stop_response.text)
        _check("disabled direct declare helper is not success-looking", declare_loop(_base_loop()) == expected)
        _check("disabled direct advance helper is not success-looking", advance_loop_step(_base_loop(), "observe") == expected)
        _check("disabled response has no assembler error field", "error" not in expected, expected)
    finally:
        if old_enabled is None:
            os.environ.pop("ORCH_LOOPS_ENABLED", None)
        else:
            os.environ["ORCH_LOOPS_ENABLED"] = old_enabled


def _assembler_error_contract() -> None:
    with mock.patch.object(tasks_api, "_cfg", return_value=SimpleNamespace(session_ids=["conductor-codex"])), \
         mock.patch.object(tasks_api, "select_wake_context", side_effect=RuntimeError("assembler boom")):
        failed = CLIENT.get("/api/sessions/conductor-codex/wake-packet?cli=codex")
    body = failed.json()
    _check(
        "assembler error remains distinguishable from clean disabled loop",
        failed.status_code == 200 and body.get("ok") is False and body.get("enabled") is True and "assembler boom" in body.get("error", ""),
        body,
    )


def main() -> int:
    init_schema(config=CFG)
    _cleanup()
    try:
        _ship_contract()
        _disabled_loop_contract()
        _assembler_error_contract()
    finally:
        _cleanup()
    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - ship verdict and disabled loop semantics are self-describing.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
