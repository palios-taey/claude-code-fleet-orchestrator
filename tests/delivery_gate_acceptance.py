#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import sys
import uuid
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

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
os.environ.setdefault("ORCH_SESSION_IDS", "delivery-supervisor,delivery-worker")

from fleet_orchestrator.config import OrchConfig, get_neo4j_driver  # noqa: E402
from fleet_orchestrator.orch_schema import CompletionEvidenceError, create_task, get_task, init_schema  # noqa: E402
from fleet_orchestrator.plan_loader import load_plan_from_text  # noqa: E402
from fleet_orchestrator.tasks_api import app  # noqa: E402


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
PFX = f"{_require_test_namespace()}-delivery-{uuid.uuid4().hex[:8]}"
SUP = "delivery-supervisor"
PROJECT = f"{PFX}-project"
PHASE = f"{PROJECT}::main"
GATED_DELIVER = f"{PROJECT}::gated-deliver"
GATED_NOOP = f"{PROJECT}::gated-noop"
GATED_REJECT = f"{PROJECT}::gated-reject"
DIRECT_GATE = f"{PROJECT}::direct-gate"
NORMAL_EVIDENCE = f"{PROJECT}::normal-evidence"
NORMAL_DELIVERY_ONLY = f"{PROJECT}::normal-delivery-only"
FAILURES: list[str] = []


def _cleanup() -> None:
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=PFX)


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _load_plan() -> dict:
    md = f"""# Project: {PROJECT} - Delivery gate acceptance
> Validates delivery-gated task completion evidence.

## Phase: main - Main [order: 1]

### Task: gated-deliver - Must record delivery [owner: {SUP}] [delivery_gate: true]

### Task: gated-noop - Must record checked no-op [owner: {SUP}] [delivery_gate: true]

### Task: gated-reject - Reject hollow completion [owner: {SUP}] [delivery_gate: true]

### Task: normal-evidence - Normal task still accepts normal evidence [owner: {SUP}]

### Task: normal-delivery-only - Normal task still rejects delivery-only evidence [owner: {SUP}]
"""
    return load_plan_from_text(
        md,
        source_path="",
        source_kind="acceptance",
        ingested_by=SUP,
        supervisor=SUP,
        config=CFG,
    )


def main() -> int:
    _cleanup()
    client = TestClient(app)
    try:
        init_schema(config=CFG)
        ingest = _load_plan()
        _check("plan ingest succeeds", ingest.get("project_id") == PROJECT and not ingest.get("errors"), ingest)
        _check("delivery_gate metadata persisted true", get_task(GATED_DELIVER, config=CFG).get("delivery_gate") is True, get_task(GATED_DELIVER, config=CFG))
        _check("normal task has no delivery gate", get_task(NORMAL_EVIDENCE, config=CFG).get("delivery_gate") in (None, False), get_task(NORMAL_EVIDENCE, config=CFG))
        create_task(
            phase_id=PHASE,
            task_id=DIRECT_GATE,
            description="Direct create can opt into delivery gate",
            owner=SUP,
            delivery_gate=True,
            wake_owner_if_ready=False,
            config=CFG,
        )
        _check("direct create can persist delivery_gate true", get_task(DIRECT_GATE, config=CFG).get("delivery_gate") is True, get_task(DIRECT_GATE, config=CFG))

        terminal_initial_error = ""
        try:
            create_task(
                phase_id=PHASE,
                task_id=f"{PROJECT}::terminal-initial",
                description="Terminal initial status is rejected through delivery gate",
                owner=SUP,
                initial_status="completed",
                delivery_gate=True,
                wake_owner_if_ready=False,
                config=CFG,
            )
        except CompletionEvidenceError as exc:
            terminal_initial_error = str(exc)
        except Exception as exc:
            terminal_initial_error = f"{type(exc).__name__}: {exc}"
        _check(
            "delivery-gated terminal initial status uses delivery evidence rejection",
            "delivery-gated" in terminal_initial_error and "missing evidence" in terminal_initial_error,
            terminal_initial_error,
        )

        no_evidence = client.patch(f"/api/task/{GATED_REJECT}", json={"status": "completed", "from": SUP})
        _check("delivery-gated completion rejects missing evidence", no_evidence.status_code == 400 and "delivery-gated" in no_evidence.text, no_evidence.text)

        normal_only = client.patch(
            f"/api/task/{GATED_REJECT}",
            json={
                "status": "completed",
                "from": SUP,
                "evidence": {"production_observation": "verified ordinary completion only"},
            },
        )
        _check("delivery-gated completion rejects normal-only evidence", normal_only.status_code == 400 and "delivered" in normal_only.text and "no_op" in normal_only.text, normal_only.text)

        hollow = client.patch(
            f"/api/task/{GATED_REJECT}",
            json={"status": "completed", "from": SUP, "evidence": {"delivered": {}}},
        )
        _check("delivery-gated completion rejects hollow delivered object", hollow.status_code == 400 and "non-empty" in hollow.text, hollow.text)

        unchecked_noop = client.patch(
            f"/api/task/{GATED_REJECT}",
            json={
                "status": "completed",
                "from": SUP,
                "evidence": {"no_op": {"reason": "nothing to deliver", "checked": False}},
            },
        )
        _check("delivery-gated completion rejects unchecked no-op", unchecked_noop.status_code == 400 and "checked" in unchecked_noop.text, unchecked_noop.text)

        too_short_noop = client.patch(
            f"/api/task/{GATED_REJECT}",
            json={
                "status": "completed",
                "from": SUP,
                "evidence": {"no_op": {"reason": "x", "checked": True}},
            },
        )
        _check(
            "delivery-gated completion rejects too-short no-op reason",
            too_short_noop.status_code == 400 and "at least 8" in too_short_noop.text,
            too_short_noop.text,
        )

        both = client.patch(
            f"/api/task/{GATED_REJECT}",
            json={
                "status": "completed",
                "from": SUP,
                "evidence": {
                    "delivered": {"careers_db_delta": {"rows_changed": 1}},
                    "no_op": {"reason": "ambiguous", "checked": True},
                },
            },
        )
        _check("delivery-gated completion rejects both outcomes", both.status_code == 400 and "exactly one" in both.text, both.text)
        _check("delivery-gated rejection attempts do not close task", get_task(GATED_REJECT, config=CFG).get("status") != "completed", get_task(GATED_REJECT, config=CFG))

        delivered = client.patch(
            f"/api/task/{GATED_DELIVER}",
            json={
                "status": "completed",
                "from": SUP,
                "evidence": {"delivered": {"careers_db_delta": {"rows_changed": 1}}},
            },
        )
        delivered_task = get_task(GATED_DELIVER, config=CFG)
        _check(
            "delivery-gated task accepts concrete delivered evidence",
            delivered.status_code == 200
            and delivered_task.get("status") == "completed"
            and delivered_task.get("completion_evidence", {}).get("delivered", {}).get("careers_db_delta", {}).get("rows_changed") == 1,
            {"response": delivered.text, "task": delivered_task},
        )

        noop = client.patch(
            f"/api/task/{GATED_NOOP}",
            json={
                "status": "completed",
                "from": SUP,
                "evidence": {"no_op": {"reason": "  source had no fresh rows  ", "checked": True}},
            },
        )
        noop_task = get_task(GATED_NOOP, config=CFG)
        _check(
            "delivery-gated task accepts checked no-op evidence",
            noop.status_code == 200
            and noop_task.get("status") == "completed"
            and noop_task.get("completion_evidence", {}).get("no_op", {}).get("reason") == "source had no fresh rows"
            and noop_task.get("completion_evidence", {}).get("no_op", {}).get("checked") is True,
            {"response": noop.text, "task": noop_task},
        )

        normal_ok = client.patch(
            f"/api/task/{NORMAL_EVIDENCE}",
            json={
                "status": "completed",
                "from": SUP,
                "evidence": {"production_observation": "verified normal completion still works"},
            },
        )
        normal_task = get_task(NORMAL_EVIDENCE, config=CFG)
        _check(
            "normal task still accepts standard completion evidence",
            normal_ok.status_code == 200
            and normal_task.get("status") == "completed"
            and normal_task.get("completion_evidence", {}).get("production_observation") == "verified normal completion still works",
            {"response": normal_ok.text, "task": normal_task},
        )

        normal_delivery_only = client.patch(
            f"/api/task/{NORMAL_DELIVERY_ONLY}",
            json={
                "status": "completed",
                "from": SUP,
                "evidence": {"delivered": {"careers_db_delta": {"rows_changed": 1}}},
            },
        )
        _check(
            "normal task rejects delivery-only evidence outside opt-in gate",
            normal_delivery_only.status_code == 400
            and get_task(NORMAL_DELIVERY_ONLY, config=CFG).get("status") != "completed",
            {"response": normal_delivery_only.text, "task": get_task(NORMAL_DELIVERY_ONLY, config=CFG)},
        )
    finally:
        _cleanup()
    if FAILURES:
        print("\nFAIL -- delivery-gated completion evidence contract regressed:")
        for failure in FAILURES:
            print(f" - {failure}")
        return 1
    print("\nPASS -- delivery-gated steps require delivered/no-op evidence and normal completions are unchanged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
