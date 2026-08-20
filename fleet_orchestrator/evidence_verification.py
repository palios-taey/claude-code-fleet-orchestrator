from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .careers_loop_proof import verify_loop_proof_receipt


DEFAULT_REQUIRED_GITHUB_CHECKS = ("r5-audit-gate", "ship-gate-acceptance")
MERGED_PR_HEAD_PROVENANCE_CHECKS = ("r5-audit-gate",)
DEFAULT_TRUSTED_CHECK_RUN_APPS = ("github-actions",)
DEFAULT_TRUSTED_STATUS_CREATORS = ("github-actions[bot]",)
GATED_REPO_PROFILE = "gated"
GATELESS_REPO_PROFILE = "gateless"
DEFAULT_REPO_PROFILE = GATED_REPO_PROFILE
SUPPORTED_REPO_PROFILES = frozenset({GATED_REPO_PROFILE, GATELESS_REPO_PROFILE})
SUPERVISOR_VERIFICATION_SOURCE = "supervisor-verification"
SUPERVISOR_VERIFICATION_MODES = frozenset({"prototype", "research"})
VERIFIED = "VERIFIED"
UNVERIFIED = "UNVERIFIED"
_GH_TIMEOUT_SEC = 10
COMPLETION_ALLOWLIST_UNSET_WARNING = (
    "ORCH_COMPLETION_ALLOWED_REPOS unset - all commit-based completions will be UNVERIFIED until configured"
)


def required_github_checks() -> Tuple[str, ...]:
    raw = os.environ.get("ORCH_COMPLETION_REQUIRED_CHECKS") or os.environ.get("ORCH_PRE_MERGE_REQUIRED_CHECKS", "")
    values = [part.strip() for part in raw.split(",") if part.strip()] if raw else list(DEFAULT_REQUIRED_GITHUB_CHECKS)
    checks = tuple(dict.fromkeys(values))
    return checks or DEFAULT_REQUIRED_GITHUB_CHECKS


def _parse_repo_required_checks_entry(entry: str) -> Optional[Tuple[str, Tuple[str, ...]]]:
    text = entry.strip()
    if not text or "=" not in text:
        return None
    repo, raw_checks = text.split("=", 1)
    repo = repo.strip()
    if "/" not in repo:
        return None
    checks = tuple(dict.fromkeys(part.strip() for part in raw_checks.split(",") if part.strip()))
    if not checks:
        return None
    return repo, checks


def completion_repo_required_checks() -> Dict[str, Tuple[str, ...]]:
    raw = os.environ.get("ORCH_COMPLETION_REPO_CHECKS", "")
    entries = raw.split(";") if ";" in raw else [raw]
    required_checks: Dict[str, Tuple[str, ...]] = {}
    for entry in entries:
        parsed = _parse_repo_required_checks_entry(entry)
        if not parsed:
            continue
        repo, checks = parsed
        required_checks[repo.lower()] = checks
    return required_checks


def required_github_checks_for_repo(repo: str) -> Tuple[str, ...]:
    return completion_repo_required_checks().get(repo.strip().lower(), required_github_checks())


def _csv_env_values(name: str, defaults: Iterable[str]) -> Tuple[str, ...]:
    raw = os.environ.get(name, "")
    values = [part.strip() for part in raw.split(",") if part.strip()] if raw else list(defaults)
    return tuple(dict.fromkeys(values))


def allowed_completion_repos() -> Tuple[str, ...]:
    raw = os.environ.get("ORCH_COMPLETION_ALLOWED_REPOS", "")
    values = []
    for part in raw.split(","):
        parsed = _parse_completion_repo_entry(part)
        if parsed:
            values.append(parsed[0])
    return tuple(dict.fromkeys(values))


def _parse_completion_repo_entry(entry: str) -> Optional[Tuple[str, str]]:
    text = entry.strip()
    if not text:
        return None
    repo = text
    profile = DEFAULT_REPO_PROFILE
    for separator in ("=", ":"):
        if separator not in text:
            continue
        candidate_repo, candidate_profile = text.rsplit(separator, 1)
        if "/" not in candidate_repo:
            continue
        repo = candidate_repo.strip()
        profile = candidate_profile.strip().lower() or DEFAULT_REPO_PROFILE
        break
    if not repo:
        return None
    return repo, profile


def completion_repo_profiles() -> Dict[str, str]:
    raw = os.environ.get("ORCH_COMPLETION_ALLOWED_REPOS", "")
    profiles: Dict[str, str] = {}
    for part in raw.split(","):
        parsed = _parse_completion_repo_entry(part)
        if not parsed:
            continue
        repo, profile = parsed
        profiles[repo.lower()] = profile
    return profiles


def warn_if_completion_allowlist_unset(logger: Optional[logging.Logger] = None) -> bool:
    if allowed_completion_repos():
        return False
    (logger or logging.getLogger(__name__)).warning(COMPLETION_ALLOWLIST_UNSET_WARNING)
    return True


def trusted_check_run_apps() -> Tuple[str, ...]:
    return _csv_env_values("ORCH_COMPLETION_TRUSTED_CHECK_RUN_APPS", DEFAULT_TRUSTED_CHECK_RUN_APPS)


def trusted_status_creators() -> Tuple[str, ...]:
    return _csv_env_values("ORCH_COMPLETION_TRUSTED_STATUS_CREATORS", DEFAULT_TRUSTED_STATUS_CREATORS)


def _gh_api(path: str) -> Any:
    try:
        result = subprocess.run(
            ["gh", "api", path],
            capture_output=True,
            text=True,
            check=False,
            timeout=_GH_TIMEOUT_SEC,
        )
    except FileNotFoundError as exc:
        raise RuntimeError("gh CLI is unavailable") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"gh api {path!r} timed out after {_GH_TIMEOUT_SEC}s") from exc
    except OSError as exc:
        raise RuntimeError(f"gh api {path!r} could not start: {exc}") from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or f"exit {result.returncode}"
        raise RuntimeError(f"gh api {path!r} failed: {detail}")
    try:
        return json.loads(result.stdout or "null")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gh api {path!r} returned invalid JSON: {exc}") from exc


def github_repo_from_environment_or_gh() -> str:
    repo = os.environ.get("ORCH_COMPLETION_GITHUB_REPO", "").strip()
    if repo:
        return repo
    repo = os.environ.get("GITHUB_REPOSITORY", "").strip()
    if repo:
        return repo
    payload = _gh_api("repos/:owner/:repo")
    repo = payload.get("full_name") if isinstance(payload, dict) else None
    if not repo:
        raise RuntimeError("could not infer GitHub repository; set ORCH_COMPLETION_GITHUB_REPO=OWNER/REPO")
    return str(repo)


def repo_from_completion_evidence(evidence: Dict[str, Any]) -> str:
    return str(evidence.get("repo") or "").strip()


def completion_evidence_verification_applies(evidence: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(evidence, dict) or not evidence:
        return False
    if str(evidence.get("commit_sha") or "").strip():
        return True
    if "loop_proof" in evidence or "supervisor_verification" in evidence:
        return True
    gated, _repo = _no_commit_evidence_targets_gated_repo(evidence)
    if gated:
        return True
    return False


def _repo_allowed_for_completion_evidence(repo: str) -> bool:
    return repo.strip().lower() in completion_repo_profiles()


def _repo_profile_for_completion_evidence(repo: str) -> str:
    return completion_repo_profiles().get(repo.strip().lower(), "")


def _configured_runtime_completion_repo() -> str:
    return (
        os.environ.get("ORCH_COMPLETION_GITHUB_REPO", "").strip()
        or os.environ.get("GITHUB_REPOSITORY", "").strip()
    )


def _no_commit_completion_repo(evidence: Dict[str, Any]) -> str:
    return _configured_runtime_completion_repo() or repo_from_completion_evidence(evidence)


def _no_commit_evidence_targets_gated_repo(evidence: Optional[Dict[str, Any]]) -> Tuple[bool, str]:
    if not isinstance(evidence, dict) or not evidence:
        return False, ""
    if str(evidence.get("commit_sha") or "").strip():
        return False, ""
    repo = _no_commit_completion_repo(evidence)
    if not repo:
        return False, ""
    return _repo_profile_for_completion_evidence(repo) == GATED_REPO_PROFILE, repo


def _repo_not_allowed_reason(repo: str, *, inferred: bool = False) -> str:
    source = "inferred completion evidence repo" if inferred else "completion evidence repo"
    if allowed_completion_repos():
        return (
            f"{source} {repo!r} is not in the ORCH_COMPLETION_ALLOWED_REPOS allowlist; "
            "arbitrary forks cannot satisfy VERIFIED provenance"
        )
    return (
        f"{source} {repo!r} cannot satisfy VERIFIED provenance because ORCH_COMPLETION_ALLOWED_REPOS is unset; "
        "set ORCH_COMPLETION_ALLOWED_REPOS=OWNER/REPO[:gated|:gateless][,OWNER/REPO[:gated|:gateless]...] "
        "to enable verified completions"
    )


def _check_run_app_slug(run: Dict[str, Any]) -> str:
    app = run.get("app")
    if not isinstance(app, dict):
        return ""
    return str(app.get("slug") or "").strip()


def _status_creator_login(status: Dict[str, Any]) -> str:
    creator = status.get("creator")
    if not isinstance(creator, dict):
        return ""
    return str(creator.get("login") or "").strip()


def _trusted_check_run(run: Dict[str, Any]) -> bool:
    slug = _check_run_app_slug(run)
    return slug.lower() in {allowed.lower() for allowed in trusted_check_run_apps()}


def _trusted_status(status: Dict[str, Any]) -> bool:
    login = _status_creator_login(status)
    return login.lower() in {allowed.lower() for allowed in trusted_status_creators()}


def _latest_named(items: Iterable[Dict[str, Any]], field: str, name: str) -> Optional[Dict[str, Any]]:
    matches = [item for item in items if item.get(field) == name]
    if not matches:
        return None
    return sorted(
        matches,
        key=lambda item: item.get("completed_at") or item.get("started_at") or item.get("created_at") or "",
        reverse=True,
    )[0]


def _check_run_state(repo: str, sha: str, check: str) -> Tuple[bool, Dict[str, Any]]:
    payload = _gh_api(f"repos/{repo}/commits/{sha}/check-runs?per_page=100")
    runs = payload.get("check_runs") if isinstance(payload, dict) else None
    if not isinstance(runs, list):
        raise RuntimeError("GitHub check-runs response did not include a check_runs list")
    run = _latest_named(runs, "name", check)
    if not run:
        return False, {"name": check, "kind": "check-run", "ok": False, "detail": "missing check-run"}
    status = run.get("status")
    conclusion = run.get("conclusion")
    trusted = _trusted_check_run(run)
    ok = status == "completed" and conclusion == "success" and trusted
    return ok, {
        "name": check,
        "kind": "check-run",
        "ok": ok,
        "detail": f"status={status} conclusion={conclusion} app={_check_run_app_slug(run) or 'missing'} trusted_app={trusted}",
        "run_id": run.get("id"),
        "url": run.get("html_url") or run.get("details_url"),
    }


def _status_state(repo: str, sha: str, check: str) -> Tuple[bool, Dict[str, Any]]:
    payload = _gh_api(f"repos/{repo}/commits/{sha}/statuses?per_page=100")
    statuses = payload if isinstance(payload, list) else payload.get("statuses") if isinstance(payload, dict) else None
    if not isinstance(statuses, list):
        raise RuntimeError("GitHub statuses response did not include a list")
    status = _latest_named(statuses, "context", check)
    if not status:
        return False, {"name": check, "kind": "commit-status", "ok": False, "detail": "missing commit status"}
    state = status.get("state")
    trusted = _trusted_status(status)
    ok = state == "success" and trusted
    return ok, {
        "name": check,
        "kind": "commit-status",
        "ok": ok,
        "detail": f"state={state} creator={_status_creator_login(status) or 'missing'} trusted_creator={trusted}",
        "url": status.get("target_url"),
    }


def _commit_exists(repo: str, sha: str) -> None:
    payload = _gh_api(f"repos/{repo}/commits/{sha}")
    if not isinstance(payload, dict) or not payload.get("sha"):
        raise RuntimeError(f"GitHub commit lookup for {sha!r} did not return a commit")


def _default_branch_for_repo(repo: str) -> str:
    payload = _gh_api(f"repos/{repo}")
    default_branch = str(payload.get("default_branch") or "").strip() if isinstance(payload, dict) else ""
    if not default_branch:
        raise RuntimeError(f"GitHub repository lookup for {repo!r} did not include default_branch")
    return default_branch


def _commit_on_default_branch(repo: str, sha: str) -> Tuple[str, str]:
    default_branch = _default_branch_for_repo(repo)
    payload = _gh_api(f"repos/{repo}/compare/{sha}...{default_branch}")
    status = str(payload.get("status") or "").strip() if isinstance(payload, dict) else ""
    if status not in {"ahead", "identical"}:
        raise RuntimeError(
            f"GitHub commit {sha!r} is not on default branch {default_branch!r} for {repo!r}; "
            f"compare status={status or 'missing'}"
        )
    return default_branch, status


def _pull_requests_for_commit(repo: str, sha: str) -> List[Dict[str, Any]]:
    payload = _gh_api(f"repos/{repo}/commits/{sha}/pulls?per_page=100")
    if not isinstance(payload, list):
        raise RuntimeError("GitHub commit pulls response did not include a list")
    return [pr for pr in payload if isinstance(pr, dict)]


def _open_pull_requests_for_commit(repo: str, sha: str) -> List[Dict[str, Any]]:
    return [
        pr for pr in _pull_requests_for_commit(repo, sha)
        if str(pr.get("state") or "").lower() == "open"
    ]


def _same_sha(left: str, right: str) -> bool:
    return left.strip().lower() == right.strip().lower()


def _check_missing(observation: Dict[str, Any]) -> bool:
    return str(observation.get("detail") or "").strip().lower().startswith("missing ")


def _merged_source_pr_heads(repo: str, merge_sha: str) -> List[Dict[str, Any]]:
    candidates: List[Dict[str, Any]] = []
    for pr_ref in _pull_requests_for_commit(repo, merge_sha):
        number = pr_ref.get("number")
        pr = pr_ref
        if number:
            detail = _gh_api(f"repos/{repo}/pulls/{number}")
            if isinstance(detail, dict):
                pr = detail
        if not isinstance(pr, dict):
            continue
        merged = pr.get("merged") is True or bool(pr.get("merged_at"))
        if not merged:
            continue
        pr_merge_sha = str(pr.get("merge_commit_sha") or "").strip()
        if pr_merge_sha and not _same_sha(pr_merge_sha, merge_sha):
            continue
        head = pr.get("head") if isinstance(pr.get("head"), dict) else {}
        head_sha = str(head.get("sha") or "").strip()
        head_repo = head.get("repo") if isinstance(head.get("repo"), dict) else {}
        head_repo_name = str(head_repo.get("full_name") or "").strip()
        if not head_sha or not head_repo_name or head_repo_name.lower() != repo.lower():
            continue
        candidates.append({
            "pr_number": number,
            "pr_url": pr.get("html_url"),
            "head_sha": head_sha,
            "head_repo": head_repo_name,
        })
    return candidates


def _with_pr_head_provenance(
    observation: Dict[str, Any],
    *,
    merge_sha: str,
    source: Dict[str, Any],
) -> Dict[str, Any]:
    enriched = dict(observation)
    enriched.update({
        "source": "merged-pr-head",
        "merge_commit_sha": merge_sha,
        "source_commit_sha": source.get("head_sha"),
        "source_repo": source.get("head_repo"),
        "source_pr": source.get("pr_number"),
        "source_pr_url": source.get("pr_url"),
    })
    return enriched


def _merged_pr_head_check_state(repo: str, merge_sha: str, check: str) -> Tuple[bool, Dict[str, Any]]:
    sources = _merged_source_pr_heads(repo, merge_sha)
    if not sources:
        return False, {
            "name": check,
            "kind": "merged-pr-head",
            "ok": False,
            "detail": "no merged same-repo source PR head found for cited merge commit",
        }
    failures: List[Dict[str, Any]] = []
    for source in sources:
        head_sha = str(source.get("head_sha") or "").strip()
        run_ok, run_observation = _check_run_state(repo, head_sha, check)
        run_observation = _with_pr_head_provenance(run_observation, merge_sha=merge_sha, source=source)
        if run_ok:
            return True, run_observation
        status_ok, status_observation = _status_state(repo, head_sha, check)
        status_observation = _with_pr_head_provenance(status_observation, merge_sha=merge_sha, source=source)
        if status_ok:
            return True, status_observation
        failures.extend([run_observation, status_observation])
    return False, {
        "name": check,
        "kind": "merged-pr-head",
        "ok": False,
        "detail": "merged same-repo source PR head did not satisfy required gate",
        "candidates": failures,
    }


def _can_use_merged_pr_head_provenance(
    check: str,
    run_observation: Dict[str, Any],
    status_observation: Dict[str, Any],
) -> bool:
    return (
        check in MERGED_PR_HEAD_PROVENANCE_CHECKS
        and _check_missing(run_observation)
        and _check_missing(status_observation)
    )


def _unverified(
    reason: str,
    *,
    commit_sha: str = "",
    repo: str = "",
    required_checks: Iterable[str] = DEFAULT_REQUIRED_GITHUB_CHECKS,
    producer: str = "",
    checks: Optional[List[Dict[str, Any]]] = None,
    applies: bool = True,
    reject_completion: bool = False,
) -> Dict[str, Any]:
    payload = {
        "status": UNVERIFIED,
        "verified": False,
        "applies": bool(applies),
        "source": "github-required-checks",
        "repo": repo,
        "commit_sha": commit_sha,
        "required_checks": list(required_checks),
        "producer": producer,
        "verifier": "github-required-checks",
        "reason": reason,
        "checks": checks or [],
    }
    if reject_completion:
        payload["reject_completion"] = True
    return payload


def _supervisor_verification_unverified(
    reason: str,
    *,
    producer: str = "",
    verifier: str = "",
    reject_completion: bool = True,
) -> Dict[str, Any]:
    payload = {
        "status": UNVERIFIED,
        "verified": False,
        "applies": True,
        "source": SUPERVISOR_VERIFICATION_SOURCE,
        "repo": "",
        "commit_sha": "",
        "required_checks": [],
        "producer": producer,
        "verifier": verifier or "none",
        "reason": reason,
        "checks": [],
    }
    if reject_completion:
        payload["reject_completion"] = True
    return payload


def _gated_no_commit_completion_unverified(no_commit_repo: str, *, producer: str = "") -> Dict[str, Any]:
    return _unverified(
        f"completion evidence for gated repo {no_commit_repo!r} must include commit_sha and repo; "
        "observation-only and supervisor_verification evidence cannot bypass open-PR and required-gate verification. "
        "Use commit_sha+repo evidence after the PR is merged and required gates pass. "
        "Supervisor verification remains valid only for research/prototype work with no gated runtime repo.",
        repo=no_commit_repo,
        required_checks=required_github_checks_for_repo(no_commit_repo),
        producer=producer,
        applies=True,
        reject_completion=True,
    )


def _verify_supervisor_completion_evidence(evidence: Dict[str, Any], *, producer: str = "") -> Optional[Dict[str, Any]]:
    if "supervisor_verification" not in evidence:
        return None
    raw = evidence.get("supervisor_verification")
    if not isinstance(raw, dict):
        return _supervisor_verification_unverified(
            "supervisor_verification evidence must be a JSON object with mode, verifier, and observation",
            producer=producer,
        )
    mode = str(raw.get("mode") or "").strip().lower()
    verifier = str(raw.get("verifier") or "").strip()
    observation = str(raw.get("observation") or "").strip()
    producer_value = str(producer or "").strip()
    if mode not in SUPERVISOR_VERIFICATION_MODES:
        return _supervisor_verification_unverified(
            "supervisor_verification.mode must be one of: prototype, research",
            producer=producer_value,
            verifier=verifier,
        )
    if not verifier:
        return _supervisor_verification_unverified(
            "supervisor_verification.verifier must name the distinct supervisor session that verified the work",
            producer=producer_value,
        )
    if not producer_value:
        return _supervisor_verification_unverified(
            "supervisor_verification requires a non-empty completion producer so verifier!=producer can be enforced",
            verifier=verifier,
        )
    if verifier.lower() == producer_value.lower():
        return _supervisor_verification_unverified(
            "supervisor_verification is self-attestation: verifier must differ from producer",
            producer=producer_value,
            verifier=verifier,
        )
    if len(observation) < 8:
        return _supervisor_verification_unverified(
            "supervisor_verification.observation must describe what the supervisor verified in at least 8 characters",
            producer=producer_value,
            verifier=verifier,
        )
    return {
        "status": VERIFIED,
        "verified": True,
        "applies": True,
        "source": SUPERVISOR_VERIFICATION_SOURCE,
        "repo": "",
        "commit_sha": "",
        "required_checks": [],
        "producer": producer_value,
        "verifier": verifier,
        "mode": mode,
        "reason": "distinct supervisor session verified this non-production research/prototype completion",
        "checks": [{
            "name": SUPERVISOR_VERIFICATION_SOURCE,
            "kind": "supervisor-attestation",
            "ok": True,
            "detail": f"{verifier} verified {mode} completion by {producer_value}: {observation}",
        }],
    }


def verify_completion_evidence(
    evidence: Optional[Dict[str, Any]],
    *,
    producer: str = "",
    trusted_task: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """Verify completion evidence.

    Audit-class completion is driven ONLY from trusted OrchTask metadata
    (``trusted_task`` loaded by the caller). Evidence may supply ``audit_receipt``
    but cannot select ``completion_class`` or overwrite pins/status IDs.
    """
    if not isinstance(evidence, dict) or not evidence:
        return None
    if "loop_proof" in evidence:
        verification = verify_loop_proof_receipt(evidence, producer=producer)
        verification["applies"] = True
        return verification

    from .audit_completion import (
        AuditContractError,
        assert_no_audit_override_in_evidence,
        is_audit_task,
        verify_audit_completion,
    )

    try:
        assert_no_audit_override_in_evidence(evidence)
    except AuditContractError as exc:
        return _unverified(
            str(exc),
            producer=producer,
            reject_completion=True,
        )

    if is_audit_task(trusted_task):
        return verify_audit_completion(trusted_task or {}, evidence, producer=producer)

    if str(evidence.get("audit_receipt") or "").strip():
        return _unverified(
            "audit_receipt is only valid when trusted OrchTask.completion_class=audit "
            "(loaded by verifier; evidence cannot self-select audit completion)",
            producer=producer,
            reject_completion=True,
        )

    commit_sha = str(evidence.get("commit_sha") or "").strip()
    repo = repo_from_completion_evidence(evidence)
    checks = required_github_checks_for_repo(repo) if repo else required_github_checks()
    if not commit_sha:
        gated, no_commit_repo = _no_commit_evidence_targets_gated_repo(evidence)
        if gated:
            return _gated_no_commit_completion_unverified(no_commit_repo, producer=producer)
        supervisor_verification = _verify_supervisor_completion_evidence(evidence, producer=producer)
        if supervisor_verification is not None:
            return supervisor_verification
        repo = _no_commit_completion_repo(evidence)
        return _unverified(
            "completion evidence has no commit_sha; gateless/local/non-repo completion remains a self-report",
            repo=repo,
            required_checks=checks,
            producer=producer,
            applies=False,
        )
    if not repo:
        return _unverified(
            "completion evidence with commit_sha must include completion_evidence.repo; "
            "the verifier will not infer a default repo because wrong-repo verification is worse than no verification",
            commit_sha=commit_sha,
            required_checks=checks,
            producer=producer,
            reject_completion=True,
        )
    try:
        if not _repo_allowed_for_completion_evidence(repo):
            return _unverified(
                _repo_not_allowed_reason(repo),
                commit_sha=commit_sha,
                repo=repo,
                required_checks=checks,
                producer=producer,
            )
        profile = _repo_profile_for_completion_evidence(repo)
        if profile not in SUPPORTED_REPO_PROFILES:
            return _unverified(
                f"completion evidence repo {repo!r} has unsupported verification profile {profile!r}; "
                f"supported profiles are {', '.join(sorted(SUPPORTED_REPO_PROFILES))}",
                commit_sha=commit_sha,
                repo=repo,
                required_checks=checks,
                producer=producer,
                reject_completion=True,
            )
        _commit_exists(repo, commit_sha)
        if profile == GATELESS_REPO_PROFILE:
            default_branch, compare_status = _commit_on_default_branch(repo, commit_sha)
            return {
                "status": VERIFIED,
                "verified": True,
                "applies": True,
                "source": "github-default-branch",
                "repo": repo,
                "commit_sha": commit_sha,
                "required_checks": [],
                "producer": producer,
                "verifier": "github-default-branch",
                "profile": GATELESS_REPO_PROFILE,
                "default_branch": default_branch,
                "compare_status": compare_status,
                "reason": "GitHub commit exists in the allowed gateless repo and is reachable from its default branch",
                "checks": [{
                    "name": "default-branch-containment",
                    "kind": "github-compare",
                    "ok": True,
                    "detail": f"commit is on default branch {default_branch} (compare status={compare_status})",
                }],
            }
        open_prs = _open_pull_requests_for_commit(repo, commit_sha)
        if open_prs:
            refs = ", ".join(
                f"#{pr.get('number')}" if pr.get("number") else str(pr.get("html_url") or "open-pr")
                for pr in open_prs[:5]
            )
            return _unverified(
                f"completion evidence commit_sha is still attached to open pull request(s): {refs}; "
                "merge the PR or cite a commit that is no longer on an open PR before completing the task",
                commit_sha=commit_sha,
                repo=repo,
                required_checks=checks,
                producer=producer,
                reject_completion=True,
            )
        observations: List[Dict[str, Any]] = []
        failures: List[str] = []
        merged_pr_head_provenance_used = False
        for check in checks:
            pr_head_observation: Optional[Dict[str, Any]] = None
            run_ok, run_observation = _check_run_state(repo, commit_sha, check)
            if run_ok:
                observations.append(run_observation)
                continue
            status_ok, status_observation = _status_state(repo, commit_sha, check)
            if status_ok:
                observations.append(status_observation)
                continue
            if _can_use_merged_pr_head_provenance(check, run_observation, status_observation):
                pr_head_ok, pr_head_observation = _merged_pr_head_check_state(repo, commit_sha, check)
                if pr_head_ok:
                    observations.append(pr_head_observation)
                    merged_pr_head_provenance_used = True
                    continue
                observations.append(pr_head_observation)
            observations.extend([run_observation, status_observation])
            fallback_detail = ""
            if pr_head_observation is not None:
                fallback_detail = f"; merged-pr-head: {pr_head_observation.get('detail')}"
            failures.append(
                f"{check}: {run_observation.get('detail')}; {status_observation.get('detail')}{fallback_detail}"
            )
        if failures:
            return _unverified(
                "required GitHub gates did not pass: " + "; ".join(failures),
                commit_sha=commit_sha,
                repo=repo,
                required_checks=checks,
                producer=producer,
                checks=observations,
            )
        return {
            "status": VERIFIED,
            "verified": True,
            "applies": True,
            "source": "github-required-checks",
            "repo": repo,
            "commit_sha": commit_sha,
            "required_checks": list(checks),
            "producer": producer,
            "verifier": "github-required-checks",
            "reason": (
                "GitHub commit exists and all required gate contexts passed for this exact commit_sha"
                if not merged_pr_head_provenance_used
                else "GitHub merge commit exists, non-R5 gates passed for this exact commit_sha, and missing R5 provenance was satisfied by the merged same-repo PR head"
            ),
            "checks": observations,
        }
    except RuntimeError as exc:
        return _unverified(
            str(exc),
            commit_sha=commit_sha,
            repo=repo,
            required_checks=checks,
            producer=producer,
        )
