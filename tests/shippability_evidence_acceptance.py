#!/usr/bin/env python3
"""Acceptance: shippability requires completed ship-gates to carry valid evidence."""
from __future__ import annotations

import json
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
    if not any(marker in raw.lower() for marker in ("test", "ci", "acceptance")):
        raise SystemExit("ORCH_TEST_NAMESPACE must include test, ci, or acceptance")
    return raw


PFX = f"{_require_test_namespace()}-ship-evidence-{uuid.uuid4().hex[:8]}"
os.environ["ORCH_SHIP_GATES"] = "audit"

from fleet_orchestrator.config import OrchConfig, get_neo4j_driver  # noqa: E402
from fleet_orchestrator.orch_schema import create_phase, create_project, create_task, init_schema, _normalize_completion_evidence, CompletionEvidenceError  # noqa: E402
from fleet_orchestrator.shippability import evaluate_shippability  # noqa: E402


CFG = OrchConfig()
PROJECT = f"{PFX}-project"
PHASE = f"{PROJECT}::phase"
GATE = f"{PROJECT}::audit"
NONGATE_PROJECT = f"{PFX}-nogate-project"
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _cleanup() -> None:
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=PFX)


def _seed_project(project_id: str, task_id: str) -> None:
    phase_id = f"{project_id}::phase"
    create_project(project_id, "shippability evidence project", supervisor="conductor", priority=1, config=CFG)
    create_phase(project_id, phase_id, "Main", config=CFG)
    create_task(
        phase_id,
        task_id,
        "ship gate",
        owner="conductor",
        priority=1,
        wake_owner_if_ready=False,
        config=CFG,
    )


def _force_completed(task_id: str, evidence: object) -> None:
    encoded = json.dumps(evidence, separators=(",", ":"), sort_keys=True) if evidence is not None else None
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run(
            """
            MATCH (t:OrchTask {id: $task_id})
            SET t.status = 'completed',
                t.completion_evidence = $evidence,
                t.updated_at = datetime()
            """,
            task_id=task_id,
            evidence=encoded,
        )


def main() -> int:
    _cleanup()
    try:
        init_schema(config=CFG)
        _seed_project(PROJECT, GATE)

        _force_completed(GATE, None)
        missing = evaluate_shippability(PROJECT, config=CFG)
        _check("completed gate without evidence is not shippable", missing.get("shippable") is False, missing)
        _check("evidence-less gate is listed as incomplete", len(missing.get("incomplete_gates") or []) == 1, missing)
        _check(
            "evidence-less gate reason is explicit",
            any(gate.get("reason") == "completed without evidence" for gate in missing.get("incomplete_gates") or []),
            missing,
        )
        _check("failure reason mentions evidence", "evidence" in str(missing.get("reason") or "").lower(), missing)

        _force_completed(GATE, {"production_observation": "verified shippability evidence acceptance"})
        valid = evaluate_shippability(PROJECT, config=CFG)
        _check("completed gate with valid evidence is shippable", valid.get("shippable") is True, valid)
        _check("valid evidence leaves no incomplete gates", valid.get("incomplete_gates") == [], valid)
        _check("success reason says valid evidence", "valid evidence" in str(valid.get("reason") or ""), valid)

        # ADVERSARIAL: shape-check accepts fabricated (documents it is NOT verification)
        fabricated = {"commit_sha": "deadbeef", "production_observation": "fabricated, never ran"}
        try:
            norm = _normalize_completion_evidence(fabricated)
            _check("ADVERSARIAL: fabricated-but-shape-valid evidence is ACCEPTED by _normalize (shape filter, NOT provenance verification)", norm is not None and norm.get("commit_sha") == "deadbeef", norm)
        except Exception as e:
            _check("ADVERSARIAL: fabricated-but-shape-valid evidence is ACCEPTED by _normalize (shape filter, NOT provenance verification)", False, str(e))

        # trivial junk rejected
        try:
            _normalize_completion_evidence({"production_observation": "x"})
            _check("ADVERSARIAL: junk evidence 'x' is REJECTED", False)
        except CompletionEvidenceError:
            _check("ADVERSARIAL: junk evidence 'x' is REJECTED by shape check", True)
        try:
            _normalize_completion_evidence({})
            _check("ADVERSARIAL: empty evidence dict for completed is REJECTED", False)
        except CompletionEvidenceError:
            _check("ADVERSARIAL: empty evidence dict for completed is REJECTED by shape check", True)

        _seed_project(NONGATE_PROJECT, f"{NONGATE_PROJECT}::not-a-gate")
        no_gates = evaluate_shippability(NONGATE_PROJECT, config=CFG)
        _check("project with no configured ship-gates still fails closed", no_gates.get("shippable") is False, no_gates)
        _check("no-gates reason remains fail-closed", "fail-closed" in str(no_gates.get("reason") or ""), no_gates)
    finally:
        _cleanup()

    if FAILURES:
        print(f"\nFAIL -- {len(FAILURES)}: {FAILURES}")
        return 1
    print("\nPASS -- shippability requires completed ship-gates to carry valid evidence.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
