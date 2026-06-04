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

from lib.config import OrchConfig, get_neo4j_driver  # noqa: E402
from lib.orch_schema import create_phase, create_project, create_task  # noqa: E402
from lib.tasks_api import app  # noqa: E402

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
    try:
        task_id = _seed_task()
        no_evidence = client.patch(
            f"/api/task/{task_id}",
            json={"status": "completed", "from": "tester-api"},
        )
        print(
            "PASS completed-without-evidence-rejected"
            if no_evidence.status_code == 400 and "requires evidence" in no_evidence.text
            else f"FAIL completed-without-evidence-rejected {no_evidence.status_code} {no_evidence.text}"
        )

        with_evidence = client.patch(
            f"/api/task/{task_id}",
            json={
                "status": "completed",
                "from": "tester-api",
                "evidence": {
                    "commit_sha": "deadbeef",
                    "gate_run_id": "gate-123",
                    "production_observation": "verified in acceptance",
                },
            },
        )
        task = client.get(f"/api/tasks/{task_id}")
        payload = task.json() if task.status_code == 200 else {}
        print(
            "PASS completed-with-evidence-persists"
            if (
                with_evidence.status_code == 200
                and with_evidence.json().get("ok") is True
                and payload.get("status") == "completed"
                and payload.get("completed_by") == "tester-api"
                and payload.get("completion_evidence", {}).get("commit_sha") == "deadbeef"
                and payload.get("completion_evidence", {}).get("gate_run_id") == "gate-123"
                and payload.get("completion_evidence", {}).get("production_observation") == "verified in acceptance"
            )
            else f"FAIL completed-with-evidence-persists update={with_evidence.status_code} task={task.status_code} payload={payload}"
        )
        return 0
    finally:
        _cleanup(PREFIX)


if __name__ == "__main__":
    raise SystemExit(main())
