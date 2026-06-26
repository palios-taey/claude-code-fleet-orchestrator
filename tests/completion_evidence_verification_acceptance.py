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
ORCH_REPO = "palios-taey/claude-code-fleet-orchestrator"
CONDUCTOR_REPO = "palios-taey/the-conductor"
WRONG_REPO = "palios-taey/not-the-repo"
ATTACKER_REPO = "attacker-acct/evil-fork"
GREEN_SHA = "1111111111111111111111111111111111111111"
RED_SHA = "2222222222222222222222222222222222222222"
CONDUCTOR_SHA = "3333333333333333333333333333333333333333"
ATTACKER_SHA = "4444444444444444444444444444444444444444"
UNTRUSTED_STATUS_SHA = "5555555555555555555555555555555555555555"
UNTRUSTED_CHECK_SHA = "6666666666666666666666666666666666666666"
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
                print(json.dumps({{"full_name": "{CONDUCTOR_REPO}"}}))
                sys.exit(0)

            existing = {{
                ("{ORCH_REPO}", "{GREEN_SHA}"): "green",
                ("{ORCH_REPO}", "{RED_SHA}"): "red",
                ("{ORCH_REPO}", "{UNTRUSTED_STATUS_SHA}"): "untrusted-status",
                ("{ORCH_REPO}", "{UNTRUSTED_CHECK_SHA}"): "untrusted-check",
                ("{CONDUCTOR_REPO}", "{CONDUCTOR_SHA}"): "green",
                ("{ATTACKER_REPO}", "{ATTACKER_SHA}"): "green",
            }}
            for (repo, sha), mode in existing.items():
                if path == f"repos/{{repo}}/commits/{{sha}}":
                    print(json.dumps({{"sha": sha}}))
                    sys.exit(0)
                if path == f"repos/{{repo}}/commits/{{sha}}/check-runs?per_page=100":
                    conclusion = "success" if mode == "green" else "failure"
                    app_slug = "evil-ci" if mode == "untrusted-check" else "github-actions"
                    if mode in {{"untrusted-status", "untrusted-check"}}:
                        conclusion = "success"
                    print(json.dumps({{
                        "check_runs": [
                            {{
                                "name": "ship-gate-acceptance",
                                "status": "completed",
                                "conclusion": conclusion,
                                "completed_at": "2026-06-26T00:00:01Z",
                                "app": {{"slug": app_slug}},
                            }}
                        ]
                    }}))
                    sys.exit(0)
                if path == f"repos/{{repo}}/commits/{{sha}}/statuses?per_page=100":
                    creator = "attacker-bot" if mode == "untrusted-status" else "github-actions[bot]"
                    print(json.dumps([
                        {{
                            "context": "r5-audit-gate",
                            "state": "success",
                            "created_at": "2026-06-26T00:00:00Z",
                            "creator": {{"login": creator}},
                        }}
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
    env_keys = (
        "PATH",
        "ORCH_COMPLETION_GITHUB_REPO",
        "ORCH_COMPLETION_REQUIRED_CHECKS",
        "ORCH_COMPLETION_ALLOWED_REPOS",
        "ORCH_COMPLETION_TRUSTED_CHECK_RUN_APPS",
        "ORCH_COMPLETION_TRUSTED_STATUS_CREATORS",
    )
    previous_env = {key: os.environ.get(key) for key in env_keys}

    def check(label: str, cond: bool, extra: str = "") -> None:
        print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {extra}"))
        if not cond:
            failures.append(label)

    try:
        _write_fake_gh(tmp)
        os.environ["PATH"] = f"{tmp}{os.pathsep}{os.environ['PATH']}"
        os.environ["ORCH_COMPLETION_GITHUB_REPO"] = CONDUCTOR_REPO
        os.environ["ORCH_COMPLETION_REQUIRED_CHECKS"] = "r5-audit-gate,ship-gate-acceptance"
        os.environ["ORCH_COMPLETION_ALLOWED_REPOS"] = f"{ORCH_REPO},{CONDUCTOR_REPO}"
        os.environ["ORCH_COMPLETION_TRUSTED_CHECK_RUN_APPS"] = "github-actions"
        os.environ["ORCH_COMPLETION_TRUSTED_STATUS_CREATORS"] = "github-actions[bot]"

        repo_only_task = _seed_task("repo-only")
        repo_only_response = client.patch(
            f"/api/task/{repo_only_task}",
            json={"status": "completed", "from": "tester-api", "evidence": {"repo": ORCH_REPO}},
        )
        check(
            "repo alone is context, not completion evidence",
            repo_only_response.status_code == 400
            and "commit_sha, gate_run_id, production_observation" in repo_only_response.text,
            f"{repo_only_response.status_code} {repo_only_response.text}",
        )

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

        conductor_task = _seed_task("conductor")
        _complete(
            client,
            conductor_task,
            {"commit_sha": CONDUCTOR_SHA, "production_observation": "fallback runtime repo probe"},
        )
        conductor_payload = client.get(f"/api/tasks/{conductor_task}").json()
        check(
            "evidence without repo falls back to configured runtime repo",
            conductor_payload.get("completion_evidence_verification_status") == "VERIFIED"
            and conductor_payload.get("completion_evidence_verification", {}).get("repo") == CONDUCTOR_REPO,
            json.dumps(conductor_payload.get("completion_evidence_verification"), sort_keys=True),
        )

        missing_task = _seed_task("missing")
        missing_update = _complete(
            client,
            missing_task,
            {"commit_sha": MISSING_SHA, "repo": ORCH_REPO, "production_observation": "fabricated sha probe"},
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
            {"commit_sha": RED_SHA, "repo": ORCH_REPO, "production_observation": "red gate probe"},
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
            {"commit_sha": GREEN_SHA, "repo": ORCH_REPO, "production_observation": "green gate probe"},
        )
        green_payload = client.get(f"/api/tasks/{green_task}").json()
        check(
            "evidence repo overrides runtime repo and verifies orchestrator commit",
            green_update.get("completion_evidence_verification_status") == "VERIFIED"
            and green_payload.get("completion_evidence_verification_status") == "VERIFIED"
            and green_payload.get("completion_evidence_verified") is True
            and green_payload.get("completion_evidence", {}).get("repo") == ORCH_REPO
            and green_payload.get("completion_evidence_verification", {}).get("repo") == ORCH_REPO,
            f"update={green_update} payload={green_payload}",
        )
        wrong_repo_task = _seed_task("wrong-repo")
        _complete(
            client,
            wrong_repo_task,
            {"commit_sha": GREEN_SHA, "repo": WRONG_REPO, "production_observation": "wrong repo spoof probe"},
        )
        wrong_repo_payload = client.get(f"/api/tasks/{wrong_repo_task}").json()
        check(
            "wrong evidence repo cannot forge VERIFIED",
            wrong_repo_payload.get("completion_evidence_verification_status") == "UNVERIFIED"
            and wrong_repo_payload.get("completion_evidence_verified") is False
            and wrong_repo_payload.get("completion_evidence_verification", {}).get("repo") == WRONG_REPO,
            json.dumps(wrong_repo_payload.get("completion_evidence_verification"), sort_keys=True),
        )
        attacker_task = _seed_task("attacker")
        _complete(
            client,
            attacker_task,
            {"commit_sha": ATTACKER_SHA, "repo": ATTACKER_REPO, "production_observation": "attacker repo green gate spoof"},
        )
        attacker_payload = client.get(f"/api/tasks/{attacker_task}").json()
        check(
            "off-allowlist repo with green gates cannot forge VERIFIED",
            attacker_payload.get("completion_evidence_verification_status") == "UNVERIFIED"
            and attacker_payload.get("completion_evidence_verified") is False
            and attacker_payload.get("completion_evidence_verification", {}).get("repo") == ATTACKER_REPO
            and "allowlist" in attacker_payload.get("completion_evidence_verification", {}).get("reason", ""),
            json.dumps(attacker_payload.get("completion_evidence_verification"), sort_keys=True),
        )
        untrusted_status_task = _seed_task("untrusted-status")
        _complete(
            client,
            untrusted_status_task,
            {
                "commit_sha": UNTRUSTED_STATUS_SHA,
                "repo": ORCH_REPO,
                "production_observation": "allowed repo untrusted status creator probe",
            },
        )
        untrusted_status_payload = client.get(f"/api/tasks/{untrusted_status_task}").json()
        check(
            "allowed repo status from untrusted creator cannot forge VERIFIED",
            untrusted_status_payload.get("completion_evidence_verification_status") == "UNVERIFIED"
            and untrusted_status_payload.get("completion_evidence_verified") is False
            and "trusted_creator=False" in json.dumps(untrusted_status_payload.get("completion_evidence_verification"), sort_keys=True),
            json.dumps(untrusted_status_payload.get("completion_evidence_verification"), sort_keys=True),
        )
        untrusted_check_task = _seed_task("untrusted-check")
        _complete(
            client,
            untrusted_check_task,
            {
                "commit_sha": UNTRUSTED_CHECK_SHA,
                "repo": ORCH_REPO,
                "production_observation": "allowed repo untrusted check app probe",
            },
        )
        untrusted_check_payload = client.get(f"/api/tasks/{untrusted_check_task}").json()
        check(
            "allowed repo check-run from untrusted app cannot forge VERIFIED",
            untrusted_check_payload.get("completion_evidence_verification_status") == "UNVERIFIED"
            and untrusted_check_payload.get("completion_evidence_verified") is False
            and "trusted_app=False" in json.dumps(untrusted_check_payload.get("completion_evidence_verification"), sort_keys=True),
            json.dumps(untrusted_check_payload.get("completion_evidence_verification"), sort_keys=True),
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
