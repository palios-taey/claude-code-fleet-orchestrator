#!/usr/bin/env python3
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PREFIX = f"premerge-{uuid.uuid4().hex[:8]}"
HEAD_SHA = "0123456789abcdef0123456789abcdef01234567"
REPO = "palios-taey/claude-code-fleet-orchestrator"
PR_NUM = 77
PR_BRANCH = "codex/premerge-branch"

if "ORCH_DOTENV" not in os.environ:
    for candidate in (
        ROOT / ".env",
        Path.home() / "claude-code-fleet-orchestrator/.env",
    ):
        if candidate.is_file():
            os.environ["ORCH_DOTENV"] = str(candidate)
            break

from fleet_orchestrator.config import OrchConfig, get_neo4j_driver  # noqa: E402
from fleet_orchestrator.orch_schema import create_phase, create_project, create_task, update_task_status  # noqa: E402


CFG = OrchConfig()


def _cleanup(prefix: str) -> None:
    driver = get_neo4j_driver(CFG)
    with driver.session(database=CFG.neo4j_db) as session:
        session.run("MATCH (t:OrchTask) WHERE t.id STARTS WITH $prefix DETACH DELETE t", prefix=prefix)
        session.run("MATCH (ph:OrchPhase) WHERE ph.id STARTS WITH $prefix DETACH DELETE ph", prefix=prefix)
        session.run("MATCH (p:OrchProject) WHERE p.id STARTS WITH $prefix DETACH DELETE p", prefix=prefix)


def _seed_gate_task(evidence_sha: str = HEAD_SHA) -> str:
    project_id = f"{PREFIX}-project"
    phase_id = f"{PREFIX}-phase"
    task_id = f"{PREFIX}-gate-task"
    create_project(project_id, "pre-merge gate probe", supervisor="tester", priority=1, config=CFG)
    create_phase(project_id, phase_id, "CONTROL", config=CFG)
    create_task(
        phase_id,
        task_id,
        "gate task must close with committed evidence",
        owner="tester-codex",
        priority=5,
        wake_owner_if_ready=False,
        config=CFG,
    )
    update_task_status(
        task_id,
        "completed",
        owner="tester-codex",
        completed_by="tester-codex",
        completion_evidence={
            "commit_sha": evidence_sha,
            "gate_run_id": f"{PREFIX}-gate-run",
            "production_observation": "isolated pre-merge gate probe",
        },
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
            import os
            import sys

            if len(sys.argv) < 3 or sys.argv[1] != "api":
                print("unsupported fake gh invocation", file=sys.stderr)
                sys.exit(2)

            path = sys.argv[2]
            sha = os.environ["PROBE_HEAD_SHA"]
            branch = os.environ.get("PROBE_HEAD_BRANCH", "codex/premerge-branch")
            if path == f"repos/{REPO}/pulls/{PR_NUM}":
                print(json.dumps({{
                    "head": {{
                        "sha": sha,
                        "ref": branch,
                        "repo": {{"full_name": "{REPO}"}}
                    }}
                }}))
                sys.exit(0)
            if path == f"repos/{REPO}/commits/{{sha}}/check-runs?per_page=100":
                mode = os.environ.get("PROBE_CHECK_MODE", "green")
                ship_conclusion = "failure" if mode == "ship-fail" else "success"
                print(json.dumps({{
                    "check_runs": [
                        {{"name": "ship-gate-acceptance", "status": "completed", "conclusion": ship_conclusion, "completed_at": "2026-06-12T00:00:01Z"}}
                    ]
                }}))
                sys.exit(0)
            if path == f"repos/{REPO}/commits/{{sha}}/statuses?per_page=100":
                print(json.dumps([
                    {{"context": "r5-audit-gate", "state": "success", "created_at": "2026-06-12T00:00:00Z"}}
                ]))
                sys.exit(0)
            print(f"unexpected path {{path}}", file=sys.stderr)
            sys.exit(2)
            """
        )
    )
    gh.chmod(0o755)


def _git(args: list[str], cwd: Path) -> None:
    result = subprocess.run(["git", *args], cwd=cwd, text=True, capture_output=True, check=False)
    if result.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed:\nstdout={result.stdout}\nstderr={result.stderr}")


def _seed_repo_for_pr(tmp: Path) -> tuple[Path, Path, Path]:
    repo = tmp / "repo"
    peer_root = tmp / "peer-worktrees"
    peer_root.mkdir()
    repo.mkdir()
    _git(["init"], repo)
    _git(["config", "user.email", "premerge@example.invalid"], repo)
    _git(["config", "user.name", "premerge acceptance"], repo)
    _git(["switch", "-c", "main"], repo)
    (repo / "README.md").write_text("root\n", encoding="utf-8")
    _git(["add", "README.md"], repo)
    _git(["commit", "-m", "root"], repo)
    _git(["switch", "-c", PR_BRANCH], repo)
    (repo / "branch.txt").write_text("branch\n", encoding="utf-8")
    _git(["add", "branch.txt"], repo)
    _git(["commit", "-m", "branch"], repo)
    _git(["switch", "main"], repo)
    peer_worktree = peer_root / "worker-codex"
    _git(["worktree", "add", str(peer_worktree), PR_BRANCH], repo)
    return repo, peer_root, peer_worktree


def _run_gate(
    fake_bin: Path,
    task_id: str,
    *,
    check_mode: str = "green",
    use_pr: bool = False,
    local_repo: Path | None = None,
    peer_root: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PATH"] = f"{fake_bin}{os.pathsep}{env['PATH']}"
    env["PROBE_HEAD_SHA"] = HEAD_SHA
    env["PROBE_HEAD_BRANCH"] = PR_BRANCH
    env["PROBE_CHECK_MODE"] = check_mode
    target = ["--pr", str(PR_NUM)] if use_pr else ["--sha", HEAD_SHA]
    if local_repo is not None:
        target.extend(["--local-repo", str(local_repo)])
    if peer_root is not None:
        target.extend(["--peer-worktree-root", str(peer_root)])
    return subprocess.run(
        [
            str(ROOT / "scripts/orch-pre-merge-gate"),
            "--repo",
            REPO,
            *target,
            "--gate-task",
            task_id,
        ],
        cwd=ROOT,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def main() -> int:
    _cleanup(PREFIX)
    tmp = Path(tempfile.mkdtemp(prefix=f"{PREFIX}-"))
    failures = []

    def check(label: str, cond: bool, detail: str = "") -> None:
        print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {detail}"))
        if not cond:
            failures.append(label)

    try:
        _write_fake_gh(tmp)
        task_id = _seed_gate_task()
        local_repo, peer_root, peer_worktree = _seed_repo_for_pr(tmp)

        success = _run_gate(tmp, task_id)
        check("green checks + closed committed gate task pass", success.returncode == 0, success.stderr or success.stdout)

        pr_success = _run_gate(tmp, task_id, use_pr=True, local_repo=local_repo, peer_root=peer_root)
        detached = subprocess.run(
            ["git", "-C", str(peer_worktree), "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        check("PR pre-merge gate detaches peer worktree branch",
              pr_success.returncode == 0 and detached.stdout.strip() == "HEAD" and "worktree_pruned=1" in pr_success.stdout,
              pr_success.stderr or pr_success.stdout or detached.stdout)

        ci_fail = _run_gate(tmp, task_id, check_mode="ship-fail")
        check("red required check refuses merge", ci_fail.returncode != 0 and "ship-gate-acceptance" in ci_fail.stderr,
              ci_fail.stderr or ci_fail.stdout)

        wrong_task = _seed_gate_task("fedcba9876543210fedcba9876543210fedcba98")
        evidence_fail = _run_gate(tmp, wrong_task)
        check("closed gate task with different commit refuses merge",
              evidence_fail.returncode != 0 and "does not match head SHA" in evidence_fail.stderr,
              evidence_fail.stderr or evidence_fail.stdout)

        if failures:
            print(f"\nFAIL - {len(failures)} assertion(s): {failures}")
            return 1
        print("\nPASS - pre-merge gate enforces CI-green plus committed gate evidence")
        return 0
    finally:
        _cleanup(PREFIX)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
