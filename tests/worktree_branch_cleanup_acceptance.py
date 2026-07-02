#!/usr/bin/env python3
"""Acceptance: merged branch cleanup detaches peer worktrees before remote delete."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "orch-prune-merged-branches"
FAILURES: list[str] = []


def _check(label: str, condition: bool, detail: object = "") -> None:
    print(("PASS " if condition else "FAIL ") + label + ("" if condition else f" -> {detail}"))
    if not condition:
        FAILURES.append(label)


def _run(
    args: list[str],
    cwd: Path,
    *,
    check: bool = True,
    env: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    merged_env = os.environ.copy()
    if env:
        merged_env.update(env)
    result = subprocess.run(args, cwd=cwd, env=merged_env, capture_output=True, text=True, check=False)
    if check and result.returncode != 0:
        raise AssertionError(f"{args!r} failed:\nstdout={result.stdout}\nstderr={result.stderr}")
    return result


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return _run(["git", "-C", str(repo), *args], repo, check=check)


def _commit(repo: Path, filename: str, content: str, message: str) -> str:
    (repo / filename).write_text(content, encoding="utf-8")
    _git(repo, "add", filename)
    _git(repo, "commit", "-m", message)
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _remote_has(origin: Path, branch: str) -> bool:
    result = _run(["git", "ls-remote", "--heads", str(origin), branch], ROOT)
    return bool(result.stdout.strip())


def _create_branch(repo: Path, branch: str, filename: str, *, merge: bool) -> None:
    _git(repo, "switch", "main")
    _git(repo, "switch", "-c", branch)
    _commit(repo, filename, f"{branch}\n", f"{branch} commit")
    _git(repo, "push", "-u", "origin", branch)
    if merge:
        _git(repo, "switch", "main")
        _git(repo, "merge", "--no-ff", branch, "-m", f"merge {branch}")
        _git(repo, "push", "origin", "main")


def _write_fake_gh(fake_bin: Path, branch: str, head: str) -> None:
    gh = fake_bin / "gh"
    gh.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import json
            import sys

            if sys.argv[1:3] != ["pr", "list"]:
                print("unsupported fake gh", file=sys.stderr)
                sys.exit(2)
            print(json.dumps([{{
                "number": 991,
                "state": "MERGED",
                "mergedAt": "2026-07-01T00:00:00Z",
                "baseRefName": "main",
                "headRefName": "{branch}",
                "headRefOid": "{head}",
                "title": "squash merged fixture"
            }}]))
            """
        ),
        encoding="utf-8",
    )
    gh.chmod(0o755)


def main() -> int:
    syntax = _run(["python", "-m", "py_compile", str(SCRIPT), str(ROOT / "fleet_orchestrator" / "git_branch_cleanup.py")], ROOT)
    _check("cleanup scripts compile", syntax.returncode == 0, syntax.stderr)

    with tempfile.TemporaryDirectory(prefix="orch-branch-cleanup-") as raw_tmp:
        tmp = Path(raw_tmp)
        origin = tmp / "origin.git"
        repo = tmp / "repo"
        peer_root = tmp / "peer-worktrees"
        outside_root = tmp / "outside-worktrees"
        peer_root.mkdir()
        outside_root.mkdir()

        _run(["git", "init", "--bare", str(origin)], ROOT)
        _run(["git", "clone", str(origin), str(repo)], ROOT)
        _git(repo, "config", "user.email", "cleanup@example.invalid")
        _git(repo, "config", "user.name", "cleanup acceptance")
        _git(repo, "switch", "-c", "main")
        _commit(repo, "README.md", "root\n", "root")
        _git(repo, "push", "-u", "origin", "main")

        _create_branch(repo, "codex/merged", "merged.txt", merge=True)
        _create_branch(repo, "codex/unmerged", "unmerged.txt", merge=False)
        _create_branch(repo, "codex/dirty-peer", "dirty-peer.txt", merge=True)
        _git(repo, "switch", "main")
        peer_worktree = peer_root / "worker-codex"
        _git(repo, "worktree", "add", str(peer_worktree), "codex/merged")
        dirty_peer_worktree = peer_root / "dirty-worker-codex"
        _git(repo, "worktree", "add", str(dirty_peer_worktree), "codex/dirty-peer")
        (dirty_peer_worktree / "local-only.txt").write_text("do not discard\n", encoding="utf-8")

        cleanup = _run(
            [
                str(SCRIPT),
                "--repo",
                str(repo),
                "--pattern",
                "codex/*",
                "--peer-worktree-root",
                str(peer_root),
                "--delete-merged",
                "--json",
            ],
            ROOT,
        )
        rows = json.loads(cleanup.stdout)
        deleted = {row["branch"] for row in rows if row.get("deleted")}
        skipped = {row["branch"]: row.get("skipped_reason") for row in rows if not row.get("deleted")}
        _check("merged branch is deleted", deleted == {"codex/merged"}, rows)
        _check("unmerged branch is skipped and listed", skipped.get("codex/unmerged") == "unmerged", rows)
        _check("dirty peer worktree branch is skipped and listed",
               skipped.get("codex/dirty-peer") == "dirty-peer-worktree",
               rows)
        _check("deleted branch is gone from remote", not _remote_has(origin, "codex/merged"), cleanup.stdout)
        _check("unmerged branch remains on remote", _remote_has(origin, "codex/unmerged"), cleanup.stdout)
        _check("dirty peer branch remains on remote", _remote_has(origin, "codex/dirty-peer"), cleanup.stdout)
        detached = _git(peer_worktree, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
        _check("peer worktree was detached before delete", detached == "HEAD", detached)

        _create_branch(repo, "codex/squashed", "squashed.txt", merge=False)
        squashed_head = _git(repo, "rev-parse", "origin/codex/squashed").stdout.strip()
        _git(repo, "switch", "main")
        fake_bin = tmp / "fake-bin"
        fake_bin.mkdir()
        _write_fake_gh(fake_bin, "codex/squashed", squashed_head)
        squash_cleanup = _run(
            [
                str(SCRIPT),
                "--repo",
                str(repo),
                "--pattern",
                "codex/squashed",
                "--github-repo",
                "palios-taey/fixture",
                "--delete-merged",
                "--json",
            ],
            ROOT,
            env={"PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}"},
        )
        squash_rows = json.loads(squash_cleanup.stdout)
        _check("matching merged GitHub PR proves squash-merged branch",
               squash_rows and squash_rows[0].get("merge_proof") == "github-merged-pr",
               squash_rows)
        _check("squash-merged branch is deleted", not _remote_has(origin, "codex/squashed"), squash_rows)

        _create_branch(repo, "codex/nonpeer", "nonpeer.txt", merge=True)
        _git(repo, "switch", "main")
        nonpeer_worktree = outside_root / "worker-codex"
        _git(repo, "worktree", "add", str(nonpeer_worktree), "codex/nonpeer")
        blocked = _run(
            [
                str(SCRIPT),
                "--repo",
                str(repo),
                "--pattern",
                "codex/nonpeer",
                "--peer-worktree-root",
                str(peer_root),
                "--delete-merged",
            ],
            ROOT,
            check=False,
        )
        _check("non-peer worktree checkout blocks deletion", blocked.returncode != 0, blocked.stdout or blocked.stderr)
        _check("blocked branch remains on remote", _remote_has(origin, "codex/nonpeer"), blocked.stdout or blocked.stderr)

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - merged branch cleanup detaches peer worktrees and preserves unmerged work.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
