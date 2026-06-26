#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import importlib
import io
import json
import os
import shutil
import sys
import tempfile
import textwrap
import uuid
from pathlib import Path
from unittest import mock

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PREFIX = f"truth-{uuid.uuid4().hex[:8]}"
REPO = "palios-taey/claude-code-fleet-orchestrator"
GREEN_SHA = "1111111111111111111111111111111111111111"
RED_SHA = "2222222222222222222222222222222222222222"
MISSING_SHA = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

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


def _seed_task(suffix: str) -> str:
    project_id = f"{PREFIX}-project"
    phase_id = f"{PREFIX}-phase"
    task_id = f"{PREFIX}-{suffix}"
    create_project(project_id, "completion evidence truth project", supervisor="tester", priority=1, config=CFG)
    create_phase(project_id, phase_id, "Main", config=CFG)
    create_task(
        phase_id,
        task_id,
        f"completion evidence truth task {suffix}",
        owner="tester-codex",
        priority=5,
        wake_owner_if_ready=False,
        config=CFG,
    )
    return task_id


def _write_fake_gh(directory: Path) -> None:
    gh = directory / "gh"
    gh.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import json
            import sys

            if len(sys.argv) < 3 or sys.argv[1] != "api":
                print("unsupported fake gh invocation", file=sys.stderr)
                sys.exit(2)

            path = sys.argv[2]
            if path == "repos/:owner/:repo":
                print(json.dumps({{"full_name": "{REPO}"}}))
                sys.exit(0)

            existing = {{"{GREEN_SHA}": "green", "{RED_SHA}": "red"}}
            for sha, mode in existing.items():
                if path == f"repos/{REPO}/commits/{{sha}}":
                    print(json.dumps({{"sha": sha}}))
                    sys.exit(0)
                if path == f"repos/{REPO}/commits/{{sha}}/check-runs?per_page=100":
                    conclusion = "success" if mode == "green" else "failure"
                    print(json.dumps({{
                        "check_runs": [
                            {{"name": "ship-gate-acceptance", "status": "completed", "conclusion": conclusion, "completed_at": "2026-06-26T00:00:01Z"}}
                        ]
                    }}))
                    sys.exit(0)
                if path == f"repos/{REPO}/commits/{{sha}}/statuses?per_page=100":
                    print(json.dumps([
                        {{"context": "r5-audit-gate", "state": "success", "created_at": "2026-06-26T00:00:00Z"}}
                    ]))
                    sys.exit(0)

            print(f"fake gh: not found {{path}}", file=sys.stderr)
            sys.exit(1)
            """
        )
    )
    gh.chmod(0o755)


def _complete(client: TestClient, task_id: str, evidence: dict) -> dict:
    response = client.patch(
        f"/api/task/{task_id}",
        json={"status": "completed", "from": "tester-api", "evidence": evidence},
    )
    if response.status_code != 200:
        raise AssertionError(f"completion failed HTTP {response.status_code}: {response.text}")
    return response.json()


def _cli_status_text(client: TestClient, task_id: str) -> str:
    cli = importlib.import_module("fleet_orchestrator.cli_taey_task")

    def api_call(method: str, endpoint: str, data=None):
        if method != "GET":
            raise AssertionError(f"unexpected CLI method {method}")
        response = client.get(endpoint)
        if response.status_code >= 400:
            raise AssertionError(f"CLI API call failed HTTP {response.status_code}: {response.text}")
        return response.json()

    stdout = io.StringIO()
    argv = ["taey-task", "status", task_id]
    with mock.patch.object(cli, "api_call", side_effect=api_call), \
         mock.patch.object(sys, "argv", argv), \
         contextlib.redirect_stdout(stdout):
        try:
            cli.main()
        except SystemExit as exc:
            if int(exc.code or 0) != 0:
                raise
    return stdout.getvalue()


def main() -> int:
    _cleanup(PREFIX)
    client = TestClient(app)
    tmp = Path(tempfile.mkdtemp(prefix=f"{PREFIX}-"))
    failures = []
    env_keys = ("PATH", "ORCH_COMPLETION_GITHUB_REPO", "ORCH_COMPLETION_REQUIRED_CHECKS")
    previous_env = {key: os.environ.get(key) for key in env_keys}

    def check(label: str, cond: bool, extra: str = "") -> None:
        print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {extra}"))
        if not cond:
            failures.append(label)

    try:
        _write_fake_gh(tmp)
        os.environ["PATH"] = f"{tmp}{os.pathsep}{os.environ['PATH']}"
        os.environ["ORCH_COMPLETION_GITHUB_REPO"] = REPO
        os.environ["ORCH_COMPLETION_REQUIRED_CHECKS"] = "r5-audit-gate,ship-gate-acceptance"

        local_task = _seed_task("local")
        _complete(
            client,
            local_task,
            {"production_observation": "local-only completion probe"},
        )
        local_payload = client.get(f"/api/tasks/{local_task}").json()
        check(
            "local completion without commit_sha is UNVERIFIED",
            local_payload.get("completion_evidence_verification_status") == "UNVERIFIED"
            and local_payload.get("completion_evidence_verified") is False
            and "no commit_sha" in local_payload.get("completion_evidence_verification", {}).get("reason", ""),
            json.dumps(local_payload.get("completion_evidence_verification"), sort_keys=True),
        )

        missing_task = _seed_task("missing")
        missing_update = _complete(
            client,
            missing_task,
            {"commit_sha": MISSING_SHA, "production_observation": "fabricated sha probe"},
        )
        missing_payload = client.get(f"/api/tasks/{missing_task}").json()
        check(
            "fabricated commit completes but is UNVERIFIED",
            missing_update.get("completion_evidence_verification_status") == "UNVERIFIED"
            and missing_payload.get("completion_evidence_verification_status") == "UNVERIFIED"
            and missing_payload.get("completion_evidence_verified") is False,
            f"update={missing_update} payload={missing_payload}",
        )

        red_task = _seed_task("red")
        _complete(
            client,
            red_task,
            {"commit_sha": RED_SHA, "production_observation": "red gate probe"},
        )
        red_payload = client.get(f"/api/tasks/{red_task}").json()
        check(
            "existing commit without all required gates is UNVERIFIED",
            red_payload.get("completion_evidence_verification_status") == "UNVERIFIED"
            and "ship-gate-acceptance" in red_payload.get("completion_evidence_verification", {}).get("reason", ""),
            json.dumps(red_payload.get("completion_evidence_verification"), sort_keys=True),
        )

        green_task = _seed_task("green")
        green_update = _complete(
            client,
            green_task,
            {"commit_sha": GREEN_SHA, "production_observation": "green gate probe"},
        )
        green_payload = client.get(f"/api/tasks/{green_task}").json()
        check(
            "commit with required independent gates is VERIFIED",
            green_update.get("completion_evidence_verification_status") == "VERIFIED"
            and green_payload.get("completion_evidence_verification_status") == "VERIFIED"
            and green_payload.get("completion_evidence_verified") is True,
            f"update={green_update} payload={green_payload}",
        )
        check(
            "taey-task status surfaces VERIFIED",
            "Completion evidence verification: VERIFIED" in _cli_status_text(client, green_task),
        )
        check(
            "taey-task status surfaces UNVERIFIED",
            "Completion evidence verification: UNVERIFIED" in _cli_status_text(client, missing_task),
        )

        if failures:
            print(f"\nFAIL - {len(failures)} assertion(s): {failures}")
            return 1
        print("\nPASS - completion evidence truth marker distinguishes VERIFIED from UNVERIFIED")
        return 0
    finally:
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        _cleanup(PREFIX)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
