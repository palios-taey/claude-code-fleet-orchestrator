#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PREFIX = f"stopapi-{uuid.uuid4().hex[:8]}"
if "ORCH_DOTENV" not in os.environ:
    candidate = ROOT / ".env"
    if candidate.is_file():
        os.environ["ORCH_DOTENV"] = str(candidate)
os.environ["NOTIFY_KEY_PREFIX"] = PREFIX
os.environ.setdefault("ORCH_REDIS_HOST", "127.0.0.1")
os.environ.setdefault("ORCH_REDIS_PORT", "6379")

from lib.config import OrchConfig, get_neo4j_driver, get_redis_sync  # noqa: E402
from lib.orch_schema import create_phase, create_project, create_task, update_task_status  # noqa: E402

CFG = OrchConfig()


def _ensure_dotenv_for_server() -> str:
    explicit = os.environ.get("ORCH_DOTENV")
    if explicit:
        return explicit
    handle = tempfile.NamedTemporaryFile("w", delete=False, suffix=".env")
    for key in (
        "ORCH_REDIS_HOST",
        "ORCH_REDIS_PORT",
        "ORCH_NEO4J_URI",
        "ORCH_NEO4J_USER",
        "ORCH_NEO4J_PASS",
        "ORCH_NEO4J_DB",
        "ORCH_DASHBOARD_URL",
        "ORCH_NOTIFY_LIB_ROOT",
        "ORCH_NOTIFY_CLI",
    ):
        value = os.environ.get(key)
        if value:
            handle.write(f"{key}={value}\n")
    handle.flush()
    handle.close()
    return handle.name


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_http(url: str, timeout: float = 10.0) -> None:
    deadline = time.time() + timeout
    last_error = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1):
                return
        except Exception as exc:
            last_error = exc
            time.sleep(0.1)
    raise RuntimeError(f"server did not become ready: {last_error}")


def _json_get(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=5) as resp:
        return json.load(resp)


def _stop_block_count(session_id: str) -> str | None:
    r = get_redis_sync(CFG)
    raw = r.get(f"{PREFIX}:{session_id}:stop_block_count")
    return None if raw is None else str(raw)


def _cleanup(prefix: str) -> None:
    driver = get_neo4j_driver(CFG)
    with driver.session(database=CFG.neo4j_db) as session:
        session.run("MATCH (t:OrchTask) WHERE t.id STARTS WITH $prefix DETACH DELETE t", prefix=prefix)
        session.run("MATCH (ph:OrchPhase) WHERE ph.id STARTS WITH $prefix DETACH DELETE ph", prefix=prefix)
        session.run("MATCH (p:OrchProject) WHERE p.id STARTS WITH $prefix DETACH DELETE p", prefix=prefix)
    r = get_redis_sync(CFG)
    cursor = 0
    pattern = f"{PREFIX}:*"
    while True:
        cursor, keys = r.scan(cursor=cursor, match=pattern, count=100)
        if keys:
            r.delete(*keys)
        if cursor == 0:
            break


def _wire_dependency(task_id: str, dep_id: str) -> None:
    driver = get_neo4j_driver(CFG)
    with driver.session(database=CFG.neo4j_db) as session:
        session.run(
            """
            MATCH (t:OrchTask {id: $task_id}), (dep:OrchTask {id: $dep_id})
            MERGE (t)-[:DEPENDS_ON]->(dep)
            """,
            task_id=task_id,
            dep_id=dep_id,
        )


def main() -> int:
    project_id = f"{PREFIX}-project"
    phase_id = f"{PREFIX}-phase"
    task1 = f"{PREFIX}-task-1"
    task2 = f"{PREFIX}-task-2"
    port = _find_free_port()
    server_env = os.environ.copy()
    server_env["NOTIFY_KEY_PREFIX"] = PREFIX
    server_env["ORCH_DOTENV"] = _ensure_dotenv_for_server()
    server = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "lib.tasks_api:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT),
        env=server_env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        _cleanup(PREFIX)
        create_project(project_id, "Stop Decision API", supervisor="conductor", config=CFG)
        create_phase(project_id, phase_id, "Main", config=CFG)
        create_task(phase_id, task1, "First ready task", owner="conductor-codex", wake_owner_if_ready=False, config=CFG)
        create_task(phase_id, task2, "Second ready task", owner="conductor-codex", wake_owner_if_ready=False, config=CFG)
        _wire_dependency(task2, task1)

        _wait_for_http(f"http://127.0.0.1:{port}/health")

        first = _json_get(f"http://127.0.0.1:{port}/api/sessions/conductor-codex/stop-decision")
        print(
            "PASS live_cycle_step1"
            if first.get("block") and first.get("task_id") == task1 and first.get("wake_type") == "WAKE_WITH_QUEUE"
            else f"FAIL live_cycle_step1 {first}"
        )
        passive_before = _stop_block_count("conductor-codex")
        passive = _json_get(f"http://127.0.0.1:{port}/api/sessions/conductor-codex/stop-decision")
        passive_after = _stop_block_count("conductor-codex")
        print(
            "PASS passive_get_no_mutation"
            if passive.get("block") is True and passive_before == passive_after
            else f"FAIL passive_get_no_mutation before={passive_before} after={passive_after} payload={passive}"
        )
        active = _json_get(f"http://127.0.0.1:{port}/api/sessions/conductor-codex/stop-decision?stop_hook_active=true")
        active_after = _stop_block_count("conductor-codex")
        print(
            "PASS active_get_mutates"
            if active.get("block") is True and active_after == "1"
            else f"FAIL active_get_mutates count={active_after} payload={active}"
        )

        update_task_status(task1, "completed", owner="conductor-codex", config=CFG)
        second = _json_get(f"http://127.0.0.1:{port}/api/sessions/conductor-codex/stop-decision")
        print(
            "PASS live_cycle_step2"
            if second.get("block") and second.get("task_id") == task2 and second.get("wake_type") == "WAKE_WITH_QUEUE"
            else f"FAIL live_cycle_step2 {second}"
        )

        update_task_status(task2, "completed", owner="conductor-codex", config=CFG)
        third = _json_get(f"http://127.0.0.1:{port}/api/sessions/conductor-codex/stop-decision")
        print(
            "PASS live_cycle_step3"
            if third.get("block") is False and third.get("wake_type") == "ALLOW_STOP"
            else f"FAIL live_cycle_step3 {third}"
        )
        return 0
    finally:
        server.terminate()
        try:
            server.wait(timeout=5)
        except subprocess.TimeoutExpired:
            server.kill()
        _cleanup(PREFIX)


if __name__ == "__main__":
    raise SystemExit(main())
