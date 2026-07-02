from __future__ import annotations

import fnmatch
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence


DEFAULT_REMOTE_BRANCH_PATTERNS = ("codex/*",)


class WorktreeRefPruneBlocked(RuntimeError):
    def __init__(self, message: str, *, skipped_reason: str, blockers: Sequence[str], skippable: bool) -> None:
        super().__init__(message)
        self.skipped_reason = skipped_reason
        self.blockers = list(blockers)
        self.skippable = skippable


def _run_git(repo: Path, args: Sequence[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise RuntimeError(f"git {' '.join(args)} failed in {repo}: {detail}")
    return result


def _git_stdout(repo: Path, args: Sequence[str]) -> str:
    return _run_git(repo, args).stdout


def _normalize_path(path: str | Path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def peer_worktree_roots(values: Optional[Iterable[str]] = None) -> List[Path]:
    if values is None:
        raw = os.environ.get("ORCH_PEER_WORKTREE_ROOTS", "").strip()
        if raw:
            values = [part.strip() for chunk in raw.split(os.pathsep) for part in chunk.split(",") if part.strip()]
        else:
            values = [str(Path.home() / ".peer-worktrees")]
    roots = [_normalize_path(value) for value in values if str(value).strip()]
    if not roots:
        raise RuntimeError("at least one peer worktree root is required")
    return roots


def _parse_worktree_porcelain(raw: str) -> List[Dict[str, str]]:
    records: List[Dict[str, str]] = []
    current: Dict[str, str] = {}
    for line in raw.splitlines():
        if not line.strip():
            if current:
                records.append(current)
                current = {}
            continue
        key, _, value = line.partition(" ")
        current[key] = value
    if current:
        records.append(current)
    return records


def _local_branch_ref(branch: str) -> str:
    stripped = branch.strip()
    if not stripped:
        raise RuntimeError("branch name is required")
    if stripped.startswith("refs/heads/"):
        return stripped
    return f"refs/heads/{stripped}"


def _local_branch_name(branch: str) -> str:
    ref = _local_branch_ref(branch)
    return ref.removeprefix("refs/heads/")


def _branch_worktrees(repo: Path, branch: str) -> List[Dict[str, str]]:
    target_ref = _local_branch_ref(branch)
    raw = _git_stdout(repo, ["worktree", "list", "--porcelain"])
    return [record for record in _parse_worktree_porcelain(raw) if record.get("branch") == target_ref]


def _status_porcelain(worktree: Path) -> str:
    return _git_stdout(worktree, ["status", "--porcelain"]).strip()


def detach_branch_worktree_refs(
    repo: str | Path,
    branch: str,
    *,
    peer_roots: Optional[Iterable[str]] = None,
    dirty_peer_is_skippable: bool = False,
) -> List[Dict[str, Any]]:
    """Detach clean peer worktrees that currently pin ``branch``.

    Non-peer checkouts and dirty peer checkouts fail loud. That makes CONTROL
    branch deletion safe without silently discarding work.
    """
    repo_path = _normalize_path(repo)
    roots = peer_worktree_roots(peer_roots)
    _run_git(repo_path, ["worktree", "prune"])

    actions: List[Dict[str, Any]] = []
    hard_blockers: List[str] = []
    dirty_peer_blockers: List[str] = []
    for record in _branch_worktrees(repo_path, branch):
        raw_path = record.get("worktree", "")
        worktree = _normalize_path(raw_path)
        if not any(_is_under(worktree, root) for root in roots):
            hard_blockers.append(f"{worktree} is not under peer worktree roots {[str(root) for root in roots]}")
            continue
        dirty = _status_porcelain(worktree)
        if dirty:
            dirty_peer_blockers.append(f"{worktree} has uncommitted changes")
            continue
        before_head = record.get("HEAD", "")
        _run_git(worktree, ["switch", "--detach"])
        actions.append({
            "action": "detached",
            "branch": _local_branch_name(branch),
            "worktree": str(worktree),
            "head": before_head,
        })
    if hard_blockers or dirty_peer_blockers:
        blockers = [*hard_blockers, *dirty_peer_blockers]
        skippable = dirty_peer_is_skippable and not hard_blockers
        skipped_reason = "dirty-peer-worktree" if skippable else "worktree-ref-blocked"
        raise WorktreeRefPruneBlocked(
            f"cannot prune worktree refs for branch {_local_branch_name(branch)!r}: {'; '.join(blockers)}",
            skipped_reason=skipped_reason,
            blockers=blockers,
            skippable=skippable,
        )
    return actions


def _remote_branch_refs(repo: Path, remote: str) -> List[str]:
    raw = _git_stdout(repo, ["for-each-ref", f"refs/remotes/{remote}", "--format=%(refname:short)"])
    prefix = f"{remote}/"
    refs = []
    for line in raw.splitlines():
        ref = line.strip()
        if not ref or ref == f"{remote}/HEAD" or not ref.startswith(prefix):
            continue
        refs.append(ref)
    return refs


def _is_ancestor(repo: Path, ancestor_ref: str, descendant_ref: str) -> bool:
    result = _run_git(repo, ["merge-base", "--is-ancestor", ancestor_ref, descendant_ref], check=False)
    if result.returncode == 0:
        return True
    if result.returncode == 1:
        return False
    detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
    raise RuntimeError(f"git merge-base --is-ancestor {ancestor_ref} {descendant_ref} failed: {detail}")


def _github_merged_pr(
    *,
    github_repo: str,
    branch: str,
    branch_oid: str,
    base_branch: str,
) -> Optional[Dict[str, Any]]:
    result = subprocess.run(
        [
            "gh",
            "pr",
            "list",
            "--repo",
            github_repo,
            "--head",
            branch,
            "--state",
            "merged",
            "--json",
            "number,state,mergedAt,baseRefName,headRefName,headRefOid,title",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise RuntimeError(f"gh pr list for branch {branch!r} failed: {detail}")
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gh pr list for branch {branch!r} returned invalid JSON: {exc}") from exc
    if not isinstance(payload, list):
        raise RuntimeError(f"gh pr list for branch {branch!r} did not return a list")
    for item in payload:
        if not isinstance(item, dict):
            continue
        if item.get("state") != "MERGED" or not item.get("mergedAt"):
            continue
        if item.get("baseRefName") != base_branch or item.get("headRefName") != branch:
            continue
        if str(item.get("headRefOid") or "").lower() != branch_oid.lower():
            continue
        return item
    return None


def cleanup_merged_remote_branches(
    repo: str | Path,
    *,
    remote: str = "origin",
    base: str = "origin/main",
    base_branch: str = "main",
    patterns: Iterable[str] = DEFAULT_REMOTE_BRANCH_PATTERNS,
    delete_merged: bool = False,
    peer_roots: Optional[Iterable[str]] = None,
    fetch: bool = True,
    github_repo: Optional[str] = None,
) -> List[Dict[str, Any]]:
    repo_path = _normalize_path(repo)
    pattern_list = [pattern for pattern in patterns if pattern]
    if not pattern_list:
        raise RuntimeError("at least one remote branch pattern is required")
    if fetch:
        _run_git(repo_path, ["fetch", "--prune", remote])

    rows: List[Dict[str, Any]] = []
    prefix = f"{remote}/"
    for remote_ref in _remote_branch_refs(repo_path, remote):
        branch = remote_ref.removeprefix(prefix)
        if not any(fnmatch.fnmatchcase(branch, pattern) for pattern in pattern_list):
            continue
        branch_oid = _git_stdout(repo_path, ["rev-parse", remote_ref]).strip()
        merge_proof = "git-ancestor" if _is_ancestor(repo_path, remote_ref, base) else None
        merged_pr = None
        if merge_proof is None and github_repo:
            merged_pr = _github_merged_pr(
                github_repo=github_repo,
                branch=branch,
                branch_oid=branch_oid,
                base_branch=base_branch,
            )
            if merged_pr:
                merge_proof = "github-merged-pr"
        row: Dict[str, Any] = {
            "branch": branch,
            "head": branch_oid,
            "remote_ref": remote_ref,
            "merged_into": base if merge_proof else None,
            "merge_proof": merge_proof,
            "deleted": False,
            "worktree_actions": [],
        }
        if merged_pr:
            row["pull_request"] = {
                "number": merged_pr.get("number"),
                "merged_at": merged_pr.get("mergedAt"),
                "title": merged_pr.get("title"),
            }
        if not merge_proof:
            row["skipped_reason"] = "unmerged"
        elif not delete_merged:
            row["skipped_reason"] = "dry-run"
        else:
            try:
                row["worktree_actions"] = detach_branch_worktree_refs(
                    repo_path,
                    branch,
                    peer_roots=peer_roots,
                    dirty_peer_is_skippable=True,
                )
            except WorktreeRefPruneBlocked as exc:
                if not exc.skippable:
                    raise
                row["skipped_reason"] = exc.skipped_reason
                row["worktree_blockers"] = exc.blockers
                rows.append(row)
                continue
            _run_git(repo_path, ["push", remote, "--delete", branch])
            row["deleted"] = True
        rows.append(row)
    return rows
