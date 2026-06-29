#!/usr/bin/env python3
from __future__ import annotations

import importlib
import json
import os
import hashlib
import sys
import uuid
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PREFIX = f"evidence-{uuid.uuid4().hex[:8]}"
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

from fleet_orchestrator.config import OrchConfig, get_neo4j_driver  # noqa: E402
from fleet_orchestrator.evidence_contract import TERMINAL_STATUSES  # noqa: E402
from fleet_orchestrator.orch_schema import create_phase, create_project, create_task  # noqa: E402
from fleet_orchestrator.tasks_api import app  # noqa: E402

CFG = OrchConfig()


def _cleanup(prefix: str) -> None:
    driver = get_neo4j_driver(CFG)
    with driver.session(database=CFG.neo4j_db) as session:
        session.run("MATCH (t:OrchTask) WHERE t.id STARTS WITH $prefix DETACH DELETE t", prefix=prefix)
        session.run("MATCH (ph:OrchPhase) WHERE ph.id STARTS WITH $prefix DETACH DELETE ph", prefix=prefix)
        session.run("MATCH (p:OrchProject) WHERE p.id STARTS WITH $prefix DETACH DELETE p", prefix=prefix)


def _seed_task(suffix: str = "task") -> str:
    project_id = f"{PREFIX}-project"
    phase_id = f"{PREFIX}-phase"
    task_id = f"{PREFIX}-{suffix}"
    create_project(project_id, "evidence project", supervisor="tester", priority=1, config=CFG)
    create_phase(project_id, phase_id, "Main", config=CFG)
    create_task(
        phase_id,
        task_id,
        "evidence completion task",
        owner="tester-codex",
        priority=5,
        wake_owner_if_ready=False,
        config=CFG,
    )
    return task_id


def _load_taey_task_cli():
    return importlib.import_module("fleet_orchestrator.cli_taey_task")


def _cli_update_round_trip(client: TestClient, task_id: str, status: str, evidence: dict) -> tuple[int, str]:
    cli = _load_taey_task_cli()

    def api_call(method: str, endpoint: str, data=None):
        if method == "PATCH":
            response = client.patch(endpoint, json=data)
        elif method == "GET":
            response = client.get(endpoint)
        else:
            raise AssertionError(f"unexpected CLI method {method}")
        if response.status_code >= 400:
            raise AssertionError(f"CLI API call failed HTTP {response.status_code}: {response.text}")
        return response.json()

    argv = [
        "taey-task",
        "update",
        task_id,
        status,
        "--evidence",
        json.dumps(evidence),
    ]
    with mock.patch.object(cli, "api_call", side_effect=api_call), \
         mock.patch.object(cli, "detect_from_node", return_value=f"cli-{status}"), \
         mock.patch.object(sys, "argv", argv):
        try:
            cli.main()
        except SystemExit as exc:
            return int(exc.code or 0), status
    return 0, status


def main() -> int:
    _cleanup(PREFIX)
    client = TestClient(app)
    failures = []

    def check(label: str, cond: bool, extra: str = "") -> None:
        print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {extra}"))
        if not cond:
            failures.append(label)

    try:
        task_id = _seed_task()
        # --- rejection edge cases (GAIA/Clarity ws0 audit) — each must 400, none may persist ---
        rejections = [
            ("reject status 'Completed' (case bypass #2)", {"status": "Completed", "from": "t"}),
            ("reject status 'done' (synonym bypass #2)", {"status": "done", "from": "t"}),
            ("reject unknown status 'finished'", {"status": "finished", "from": "t"}),
            ("reject evidence commit_sha=0 (junk #5)", {"status": "completed", "from": "t", "evidence": {"commit_sha": 0}}),
            ("reject evidence production_observation=false (junk #5)", {"status": "completed", "from": "t", "evidence": {"production_observation": False}}),
            ("reject malformed commit_sha 'x' (#5 format)", {"status": "completed", "from": "t", "evidence": {"commit_sha": "x"}}),
            ("reject malformed commit_sha '0' string (#5 format)", {"status": "completed", "from": "t", "evidence": {"commit_sha": "0"}}),
            ("reject too-short production_observation 'ok' (#5 format)", {"status": "completed", "from": "t", "evidence": {"production_observation": "ok"}}),
            ("reject too-short gate_run_id 'x' (#5 format)", {"status": "completed", "from": "t", "evidence": {"gate_run_id": "x"}}),
            ("reject empty-dict evidence", {"status": "completed", "from": "t", "evidence": {}}),
            ("reject unknown-key-only evidence", {"status": "completed", "from": "t", "evidence": {"foo": "bar"}}),
            ("reject empty outbound_actions evidence", {"status": "completed", "from": "t", "evidence": {"outbound_actions": []}}),
            ("reject evidence on in_progress (wrong transition)", {"status": "in_progress", "from": "t", "evidence": {"commit_sha": "x"}}),
            ("reject completed without evidence", {"status": "completed", "from": "t"}),
        ]
        for label, body in rejections:
            r = client.patch(f"/api/task/{task_id}", json=body)
            check(label, r.status_code == 400, f"got {r.status_code} {r.text[:90]}")
        st = client.get(f"/api/tasks/{task_id}").json().get("status")
        check("no rejection persisted (task still not completed)", st != "completed", f"status={st}")

        # --- success: a single valid evidence key completes + persists ---
        ok = client.patch(
            f"/api/task/{task_id}",
            json={"status": "completed", "from": "tester-api",
                  "evidence": {"production_observation": "verified in acceptance"}},
        )
        payload = client.get(f"/api/tasks/{task_id}").json()
        check(
            "completed-with-single-evidence-persists",
            ok.status_code == 200 and ok.json().get("ok") is True
            and payload.get("status") == "completed"
            and payload.get("completed_by") == "tester-api"
            and payload.get("completion_evidence", {}).get("production_observation") == "verified in acceptance",
            f"update={ok.status_code} payload={payload}",
        )

        outbound_hash = hashlib.sha256(b"hello from a gated outbound action").hexdigest()
        outbound_task_id = _seed_task("outbound-good")
        outbound_evidence = {
            "outbound_actions": [
                {
                    "kind": "connect",
                    "target": "linkedin:example",
                    "sent_text_sha256": outbound_hash,
                    "gate_pass": {
                        "action_id": "gate-token-1",
                        "content_hash": outbound_hash,
                        "verdict": "signoff",
                        "draft_source": "grok",
                        "token_consumed_at": 1234567890,
                    },
                }
            ]
        }
        outbound_ok = client.patch(
            f"/api/task/{outbound_task_id}",
            json={"status": "completed", "from": "tester-api", "evidence": outbound_evidence},
        )
        outbound_payload = client.get(f"/api/tasks/{outbound_task_id}").json()
        check(
            "completed outbound action requires and persists matching signoff gate_pass",
            outbound_ok.status_code == 200
            and outbound_payload.get("status") == "completed"
            and outbound_payload.get("completion_evidence", {}).get("outbound_actions", [{}])[0]
            .get("gate_pass", {})
            .get("content_hash") == outbound_hash,
            f"update={outbound_ok.status_code} {outbound_ok.text[:160]} payload={outbound_payload}",
        )

        mismatch_task_id = _seed_task("outbound-mismatch")
        mismatch_hash = hashlib.sha256(b"different gated text").hexdigest()
        mismatch = client.patch(
            f"/api/task/{mismatch_task_id}",
            json={
                "status": "completed",
                "from": "tester-api",
                "evidence": {
                    "outbound_actions": [
                        {
                            "kind": "comment",
                            "target": "linkedin:example",
                            "sent_text_sha256": outbound_hash,
                            "gate_pass": {
                                "action_id": "gate-token-2",
                                "content_hash": mismatch_hash,
                                "verdict": "signoff",
                                "draft_source": "gemini",
                                "token_consumed_at": 1234567891,
                            },
                        }
                    ]
                },
            },
        )
        mismatch_payload = client.get(f"/api/tasks/{mismatch_task_id}").json()
        check(
            "reject outbound action when gate_pass content_hash mismatches sent_text_sha256",
            mismatch.status_code == 400
            and "gate_pass.content_hash" in mismatch.text
            and mismatch_payload.get("status") != "completed",
            f"got {mismatch.status_code} {mismatch.text[:200]} payload={mismatch_payload}",
        )

        missing_gate_task_id = _seed_task("outbound-missing-gate")
        missing_gate = client.patch(
            f"/api/task/{missing_gate_task_id}",
            json={
                "status": "completed",
                "from": "tester-api",
                "evidence": {
                    "outbound_actions": [
                        {
                            "kind": "dm",
                            "target": "linkedin:example",
                            "sent_text_sha256": outbound_hash,
                        }
                    ]
                },
            },
        )
        missing_gate_payload = client.get(f"/api/tasks/{missing_gate_task_id}").json()
        check(
            "reject outbound action without gate_pass",
            missing_gate.status_code == 400
            and "gate_pass" in missing_gate.text
            and missing_gate_payload.get("status") != "completed",
            f"got {missing_gate.status_code} {missing_gate.text[:200]} payload={missing_gate_payload}",
        )

        for status in sorted(TERMINAL_STATUSES):
            cli_task_id = _seed_task(f"cli-{status}")
            if status == "completed":
                evidence = {"production_observation": f"verified CLI/API round-trip for {status}"}
            else:
                evidence = {"reason": f"CLI/API round-trip reason for {status}"}
            code, _ = _cli_update_round_trip(client, cli_task_id, status, evidence)
            cli_payload = client.get(f"/api/tasks/{cli_task_id}").json()
            check(
                f"taey-task CLI accepts evidence and API persists {status}",
                code == 0
                and cli_payload.get("status") == status
                and all(cli_payload.get("completion_evidence", {}).get(k) == v for k, v in evidence.items()),
                f"code={code} payload={cli_payload}",
            )
        if failures:
            print(f"\nFAIL — {len(failures)} assertion(s): {failures}")
            return 1
        print("\nPASS — evidence gate enforces on every edge case")
        return 0
    finally:
        _cleanup(PREFIX)


if __name__ == "__main__":
    raise SystemExit(main())
