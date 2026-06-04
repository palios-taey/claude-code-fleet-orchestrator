#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PREFIX = f"reclaim-{uuid.uuid4().hex[:8]}"
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
os.environ.setdefault("ORCH_NOTIFY_LIB_ROOT", "/home/mira/claude-code-fleet-notify")

from lib.config import OrchConfig, get_neo4j_driver, get_redis_sync  # noqa: E402
from lib.dispatch import OrchTaskNotReady, dispatch  # noqa: E402
from lib.orch_schema import create_phase, create_project, create_task, get_task, update_task_status  # noqa: E402

CFG = OrchConfig()


def _cleanup(prefix: str) -> None:
    driver = get_neo4j_driver(CFG)
    with driver.session(database=CFG.neo4j_db) as session:
        session.run("MATCH (t:OrchTask) WHERE t.id STARTS WITH $prefix DETACH DELETE t", prefix=prefix)
        session.run("MATCH (ph:OrchPhase) WHERE ph.id STARTS WITH $prefix DETACH DELETE ph", prefix=prefix)
        session.run("MATCH (p:OrchProject) WHERE p.id STARTS WITH $prefix DETACH DELETE p", prefix=prefix)
    r = get_redis_sync(CFG)
    for node in (f"{PREFIX}-worker",):
        r.delete(f"taey:{node}:current_task")
        r.delete(f"taey:{node}:last_outcome")


def _seed_task(task_id: str) -> None:
    project_id = f"{PREFIX}-project"
    phase_id = f"{PREFIX}-phase"
    create_project(project_id, "dispatch reclaim", supervisor="tester", priority=1, config=CFG)
    create_phase(project_id, phase_id, "Main", config=CFG)
    create_task(
        phase_id,
        task_id,
        f"task {task_id}",
        owner=f"{PREFIX}-worker",
        priority=5,
        wake_owner_if_ready=False,
        config=CFG,
    )


def _mock_notify():
    return mock.patch("lib.dispatch.subprocess.run", return_value=mock.Mock(returncode=0, stderr="", stdout=""))


def main() -> int:
    _cleanup(PREFIX)
    worker = f"{PREFIX}-worker"
    try:
        completed_task = f"{PREFIX}-completed"
        _seed_task(completed_task)
        update_task_status(
            completed_task,
            "completed",
            owner=worker,
            completion_evidence={"commit_sha": "deadbeef"},
            completed_by="tester",
            config=CFG,
        )
        try:
            with _mock_notify():
                dispatch(worker, completed_task, "completed task", supervisor="tester", allow_reclaim=False)
        except OrchTaskNotReady:
            print("PASS completed-without-reclaim-rejected")
        else:
            print("FAIL completed-without-reclaim-rejected no exception")

        with _mock_notify():
            dispatch(worker, completed_task, "completed task", supervisor="tester", allow_reclaim=True)
        reclaimed = get_task(completed_task, config=CFG)
        print(
            "PASS completed-reclaim-allowed"
            if (
                reclaimed
                and reclaimed.get("status") == "in_progress"
                and reclaimed.get("owner") == worker
                and reclaimed.get("completion_evidence") in (None, {})
                and int(reclaimed.get("dispatch_cycle", 0) or 0) == 1
                and reclaimed.get("last_claim_mode") == "reclaim"
                and reclaimed.get("last_claim_from_status") == "completed"
            )
            else f"FAIL completed-reclaim-allowed {reclaimed}"
        )
        r = get_redis_sync(CFG)
        r.delete(f"taey:{worker}:current_task")
        r.delete(f"taey:{worker}:last_outcome")

        in_progress_task = f"{PREFIX}-in-progress"
        _seed_task(in_progress_task)
        update_task_status(in_progress_task, "in_progress", owner=worker, config=CFG)
        with _mock_notify():
            dispatch(worker, in_progress_task, "in-progress task", supervisor="tester", allow_reclaim=True)
        reclaimed_live = get_task(in_progress_task, config=CFG)
        print(
            "PASS in-progress-reclaim-allowed"
            if (
                reclaimed_live
                and reclaimed_live.get("status") == "in_progress"
                and reclaimed_live.get("owner") == worker
                and int(reclaimed_live.get("dispatch_cycle", 0) or 0) == 1
                and reclaimed_live.get("last_claim_mode") == "reclaim"
                and reclaimed_live.get("last_claim_from_status") == "in_progress"
            )
            else f"FAIL in-progress-reclaim-allowed {reclaimed_live}"
        )
        return 0
    finally:
        _cleanup(PREFIX)


if __name__ == "__main__":
    raise SystemExit(main())
