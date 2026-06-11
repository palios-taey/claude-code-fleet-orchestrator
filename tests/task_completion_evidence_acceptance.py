#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

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
from fleet_orchestrator.orch_schema import create_phase, create_project, create_task  # noqa: E402
from fleet_orchestrator.tasks_api import app  # noqa: E402

CFG = OrchConfig()


def _cleanup(prefix: str) -> None:
    driver = get_neo4j_driver(CFG)
    with driver.session(database=CFG.neo4j_db) as session:
        session.run("MATCH (t:OrchTask) WHERE t.id STARTS WITH $prefix DETACH DELETE t", prefix=prefix)
        session.run("MATCH (ph:OrchPhase) WHERE ph.id STARTS WITH $prefix DETACH DELETE ph", prefix=prefix)
        session.run("MATCH (p:OrchProject) WHERE p.id STARTS WITH $prefix DETACH DELETE p", prefix=prefix)


def _seed_task() -> str:
    project_id = f"{PREFIX}-project"
    phase_id = f"{PREFIX}-phase"
    task_id = f"{PREFIX}-task"
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
        if failures:
            print(f"\nFAIL — {len(failures)} assertion(s): {failures}")
            return 1
        print("\nPASS — evidence gate enforces on every edge case")
        return 0
    finally:
        _cleanup(PREFIX)


if __name__ == "__main__":
    raise SystemExit(main())
