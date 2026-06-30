from __future__ import annotations

import json
import logging
import re
import subprocess
from typing import Any, Dict, List, Optional, Tuple

from .config import OrchConfig, get_neo4j_driver
from .evidence_verification import VERIFIED, github_repo_from_environment_or_gh, verify_completion_evidence
from .notify_state import redis_connect as notify_redis_connect
from .notify_state import state_key


LOG = logging.getLogger(__name__)
_GH_TIMEOUT_SEC = 10
_TERMINAL_OUTCOME_TO_STATUS = {
    "error": "failed",
    "interrupted": "interrupted",
}
_FOLLOW_UP_RE = re.compile(
    r"\b(?:r5|audit|gatekeeper|grok|review|follow[- ]?up|gate)\b",
    re.IGNORECASE,
)
_PR_NUMBER_RE = re.compile(r"\b(?:PR|pull request)\s*#\s*(\d+)\b", re.IGNORECASE)
_PR_URL_RE = re.compile(
    r"https?://github\.com/([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/pull/(\d+)",
    re.IGNORECASE,
)


def _json_dict(raw: Any) -> Optional[Dict[str, Any]]:
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def _current_task_payload(r: Any, worker: str) -> Optional[Dict[str, Any]]:
    return _json_dict(r.get(state_key(worker, "current_task")))


def _last_outcome_payload(r: Any, worker: str) -> Optional[Dict[str, Any]]:
    return _json_dict(r.get(state_key(worker, "last_outcome")))


def _outcome_matches_task(outcome: Dict[str, Any], task_id: str) -> bool:
    outcome_task = str(outcome.get("task_id") or "").strip()
    if outcome_task:
        return outcome_task == task_id
    details = str(outcome.get("details") or "")
    return task_id in details


def _float_or_none(value: Any) -> Optional[float]:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fresh_in_progress_worker_liveness(task: Dict[str, Any], worker: str, current: Dict[str, Any]) -> bool:
    if str(task.get("status") or "").strip() != "in_progress":
        return False
    if str(task.get("worker_liveness_worker") or "").strip() != worker:
        return False
    current_started = _float_or_none(current.get("started_at"))
    liveness_started = _float_or_none(task.get("worker_liveness_started_at"))
    liveness_heartbeat = _float_or_none(task.get("worker_liveness_heartbeat_at"))
    if current_started is None:
        return liveness_started is not None or liveness_heartbeat is not None
    if liveness_started is not None and liveness_started >= current_started:
        return True
    if liveness_heartbeat is not None and liveness_heartbeat >= current_started:
        return True
    return False


def _matching_worker_terminal_outcome(r: Any, worker: str, task: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    task_id = str(task.get("task_id") or "").strip()
    if not task_id:
        return None
    current = _current_task_payload(r, worker)
    if not current or str(current.get("task_id") or "").strip() != task_id:
        return None
    outcome = _last_outcome_payload(r, worker)
    if not outcome:
        return None
    outcome_name = str(outcome.get("outcome") or "").strip().lower()
    if outcome_name not in _TERMINAL_OUTCOME_TO_STATUS:
        return None
    if not _outcome_matches_task(outcome, task_id):
        return None
    if _fresh_in_progress_worker_liveness(task, worker, current):
        LOG.info(
            "stale-task reconciliation skipped active retry task=%s worker=%s outcome=%s",
            task_id,
            worker,
            outcome_name,
        )
        return None
    return outcome


def _clear_matching_worker_current_task(r: Any, worker: str, task_id: str) -> bool:
    key = state_key(worker, "current_task")
    current = _json_dict(r.get(key))
    if not current or str(current.get("task_id") or "").strip() != task_id:
        return False
    r.delete(key)
    return True


def _non_terminal_reconciliation_candidates(
    *,
    config: Optional[OrchConfig] = None,
    task_id_prefix: Optional[str] = None,
    project_id_prefix: Optional[str] = None,
) -> List[Dict[str, Any]]:
    cfg = config or OrchConfig()
    task_prefix = str(task_id_prefix or "")
    project_prefix = str(project_id_prefix or "")
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        return [dict(record) for record in session.run(
            """
            MATCH (p:OrchProject)-[:HAS_PHASE]->(:OrchPhase)-[:HAS_TASK]->(t:OrchTask)
            WHERE coalesce(t.status, 'pending') IN ['pending', 'in_progress']
              AND NOT toUpper(trim(coalesce(t.blocked_on, ''))) STARTS WITH 'AWAIT:'
              AND ($task_prefix = '' OR t.id STARTS WITH $task_prefix)
              AND ($project_prefix = '' OR p.id STARTS WITH $project_prefix)
            RETURN t.id AS task_id,
                   t.description AS description,
                   coalesce(t.status, 'pending') AS status,
                   t.blocked_on AS blocked_on,
                   t.owner AS owner,
                   t.dispatched_to AS dispatched_to,
                   t.worker_liveness_worker AS worker_liveness_worker,
                   t.worker_liveness_started_at AS worker_liveness_started_at,
                   t.worker_liveness_heartbeat_at AS worker_liveness_heartbeat_at,
                   t.task_type AS task_type,
                   p.id AS project_id,
                   p.supervisor AS project_supervisor
            """,
            task_prefix=task_prefix,
            project_prefix=project_prefix,
        )]


def _worker_candidates(task: Dict[str, Any]) -> List[str]:
    values = [
        task.get("dispatched_to"),
        task.get("worker_liveness_worker"),
        task.get("owner"),
    ]
    workers: List[str] = []
    for value in values:
        worker = str(value or "").strip()
        if worker and worker not in workers:
            workers.append(worker)
    return workers


def reconcile_terminal_outcome_tasks(
    *,
    config: Optional[OrchConfig] = None,
    task_id_prefix: Optional[str] = None,
    project_id_prefix: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Terminalize tasks whose worker already recorded error/interrupted.

    ``record_outcome(error|interrupted)`` intentionally leaves the worker's
    current_task in Redis so a supervisor can inspect the failed attempt. If no
    supervisor later acts, the Neo4j task can remain pending/in_progress
    forever. This reaper reconciles only when Redis still proves the same worker
    current_task and last_outcome point at the same task.
    """
    from .orch_schema import update_task_status

    cfg = config or OrchConfig()
    r = notify_redis_connect()
    reconciled: List[Dict[str, Any]] = []
    for task in _non_terminal_reconciliation_candidates(
        config=cfg,
        task_id_prefix=task_id_prefix,
        project_id_prefix=project_id_prefix,
    ):
        if str(task.get("task_type") or "") == "human-review":
            continue
        task_id = str(task.get("task_id") or "").strip()
        if not task_id:
            continue
        for worker in _worker_candidates(task):
            outcome = _matching_worker_terminal_outcome(r, worker, task)
            if not outcome:
                continue
            outcome_name = str(outcome.get("outcome") or "").strip().lower()
            status = _TERMINAL_OUTCOME_TO_STATUS[outcome_name]
            details = str(outcome.get("details") or "").strip()
            reason = (
                f"stale-task reconciliation: worker {worker} recorded outcome={outcome_name} "
                f"for {task_id}; no supervisor terminalized it"
            )
            if details:
                reason = f"{reason}; details={details[:180]}"
            if update_task_status(
                task_id,
                status,
                result=reason,
                completion_evidence={"reason": reason},
                config=cfg,
            ):
                cleared = _clear_matching_worker_current_task(r, worker, task_id)
                item = dict(task)
                item.update({
                    "task_id": task_id,
                    "worker": worker,
                    "status": status,
                    "outcome": outcome_name,
                    "reason": reason,
                    "cleared_worker_current_task": cleared,
                    "supervisor": task.get("project_supervisor") or task.get("owner") or "",
                    "reconciliation_kind": "terminal_outcome",
                })
                reconciled.append(item)
            break
    return reconciled


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


def _default_github_repo() -> Optional[str]:
    try:
        return github_repo_from_environment_or_gh()
    except RuntimeError as exc:
        LOG.warning("task reconciliation cannot infer GitHub repo for PR auto-close: %s", exc)
        return None


def _pr_references(text: str, *, default_repo: Optional[str]) -> List[Tuple[str, int]]:
    refs: List[Tuple[str, int]] = []
    seen: set[Tuple[str, int]] = set()
    for match in _PR_URL_RE.finditer(text):
        repo = match.group(1)
        number = int(match.group(2))
        key = (repo, number)
        if key not in seen:
            refs.append(key)
            seen.add(key)
    if default_repo:
        for match in _PR_NUMBER_RE.finditer(text):
            key = (default_repo, int(match.group(1)))
            if key not in seen:
                refs.append(key)
                seen.add(key)
    return refs


def _looks_like_pr_followup_task(task: Dict[str, Any]) -> bool:
    text = " ".join(
        str(task.get(key) or "")
        for key in ("task_id", "description")
    )
    return bool(_FOLLOW_UP_RE.search(text))


def _merged_pr_head_sha(repo: str, number: int) -> Optional[str]:
    payload = _gh_api(f"repos/{repo}/pulls/{number}")
    if not isinstance(payload, dict):
        raise RuntimeError(f"GitHub PR lookup for {repo}#{number} did not return an object")
    merged = payload.get("merged") is True or bool(payload.get("merged_at"))
    if not merged:
        return None
    head = payload.get("head")
    head_sha = str(head.get("sha") or "").strip() if isinstance(head, dict) else ""
    merge_sha = str(payload.get("merge_commit_sha") or "").strip()
    return head_sha or merge_sha or None


def _verified_pr_evidence(repo: str, number: int, sha: str) -> Optional[Dict[str, str]]:
    evidence = {
        "commit_sha": sha,
        "repo": repo,
        "production_observation": (
            f"stale-task reconciliation observed merged PR #{number} with required gates passed"
        ),
    }
    verification = verify_completion_evidence(evidence, producer="stale-task-reconciliation")
    if isinstance(verification, dict) and verification.get("status") == VERIFIED and verification.get("verified") is True:
        return evidence
    LOG.info(
        "PR follow-up task not auto-closed: repo=%s pr=%s sha=%s verification=%s",
        repo,
        number,
        sha,
        verification,
    )
    return None


def reconcile_merged_pr_followup_tasks(
    *,
    config: Optional[OrchConfig] = None,
    task_id_prefix: Optional[str] = None,
    project_id_prefix: Optional[str] = None,
    default_repo: Optional[str] = None,
) -> List[Dict[str, Any]]:
    from .orch_schema import update_task_status

    cfg = config or OrchConfig()
    repo = str(default_repo or "").strip() or None
    repo_resolved = repo is not None
    reconciled: List[Dict[str, Any]] = []
    for task in _non_terminal_reconciliation_candidates(
        config=cfg,
        task_id_prefix=task_id_prefix,
        project_id_prefix=project_id_prefix,
    ):
        if str(task.get("task_type") or "") == "human-review":
            continue
        task_id = str(task.get("task_id") or "").strip()
        description = str(task.get("description") or "")
        text = f"{task_id} {description}"
        if not _looks_like_pr_followup_task(task):
            continue
        refs = _pr_references(text, default_repo=repo)
        if not refs and _PR_NUMBER_RE.search(text):
            if not repo_resolved:
                repo = _default_github_repo()
                repo_resolved = True
            refs = _pr_references(text, default_repo=repo)
        for pr_repo, pr_number in refs:
            try:
                sha = _merged_pr_head_sha(pr_repo, pr_number)
            except RuntimeError as exc:
                LOG.warning("PR follow-up reconciliation lookup failed task=%s repo=%s pr=%s: %s",
                            task_id, pr_repo, pr_number, exc)
                continue
            if not sha:
                continue
            evidence = _verified_pr_evidence(pr_repo, pr_number, sha)
            if not evidence:
                continue
            result = (
                f"stale-task reconciliation: auto-closed PR follow-up after merged PR "
                f"{pr_repo}#{pr_number} passed required gates at {sha}"
            )
            if update_task_status(
                task_id,
                "completed",
                result=result,
                completion_evidence=evidence,
                completed_by="stale-task-reconciliation",
                config=cfg,
            ):
                item = dict(task)
                item.update({
                    "task_id": task_id,
                    "worker": task.get("dispatched_to") or task.get("owner") or "",
                    "status": "completed",
                    "repo": pr_repo,
                    "pr_number": pr_number,
                    "commit_sha": sha,
                    "reason": result,
                    "supervisor": task.get("project_supervisor") or task.get("owner") or "",
                    "reconciliation_kind": "merged_pr_followup",
                })
                reconciled.append(item)
            break
    return reconciled


def reconcile_stale_tasks(
    *,
    config: Optional[OrchConfig] = None,
    task_id_prefix: Optional[str] = None,
    project_id_prefix: Optional[str] = None,
) -> Dict[str, Any]:
    terminal_outcomes = reconcile_terminal_outcome_tasks(
        config=config,
        task_id_prefix=task_id_prefix,
        project_id_prefix=project_id_prefix,
    )
    merged_pr_followups = reconcile_merged_pr_followup_tasks(
        config=config,
        task_id_prefix=task_id_prefix,
        project_id_prefix=project_id_prefix,
    )
    reconciled = [*terminal_outcomes, *merged_pr_followups]
    return {
        "terminal_outcomes": terminal_outcomes,
        "merged_pr_followups": merged_pr_followups,
        "reconciled": reconciled,
        "count": len(reconciled),
    }
