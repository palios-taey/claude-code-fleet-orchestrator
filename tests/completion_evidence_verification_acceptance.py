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
TAEYS_HANDS_REPO = "palios-taey/taeys-hands"
WRONG_REPO = "palios-taey/not-the-repo"
ATTACKER_REPO = "attacker-acct/evil-fork"
GREEN_SHA = "1111111111111111111111111111111111111111"
RED_SHA = "2222222222222222222222222222222222222222"
CONDUCTOR_SHA = "3333333333333333333333333333333333333333"
TAEYS_HANDS_SHA = "3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a3a"
TAEYS_HANDS_BRANCH_SHA = "3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b3b"
TAEYS_HANDS_GATED_SHA = "3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c"
ATTACKER_SHA = "4444444444444444444444444444444444444444"
UNTRUSTED_STATUS_SHA = "5555555555555555555555555555555555555555"
UNTRUSTED_CHECK_SHA = "6666666666666666666666666666666666666666"
OPEN_PR_SHA = "7777777777777777777777777777777777777777"
SQUASH_MERGE_SHA = "8888888888888888888888888888888888888888"
SQUASH_HEAD_SHA = "9999999999999999999999999999999999999999"
SQUASH_FAILED_R5_SHA = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
SQUASH_UNTRUSTED_R5_SHA = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
MISSING_SHA = "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

if "ORCH_DOTENV" not in os.environ:
    for candidate in (
        ROOT / ".env",
        Path.home() / "claude-code-fleet-orchestrator/.env",
    ):
        if candidate.is_file():
            os.environ["ORCH_DOTENV"] = str(candidate)
            break
from fleet_orchestrator.test_isolation import assert_acceptance_redis_isolated  # noqa: E402

assert_acceptance_redis_isolated()

from fleet_orchestrator.config import OrchConfig, get_neo4j_driver  # noqa: E402
from fleet_orchestrator import evidence_verification  # noqa: E402
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

            repos = {{
                "{ORCH_REPO}": {{"default_branch": "main"}},
                "{CONDUCTOR_REPO}": {{"default_branch": "main"}},
                "{TAEYS_HANDS_REPO}": {{"default_branch": "main"}},
                "{ATTACKER_REPO}": {{"default_branch": "main"}},
            }}
            if path in {{f"repos/{{repo}}" for repo in repos}}:
                repo = path.removeprefix("repos/")
                print(json.dumps({{"full_name": repo, **repos[repo]}}))
                sys.exit(0)

            existing = {{
                ("{ORCH_REPO}", "{GREEN_SHA}"): "green",
                ("{ORCH_REPO}", "{RED_SHA}"): "red",
                ("{ORCH_REPO}", "{UNTRUSTED_STATUS_SHA}"): "untrusted-status",
                ("{ORCH_REPO}", "{UNTRUSTED_CHECK_SHA}"): "untrusted-check",
                ("{ORCH_REPO}", "{SQUASH_MERGE_SHA}"): "squash-merge",
                ("{ORCH_REPO}", "{SQUASH_HEAD_SHA}"): "squash-head",
                ("{ORCH_REPO}", "{SQUASH_FAILED_R5_SHA}"): "squash-failed-r5",
                ("{ORCH_REPO}", "{SQUASH_UNTRUSTED_R5_SHA}"): "squash-untrusted-r5",
                ("{CONDUCTOR_REPO}", "{CONDUCTOR_SHA}"): "green",
                ("{TAEYS_HANDS_REPO}", "{TAEYS_HANDS_SHA}"): "gateless-main",
                ("{TAEYS_HANDS_REPO}", "{TAEYS_HANDS_BRANCH_SHA}"): "gateless-branch",
                ("{TAEYS_HANDS_REPO}", "{TAEYS_HANDS_GATED_SHA}"): "taeys-gated",
                ("{ATTACKER_REPO}", "{ATTACKER_SHA}"): "green",
                ("{ORCH_REPO}", "{OPEN_PR_SHA}"): "open-pr",
            }}
            for (repo, sha), mode in existing.items():
                if path == f"repos/{{repo}}/commits/{{sha}}":
                    print(json.dumps({{"sha": sha}}))
                    sys.exit(0)
                if path == f"repos/{{repo}}/compare/{{sha}}...main":
                    status = "ahead" if mode == "gateless-main" else "diverged"
                    print(json.dumps({{"status": status, "base_commit": {{"sha": sha}}}}))
                    sys.exit(0)
                if path == f"repos/{{repo}}/commits/{{sha}}/pulls?per_page=100":
                    if mode == "open-pr":
                        print(json.dumps([
                            {{"number": 246, "state": "open", "html_url": "https://example.invalid/pr/246"}}
                        ]))
                    elif mode == "squash-merge":
                        print(json.dumps([
                            {{
                                "number": 252,
                                "state": "closed",
                                "merged_at": "2026-07-02T11:52:00Z",
                                "html_url": "https://example.invalid/pr/252",
                            }}
                        ]))
                    elif mode in {{"squash-failed-r5", "squash-untrusted-r5"}}:
                        number = 253 if mode == "squash-failed-r5" else 254
                        print(json.dumps([
                            {{
                                "number": number,
                                "state": "closed",
                                "merged_at": "2026-07-02T11:54:00Z",
                                "html_url": f"https://example.invalid/pr/{{number}}",
                            }}
                        ]))
                    else:
                        print(json.dumps([]))
                    sys.exit(0)
                if path == f"repos/{{repo}}/pulls/252" and mode == "squash-merge":
                    print(json.dumps({{
                        "number": 252,
                        "state": "closed",
                        "merged": True,
                        "merged_at": "2026-07-02T11:52:00Z",
                        "merge_commit_sha": "{SQUASH_MERGE_SHA}",
                        "html_url": "https://example.invalid/pr/252",
                        "head": {{
                            "sha": "{SQUASH_HEAD_SHA}",
                            "repo": {{"full_name": "{ORCH_REPO}"}},
                        }},
                    }}))
                    sys.exit(0)
                if mode in {{"squash-failed-r5", "squash-untrusted-r5"}}:
                    number = 253 if mode == "squash-failed-r5" else 254
                    if path == f"repos/{{repo}}/pulls/{{number}}":
                        print(json.dumps({{
                            "number": number,
                            "state": "closed",
                            "merged": True,
                            "merged_at": "2026-07-02T11:54:00Z",
                            "merge_commit_sha": sha,
                            "html_url": f"https://example.invalid/pr/{{number}}",
                            "head": {{
                                "sha": "{SQUASH_HEAD_SHA}",
                                "repo": {{"full_name": "{ORCH_REPO}"}},
                            }},
                        }}))
                        sys.exit(0)
                if path == f"repos/{{repo}}/commits/{{sha}}/check-runs?per_page=100":
                    conclusion = "success" if mode == "green" else "failure"
                    app_slug = "evil-ci" if mode == "untrusted-check" else "github-actions"
                    if mode in {{
                        "untrusted-status",
                        "untrusted-check",
                        "squash-merge",
                        "squash-head",
                        "squash-failed-r5",
                        "squash-untrusted-r5",
                    }}:
                        conclusion = "success"
                    runs = [
                        {{
                            "name": "ship-gate-acceptance",
                            "status": "completed",
                            "conclusion": conclusion,
                            "completed_at": "2026-06-26T00:00:01Z",
                            "app": {{"slug": app_slug}},
                        }}
                    ]
                    if mode == "squash-head":
                        runs = [
                            {{
                                "name": "r5-audit-gate",
                                "status": "completed",
                                "conclusion": "success",
                                "completed_at": "2026-07-02T11:51:00Z",
                                "app": {{"slug": "github-actions"}},
                            }}
                        ]
                    if mode == "taeys-gated":
                        runs = [
                            {{
                                "name": "consultation-v2-integrity",
                                "status": "completed",
                                "conclusion": "success",
                                "completed_at": "2026-07-03T00:00:01Z",
                                "app": {{"slug": "github-actions"}},
                            }}
                        ]
                    print(json.dumps({{"check_runs": runs}}))
                    sys.exit(0)
                if path == f"repos/{{repo}}/commits/{{sha}}/statuses?per_page=100":
                    if mode == "squash-merge":
                        print(json.dumps([]))
                        sys.exit(0)
                    if mode == "squash-failed-r5":
                        state = "failure"
                        creator = "github-actions[bot]"
                    else:
                        state = "success"
                        creator = "attacker-bot" if mode in {{"untrusted-status", "squash-untrusted-r5"}} else "github-actions[bot]"
                    print(json.dumps([
                        {{
                            "context": "r5-audit-gate",
                            "state": state,
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


class _WarningRecorder:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def warning(self, message: str, *args: object) -> None:
        self.messages.append(message % args if args else message)


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
        "ORCH_COMPLETION_REPO_CHECKS",
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
        os.environ.pop("ORCH_COMPLETION_REPO_CHECKS", None)
        os.environ.pop("ORCH_COMPLETION_ALLOWED_REPOS", None)
        check(
            "unset completion repo allowlist has no product default",
            evidence_verification.allowed_completion_repos() == (),
            repr(evidence_verification.allowed_completion_repos()),
        )
        recorder = _WarningRecorder()
        check(
            "unset completion repo allowlist emits UNVERIFIED startup warning",
            evidence_verification.warn_if_completion_allowlist_unset(recorder) is True
            and any("UNVERIFIED" in message and "ORCH_COMPLETION_ALLOWED_REPOS" in message for message in recorder.messages),
            repr(recorder.messages),
        )
        explicit_unset = evidence_verification.verify_completion_evidence(
            {"commit_sha": GREEN_SHA, "repo": ORCH_REPO, "production_observation": "unset allowlist explicit repo probe"},
            producer="tester-api",
        )
        check(
            "unset completion repo allowlist keeps explicit repo UNVERIFIED",
            explicit_unset is not None
            and explicit_unset.get("status") == "UNVERIFIED"
            and explicit_unset.get("verified") is False
            and explicit_unset.get("applies") is True
            and "ORCH_COMPLETION_ALLOWED_REPOS" in explicit_unset.get("reason", ""),
            json.dumps(explicit_unset, sort_keys=True),
        )
        missing_repo_unset = evidence_verification.verify_completion_evidence(
            {"commit_sha": CONDUCTOR_SHA, "production_observation": "unset allowlist inferred repo probe"},
            producer="tester-api",
        )
        check(
            "commit evidence without repo fails before fallback inference",
            missing_repo_unset is not None
            and missing_repo_unset.get("status") == "UNVERIFIED"
            and missing_repo_unset.get("verified") is False
            and missing_repo_unset.get("applies") is True
            and missing_repo_unset.get("reject_completion") is True
            and "completion_evidence.repo" in missing_repo_unset.get("reason", ""),
            json.dumps(missing_repo_unset, sort_keys=True),
        )
        os.environ["ORCH_COMPLETION_ALLOWED_REPOS"] = f"{ORCH_REPO},{CONDUCTOR_REPO},{TAEYS_HANDS_REPO}:gateless"
        os.environ["ORCH_COMPLETION_TRUSTED_CHECK_RUN_APPS"] = "github-actions"
        os.environ["ORCH_COMPLETION_TRUSTED_STATUS_CREATORS"] = "github-actions[bot]"
        check(
            "completion repo allowlist keeps repo names separate from profiles",
            evidence_verification.allowed_completion_repos() == (ORCH_REPO, CONDUCTOR_REPO, TAEYS_HANDS_REPO),
            repr(evidence_verification.allowed_completion_repos()),
        )
        check(
            "completion repo allowlist marks taeys-hands gateless",
            evidence_verification.completion_repo_profiles().get(TAEYS_HANDS_REPO.lower()) == "gateless",
            repr(evidence_verification.completion_repo_profiles()),
        )

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

        os.environ["ORCH_COMPLETION_GITHUB_REPO"] = ORCH_REPO
        gated_observation_task = _seed_task("gated-observation-only")
        gated_observation_response = client.patch(
            f"/api/task/{gated_observation_task}",
            json={
                "status": "completed",
                "from": "tester-api",
                "evidence": {"production_observation": "gated observation-only completion probe"},
            },
        )
        gated_observation_payload = client.get(f"/api/tasks/{gated_observation_task}").json()
        check(
            "gated repo observation-only completion is rejected",
            gated_observation_response.status_code == 400
            and "gated repo" in gated_observation_response.text
            and "commit_sha" in gated_observation_response.text
            and gated_observation_payload.get("status") == "pending",
            f"{gated_observation_response.status_code} {gated_observation_response.text} payload={gated_observation_payload}",
        )

        gated_runtime_override_task = _seed_task("gated-runtime-override")
        gated_runtime_override_response = client.patch(
            f"/api/task/{gated_runtime_override_task}",
            json={
                "status": "completed",
                "from": "tester-api",
                "evidence": {
                    "repo": TAEYS_HANDS_REPO,
                    "production_observation": "gated runtime must not accept explicit gateless override",
                },
            },
        )
        gated_runtime_override_payload = client.get(f"/api/tasks/{gated_runtime_override_task}").json()
        check(
            "gated runtime repo cannot be bypassed by explicit gateless repo",
            gated_runtime_override_response.status_code == 400
            and ORCH_REPO in gated_runtime_override_response.text
            and TAEYS_HANDS_REPO not in gated_runtime_override_response.text
            and "commit_sha" in gated_runtime_override_response.text
            and gated_runtime_override_payload.get("status") == "pending",
            f"{gated_runtime_override_response.status_code} {gated_runtime_override_response.text} "
            f"payload={gated_runtime_override_payload}",
        )

        gated_supervisor_task = _seed_task("gated-supervisor-verification")
        _complete(
            client,
            gated_supervisor_task,
            {
                "supervisor_verification": {
                    "mode": "research",
                    "verifier": "tester-supervisor",
                    "observation": "supervisor inspected the no-commit research artifact",
                },
                "production_observation": "gated runtime supervisor verification probe",
            },
        )
        gated_supervisor_payload = client.get(f"/api/tasks/{gated_supervisor_task}").json()
        gated_supervisor_verification = gated_supervisor_payload.get("completion_evidence_verification", {})
        check(
            "gated runtime repo still accepts valid supervisor verification",
            gated_supervisor_payload.get("status") == "completed"
            and gated_supervisor_payload.get("completion_evidence_verification_status") == "VERIFIED"
            and gated_supervisor_payload.get("completion_evidence_verified") is True
            and gated_supervisor_payload.get("completion_evidence_verification_applies") is True
            and gated_supervisor_verification.get("source") == "supervisor-verification"
            and gated_supervisor_verification.get("verifier") == "tester-supervisor",
            json.dumps(gated_supervisor_verification, sort_keys=True),
        )

        os.environ["ORCH_COMPLETION_GITHUB_REPO"] = TAEYS_HANDS_REPO
        gateless_observation_task = _seed_task("gateless-observation-only")
        _complete(
            client,
            gateless_observation_task,
            {"production_observation": "gateless observation-only completion probe"},
        )
        gateless_observation_payload = client.get(f"/api/tasks/{gateless_observation_task}").json()
        check(
            "gateless repo observation-only completion remains UNVERIFIED but accepted",
            gateless_observation_payload.get("status") == "completed"
            and gateless_observation_payload.get("completion_evidence_verification_status") == "UNVERIFIED"
            and gateless_observation_payload.get("completion_evidence_verified") is False
            and gateless_observation_payload.get("completion_evidence_verification_applies") is False
            and gateless_observation_payload.get("completion_evidence_verification", {}).get("applies") is False
            and gateless_observation_payload.get("completion_evidence_verification", {}).get("repo") == TAEYS_HANDS_REPO
            and "no commit_sha" in gateless_observation_payload.get("completion_evidence_verification", {}).get("reason", ""),
            json.dumps(gateless_observation_payload.get("completion_evidence_verification"), sort_keys=True),
        )
        os.environ["ORCH_COMPLETION_GITHUB_REPO"] = ORCH_REPO

        missing_repo_task = _seed_task("missing-repo")
        missing_repo_response = client.patch(
            f"/api/task/{missing_repo_task}",
            json={
                "status": "completed",
                "from": "tester-api",
                "evidence": {
                    "commit_sha": CONDUCTOR_SHA,
                    "production_observation": "missing repo must fail loud",
                },
            },
        )
        missing_repo_payload = client.get(f"/api/tasks/{missing_repo_task}").json()
        check(
            "commit evidence without repo is rejected loudly",
            missing_repo_response.status_code == 400
            and "completion_evidence.repo" in missing_repo_response.text
            and missing_repo_payload.get("status") == "pending",
            f"{missing_repo_response.status_code} {missing_repo_response.text} payload={missing_repo_payload}",
        )

        taeys_task = _seed_task("taeys-hands")
        taeys_update = _complete(
            client,
            taeys_task,
            {
                "commit_sha": TAEYS_HANDS_SHA,
                "repo": TAEYS_HANDS_REPO,
                "production_observation": "gateless repo merge reached production consult validation",
            },
        )
        taeys_payload = client.get(f"/api/tasks/{taeys_task}").json()
        taeys_verification = taeys_payload.get("completion_evidence_verification", {})
        check(
            "gateless repo commit on default branch verifies without gate contexts",
            taeys_update.get("completion_evidence_verification_status") == "VERIFIED"
            and taeys_payload.get("completion_evidence_verification_status") == "VERIFIED"
            and taeys_payload.get("completion_evidence_verified") is True
            and taeys_verification.get("repo") == TAEYS_HANDS_REPO
            and taeys_verification.get("profile") == "gateless"
            and taeys_verification.get("verifier") == "github-default-branch"
            and taeys_verification.get("required_checks") == [],
            json.dumps(taeys_verification, sort_keys=True),
        )

        taeys_branch_task = _seed_task("taeys-hands-branch")
        _complete(
            client,
            taeys_branch_task,
            {
                "commit_sha": TAEYS_HANDS_BRANCH_SHA,
                "repo": TAEYS_HANDS_REPO,
                "production_observation": "gateless repo feature branch probe",
            },
        )
        taeys_branch_payload = client.get(f"/api/tasks/{taeys_branch_task}").json()
        check(
            "gateless repo commit not on default branch is UNVERIFIED",
            taeys_branch_payload.get("completion_evidence_verification_status") == "UNVERIFIED"
            and taeys_branch_payload.get("completion_evidence_verified") is False
            and "not on default branch" in taeys_branch_payload.get("completion_evidence_verification", {}).get("reason", ""),
            json.dumps(taeys_branch_payload.get("completion_evidence_verification"), sort_keys=True),
        )

        os.environ["ORCH_COMPLETION_ALLOWED_REPOS"] = f"{ORCH_REPO},{CONDUCTOR_REPO},{TAEYS_HANDS_REPO}:gated"
        os.environ["ORCH_COMPLETION_REPO_CHECKS"] = f"{TAEYS_HANDS_REPO}=r5-audit-gate,consultation-v2-integrity"
        check(
            "taeys-hands repo uses repo-specific required checks",
            evidence_verification.required_github_checks_for_repo(TAEYS_HANDS_REPO)
            == ("r5-audit-gate", "consultation-v2-integrity"),
            repr(evidence_verification.required_github_checks_for_repo(TAEYS_HANDS_REPO)),
        )
        check(
            "orchestrator repo still uses global required checks",
            evidence_verification.required_github_checks_for_repo(ORCH_REPO)
            == ("r5-audit-gate", "ship-gate-acceptance"),
            repr(evidence_verification.required_github_checks_for_repo(ORCH_REPO)),
        )
        taeys_gated_task = _seed_task("taeys-hands-gated")
        taeys_gated_update = _complete(
            client,
            taeys_gated_task,
            {
                "commit_sha": TAEYS_HANDS_GATED_SHA,
                "repo": TAEYS_HANDS_REPO,
                "production_observation": "taeys-hands gated completion used repo-specific CI checks",
            },
        )
        taeys_gated_payload = client.get(f"/api/tasks/{taeys_gated_task}").json()
        taeys_gated_verification = taeys_gated_payload.get("completion_evidence_verification", {})
        check(
            "taeys-hands gated repo verifies with r5 plus consultation-v2-integrity and no ship-gate",
            taeys_gated_update.get("completion_evidence_verification_status") == "VERIFIED"
            and taeys_gated_payload.get("completion_evidence_verification_status") == "VERIFIED"
            and taeys_gated_payload.get("completion_evidence_verified") is True
            and taeys_gated_verification.get("repo") == TAEYS_HANDS_REPO
            and taeys_gated_verification.get("profile") != "gateless"
            and taeys_gated_verification.get("required_checks") == ["r5-audit-gate", "consultation-v2-integrity"]
            and "ship-gate-acceptance" not in json.dumps(taeys_gated_verification, sort_keys=True),
            json.dumps(taeys_gated_verification, sort_keys=True),
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
            and missing_payload.get("completion_evidence_verified") is False
            and missing_payload.get("completion_evidence_verification_applies") is True,
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
            and red_payload.get("completion_evidence_verification_applies") is True
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
            and green_payload.get("completion_evidence_verification_applies") is True
            and green_payload.get("completion_evidence", {}).get("repo") == ORCH_REPO
            and green_payload.get("completion_evidence_verification", {}).get("repo") == ORCH_REPO,
            f"update={green_update} payload={green_payload}",
        )
        squash_task = _seed_task("squash-merge")
        squash_update = _complete(
            client,
            squash_task,
            {
                "commit_sha": SQUASH_MERGE_SHA,
                "repo": ORCH_REPO,
                "production_observation": "squash merge commit with r5 on merged PR head",
            },
        )
        squash_payload = client.get(f"/api/tasks/{squash_task}").json()
        squash_verification = squash_payload.get("completion_evidence_verification", {})
        squash_checks = squash_verification.get("checks") or []
        check(
            "squash merge evidence verifies via merged PR head r5 provenance",
            squash_update.get("completion_evidence_verification_status") == "VERIFIED"
            and squash_payload.get("completion_evidence_verification_status") == "VERIFIED"
            and squash_payload.get("completion_evidence_verified") is True
            and "merged same-repo PR head" in squash_verification.get("reason", "")
            and any(
                item.get("name") == "r5-audit-gate"
                and item.get("source") == "merged-pr-head"
                and item.get("source_commit_sha") == SQUASH_HEAD_SHA
                and item.get("merge_commit_sha") == SQUASH_MERGE_SHA
                and item.get("source_pr") == 252
                for item in squash_checks
            )
            and any(
                item.get("name") == "ship-gate-acceptance"
                and item.get("kind") == "check-run"
                and item.get("source") != "merged-pr-head"
                for item in squash_checks
            ),
            json.dumps(squash_verification, sort_keys=True),
        )
        for suffix, merge_sha, label in (
            ("squash-failed-r5", SQUASH_FAILED_R5_SHA, "failed"),
            ("squash-untrusted-r5", SQUASH_UNTRUSTED_R5_SHA, "untrusted"),
        ):
            guarded_task = _seed_task(suffix)
            guarded_update = _complete(
                client,
                guarded_task,
                {
                    "commit_sha": merge_sha,
                    "repo": ORCH_REPO,
                    "production_observation": f"squash merge commit with {label} r5 on merge commit",
                },
            )
            guarded_payload = client.get(f"/api/tasks/{guarded_task}").json()
            guarded_verification = guarded_payload.get("completion_evidence_verification", {})
            guarded_checks = guarded_verification.get("checks") or []
            check(
                f"squash merge with {label} cited r5 is not rescued by PR-head provenance",
                guarded_update.get("completion_evidence_verification_status") == "UNVERIFIED"
                and guarded_payload.get("completion_evidence_verification_status") == "UNVERIFIED"
                and guarded_payload.get("completion_evidence_verified") is False
                and "r5-audit-gate" in guarded_verification.get("reason", "")
                and not any(item.get("source") == "merged-pr-head" for item in guarded_checks),
                json.dumps(guarded_verification, sort_keys=True),
            )
        open_pr_task = _seed_task("open-pr")
        open_pr_response = client.patch(
            f"/api/task/{open_pr_task}",
            json={
                "status": "completed",
                "from": "tester-api",
                "evidence": {
                    "commit_sha": OPEN_PR_SHA,
                    "repo": ORCH_REPO,
                    "production_observation": "open PR completion must not be accepted",
                },
            },
        )
        open_pr_payload = client.get(f"/api/tasks/{open_pr_task}").json()
        check(
            "open pull request commit evidence is rejected before completion",
            open_pr_response.status_code == 400
            and "open pull request" in open_pr_response.text
            and open_pr_payload.get("status") == "pending",
            f"{open_pr_response.status_code} {open_pr_response.text} payload={open_pr_payload}",
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
