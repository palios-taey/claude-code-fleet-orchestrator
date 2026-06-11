#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import socket
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, Iterable, Tuple

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for candidate in (ROOT / ".env", Path.home() / "claude-code-fleet-orchestrator/.env"):
    if "ORCH_DOTENV" not in os.environ and candidate.is_file():
        os.environ["ORCH_DOTENV"] = str(candidate)
        break
os.environ.setdefault("ORCH_REDIS_HOST", "127.0.0.1")
os.environ.setdefault("ORCH_REDIS_PORT", "6379")

from fleet_orchestrator.config import OrchConfig, get_neo4j_driver  # noqa: E402
from fleet_orchestrator.orch_schema import create_phase, create_project, create_task  # noqa: E402
from fleet_orchestrator.public_readonly import _UI_SESSIONS, _hidden_sessions  # noqa: E402


CFG = OrchConfig()
HIDDEN = _hidden_sessions()
PREFIX = f"publicro-{int(time.time())}"


def _request(method: str, url: str, data: Dict[str, Any] | None = None) -> Tuple[int, str]:
    payload = None
    headers = {}
    if data is not None:
        payload = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=payload, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read().decode("utf-8")


def _wait_for_http(url: str, timeout_s: float = 20.0) -> None:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            status, _ = _request("GET", url)
        except Exception:
            status = 0
        if status == 200:
            return
        time.sleep(0.2)
    raise RuntimeError(f"timeout waiting for {url}")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _ss_rows(port: int) -> str:
    result = subprocess.run(
        ["ss", "-ltn"],
        capture_output=True,
        text=True,
        cwd=str(ROOT),
        check=True,
    )
    rows = []
    for line in result.stdout.splitlines():
        parts = line.split()
        if len(parts) < 4:
            continue
        local = parts[3]
        any_ipv4 = ".".join(["0", "0", "0", "0"])
        if local == f"127.0.0.1:{port}" or local == f"{any_ipv4}:{port}" or local == f"[::]:{port}":
            rows.append(line)
    return "\n".join(rows)


def _cleanup(prefix: str) -> None:
    driver = get_neo4j_driver(CFG)
    with driver.session(database=CFG.neo4j_db) as session:
        session.run("MATCH (t:OrchTask) WHERE t.id STARTS WITH $prefix DETACH DELETE t", prefix=prefix)
        session.run("MATCH (ph:OrchPhase) WHERE ph.id STARTS WITH $prefix DETACH DELETE ph", prefix=prefix)
        session.run("MATCH (p:OrchProject) WHERE p.id STARTS WITH $prefix DETACH DELETE p", prefix=prefix)


def _seed_pointer_fixture(prefix: str, root: Path) -> Tuple[str, str]:
    os.environ["ORCH_REF_ALLOWED_ROOT"] = str(root)
    plan_dir = root / "plans"
    plan_dir.mkdir(parents=True, exist_ok=True)
    plan_path = plan_dir / "plan.md"
    plan_path.write_text("# stub\n", encoding="utf-8")
    src_dir = plan_dir / "src"
    src_dir.mkdir(exist_ok=True)
    (src_dir / "module.py").write_text("line1\nline2\n", encoding="utf-8")
    project_id = f"{prefix}-public-project"
    phase_id = f"{prefix}-public-phase"
    task_id = f"{prefix}-public-task"
    create_project(project_id, "public proof", supervisor="conductor", priority=1, source_path=str(plan_path), config=CFG)
    create_phase(project_id, phase_id, "phase", refs=[{"path": "src/module.py", "l_start": 1, "l_end": 2}], config=CFG)
    create_task(phase_id, task_id, "task", owner="conductor", priority=5, wake_owner_if_ready=False, config=CFG)
    return project_id, str(plan_path)


def _hidden_project_id() -> str:
    driver = get_neo4j_driver(CFG)
    with driver.session(database=CFG.neo4j_db) as session:
        record = session.run(
            """
            MATCH (p:OrchProject)
            WHERE coalesce(p.supervisor, '') IN $hidden
            RETURN p.id AS id
            ORDER BY p.id
            LIMIT 1
            """,
            hidden=list(HIDDEN),
        ).single()
    if not record:
        raise RuntimeError("no hidden-session project found in live data")
    return str(record["id"])


def _visible_session() -> str:
    for session_id in _UI_SESSIONS:
        if session_id not in HIDDEN:
            return session_id
    raise RuntimeError("no visible session configured")


def main() -> int:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    process = None
    pointer_project_id = ""
    fixture_root = Path("/tmp") / f"{PREFIX}-refs"
    try:
        _cleanup(PREFIX)
        if fixture_root.exists():
            shutil.rmtree(fixture_root)
        fixture_root.mkdir(parents=True, exist_ok=True)
        pointer_project_id, _ = _seed_pointer_fixture(PREFIX, fixture_root)
        hidden_project_id = _hidden_project_id()
        env = os.environ.copy()

        process = subprocess.Popen(
            [sys.executable, str(ROOT / "scripts" / "orch-public"), "--port", str(port)],
            cwd=str(ROOT),
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        _wait_for_http(f"{base_url}/health")

        bind_rows = _ss_rows(port)
        assert f"127.0.0.1:{port}" in bind_rows, bind_rows
        any_ipv4 = ".".join(["0", "0", "0", "0"])
        assert f"{any_ipv4}:{port}" not in bind_rows, bind_rows
        assert f"[::]:{port}" not in bind_rows, bind_rows
        print("PASS bind-127-only")

        health_status, health_body = _request("GET", f"{base_url}/health")
        assert health_status == 200, health_body
        print("PASS health-200")

        projects_status, projects_body = _request("GET", f"{base_url}/api/projects")
        assert projects_status == 200, projects_body
        projects = json.loads(projects_body)["projects"]
        hidden_ids = {project["id"] for project in projects}
        assert hidden_project_id not in hidden_ids, hidden_ids
        print("PASS denylist-project-list")

        direct_hidden_status, _ = _request("GET", f"{base_url}/api/projects/{hidden_project_id}")
        assert direct_hidden_status == 404, direct_hidden_status
        print("PASS denylist-project-direct")

        hidden_session = sorted(HIDDEN)[0]
        current_hidden_status, _ = _request("GET", f"{base_url}/api/sessions/{hidden_session}/current")
        assert current_hidden_status == 404, current_hidden_status
        print("PASS denylist-session-current")

        next_hidden_status, _ = _request("GET", f"{base_url}/api/sessions/{hidden_session}/next-ready")
        assert next_hidden_status == 404, next_hidden_status
        print("PASS denylist-session-next")

        projects_hidden_status, _ = _request("GET", f"{base_url}/api/sessions/{hidden_session}/projects")
        assert projects_hidden_status == 404, projects_hidden_status
        print("PASS denylist-session-projects")

        summary_status, summary_body = _request("GET", f"{base_url}/api/projects/{pointer_project_id}")
        assert summary_status == 200, summary_body
        summary = json.loads(summary_body)
        phase_ref = summary["phases"][0]["phase"]["ref_context"]["refs"][0]
        assert phase_ref["pointer"] == "src/module.py:1-2", phase_ref
        assert "content" not in phase_ref, phase_ref
        print("PASS ref-pointer-only")

        ui_status, ui_body = _request("GET", f"{base_url}/ui/")
        assert ui_status == 200, ui_body
        assert "notify-form" not in ui_body, ui_body
        for hidden in HIDDEN:
            assert f'"{hidden}"' not in ui_body, ui_body
        print("PASS ui-no-notify-form")

        private_js_status, _ = _request("GET", f"{base_url}/ui/static/app.js")
        assert private_js_status == 404, private_js_status
        print("PASS private-app-js-hidden")

        visible_session = _visible_session()
        session_projects_status, _ = _request("GET", f"{base_url}/api/sessions/{visible_session}/projects")
        assert session_projects_status == 200, session_projects_status
        print("PASS visible-session-projects")

        write_results = {}
        write_checks = [
            ("POST", "/api/projects"),
            ("POST", "/api/projects/load-md"),
            ("POST", f"/api/projects/{pointer_project_id}/complete"),
            ("POST", f"/api/projects/{pointer_project_id}/reset"),
            ("POST", f"/api/sessions/{visible_session}/notify"),
            ("POST", f"/api/sessions/{visible_session}/pause"),
            ("DELETE", f"/api/sessions/{visible_session}/pause"),
            ("POST", "/api/task/create"),
            ("PATCH", f"/api/task/{pointer_project_id}"),
        ]
        for method, path in write_checks:
            status, _ = _request(method, f"{base_url}{path}", data={})
            assert status in {404, 405}, (method, path, status)
            write_results[f"{method} {path}"] = status
        print("PASS write-routes-absent")
        for label, status in write_results.items():
            print(f"WRITE {label} -> {status}")

        return 0
    finally:
        if process is not None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
        _cleanup(PREFIX)
        if fixture_root.exists():
            shutil.rmtree(fixture_root)


if __name__ == "__main__":
    raise SystemExit(main())
