from __future__ import annotations

import json
import logging
import os
import subprocess
from typing import Any, Dict, Iterable, List, Optional, Tuple


DEFAULT_REQUIRED_GITHUB_CHECKS = ("r5-audit-gate", "ship-gate-acceptance")
DEFAULT_TRUSTED_CHECK_RUN_APPS = ("github-actions",)
DEFAULT_TRUSTED_STATUS_CREATORS = ("github-actions[bot]",)
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


def _csv_env_values(name: str, defaults: Iterable[str]) -> Tuple[str, ...]:
    raw = os.environ.get(name, "")
    values = [part.strip() for part in raw.split(",") if part.strip()] if raw else list(defaults)
    return tuple(dict.fromkeys(values))


def allowed_completion_repos() -> Tuple[str, ...]:
    raw = os.environ.get("ORCH_COMPLETION_ALLOWED_REPOS", "")
    values = [part.strip() for part in raw.split(",") if part.strip()]
    return tuple(dict.fromkeys(values))


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


def _repo_allowed_for_completion_evidence(repo: str) -> bool:
    return repo.strip().lower() in {allowed.lower() for allowed in allowed_completion_repos()}


def _repo_not_allowed_reason(repo: str, *, inferred: bool = False) -> str:
    source = "inferred completion evidence repo" if inferred else "completion evidence repo"
    if allowed_completion_repos():
        return (
            f"{source} {repo!r} is not in the ORCH_COMPLETION_ALLOWED_REPOS allowlist; "
            "arbitrary forks cannot satisfy VERIFIED provenance"
        )
    return (
        f"{source} {repo!r} cannot satisfy VERIFIED provenance because ORCH_COMPLETION_ALLOWED_REPOS is unset; "
        "set ORCH_COMPLETION_ALLOWED_REPOS=OWNER/REPO[,OWNER/REPO...] to enable verified completions"
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


def _unverified(
    reason: str,
    *,
    commit_sha: str = "",
    repo: str = "",
    required_checks: Iterable[str] = DEFAULT_REQUIRED_GITHUB_CHECKS,
    producer: str = "",
    checks: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    return {
        "status": UNVERIFIED,
        "verified": False,
        "source": "github-required-checks",
        "repo": repo,
        "commit_sha": commit_sha,
        "required_checks": list(required_checks),
        "producer": producer,
        "verifier": "github-required-checks",
        "reason": reason,
        "checks": checks or [],
    }


def verify_completion_evidence(
    evidence: Optional[Dict[str, Any]],
    *,
    producer: str = "",
) -> Optional[Dict[str, Any]]:
    if not isinstance(evidence, dict) or not evidence:
        return None
    checks = required_github_checks()
    commit_sha = str(evidence.get("commit_sha") or "").strip()
    if not commit_sha:
        repo = repo_from_completion_evidence(evidence)
        return _unverified(
            "completion evidence has no commit_sha; local/non-repo completion remains a self-report",
            repo=repo,
            required_checks=checks,
            producer=producer,
        )
    repo = repo_from_completion_evidence(evidence)
    try:
        if repo:
            if not _repo_allowed_for_completion_evidence(repo):
                return _unverified(
                    _repo_not_allowed_reason(repo),
                    commit_sha=commit_sha,
                    repo=repo,
                    required_checks=checks,
                    producer=producer,
                )
        else:
            repo = github_repo_from_environment_or_gh()
            if not _repo_allowed_for_completion_evidence(repo):
                return _unverified(
                    _repo_not_allowed_reason(repo, inferred=True),
                    commit_sha=commit_sha,
                    repo=repo,
                    required_checks=checks,
                    producer=producer,
                )
        _commit_exists(repo, commit_sha)
        observations: List[Dict[str, Any]] = []
        failures: List[str] = []
        for check in checks:
            run_ok, run_observation = _check_run_state(repo, commit_sha, check)
            if run_ok:
                observations.append(run_observation)
                continue
            status_ok, status_observation = _status_state(repo, commit_sha, check)
            if status_ok:
                observations.append(status_observation)
                continue
            observations.extend([run_observation, status_observation])
            failures.append(f"{check}: {run_observation.get('detail')}; {status_observation.get('detail')}")
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
            "source": "github-required-checks",
            "repo": repo,
            "commit_sha": commit_sha,
            "required_checks": list(checks),
            "producer": producer,
            "verifier": "github-required-checks",
            "reason": "GitHub commit exists and all required gate contexts passed for this exact commit_sha",
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
