"""Authorize and perform GitHub outward mutations under live task binding.

P0 safety (task-f396305d): unbind/revocation must invalidate the capability to
post commit statuses or issue/PR comments even if the worker process is still
running. Process halt is cleanup, not the authorization gate — the gate is
checked at the mutation side-effect boundary.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from redis import RedisError

from .config import OrchConfig
from .current_task_binding import (
    decode_current_task,
    is_live_binding_status,
    task_status,
)
from .notify_state import redis_connect, state_key

LOG = logging.getLogger(__name__)

TaskLoader = Callable[[str], Optional[Dict[str, Any]]]
GhApi = Callable[[list[str]], Any]


class OutwardAuthorizationError(RuntimeError):
    """Raised when an outward GitHub mutation is denied."""


@dataclass(frozen=True)
class OutwardAuthDecision:
    allowed: bool
    reason: str
    session_id: str = ""
    task_id: str = ""
    supervisor: str = ""
    executor: str = ""
    task_status: str = ""


def _default_task_loader(task_id: str, *, config: Optional[OrchConfig] = None) -> Optional[Dict[str, Any]]:
    from .orch_schema import get_task

    return get_task(task_id, config=config)


def _executor_identity(task: Dict[str, Any]) -> str:
    dispatched = str(task.get("dispatched_to") or "").strip()
    if dispatched:
        return dispatched
    return str(task.get("owner") or "").strip()


def authorize_outward_github_mutation(
    session_id: str,
    *,
    redis_client: Any = None,
    task_loader: Optional[TaskLoader] = None,
    config: Optional[OrchConfig] = None,
) -> OutwardAuthDecision:
    """Fail-closed live authorization for GitHub status/comment mutations.

    Requires:
    - Redis ``current_task`` binding for ``session_id``
    - Bound task_id + supervisor present
    - OrchTask exists with live status (in_progress/dispatched)
    - Executor identity (dispatched_to or owner) equals ``session_id``
    - Binding supervisor is non-empty (dispatcher/supervisor authority wire)
    """
    session = str(session_id or "").strip()
    if not session:
        return OutwardAuthDecision(False, "session_id is required for outward GitHub mutation")

    try:
        r = redis_client or redis_connect()
        raw = r.get(state_key(session, "current_task"))
    except RedisError as exc:
        return OutwardAuthDecision(
            False,
            f"redis current_task read failed (fail-closed): {exc}",
            session_id=session,
        )

    current = decode_current_task(raw)
    if not current:
        return OutwardAuthDecision(
            False,
            f"no live current_task binding for session {session}; mutation denied after unbind/revocation",
            session_id=session,
        )

    task_id = str(current.get("task_id") or "").strip()
    supervisor = str(current.get("supervisor") or "").strip()
    if not task_id:
        return OutwardAuthDecision(
            False,
            f"current_task for {session} lacks task_id; mutation denied",
            session_id=session,
        )
    if not supervisor:
        return OutwardAuthDecision(
            False,
            f"current_task for {session} lacks supervisor; mutation denied",
            session_id=session,
            task_id=task_id,
        )

    loader = task_loader or (lambda tid: _default_task_loader(tid, config=config))
    try:
        task = loader(task_id)
    except Exception as exc:  # noqa: BLE001 - fail closed on graph errors
        return OutwardAuthDecision(
            False,
            f"task load failed for {task_id} (fail-closed): {exc}",
            session_id=session,
            task_id=task_id,
            supervisor=supervisor,
        )
    if not task:
        return OutwardAuthDecision(
            False,
            f"OrchTask {task_id} not found; mutation denied",
            session_id=session,
            task_id=task_id,
            supervisor=supervisor,
        )

    status = str(task.get("status") or task_status(task_id, config=config) or "").strip().lower()
    if not is_live_binding_status(status):
        return OutwardAuthDecision(
            False,
            f"task {task_id} status={status or '<empty>'} is not live; mutation denied",
            session_id=session,
            task_id=task_id,
            supervisor=supervisor,
            task_status=status,
        )

    executor = _executor_identity(task)
    if not executor:
        return OutwardAuthDecision(
            False,
            f"task {task_id} has no executor identity (dispatched_to/owner); mutation denied",
            session_id=session,
            task_id=task_id,
            supervisor=supervisor,
            task_status=status,
        )
    if executor != session:
        return OutwardAuthDecision(
            False,
            f"session {session} is not the live executor for {task_id} (executor={executor}); mutation denied",
            session_id=session,
            task_id=task_id,
            supervisor=supervisor,
            executor=executor,
            task_status=status,
        )

    return OutwardAuthDecision(
        True,
        "authorized",
        session_id=session,
        task_id=task_id,
        supervisor=supervisor,
        executor=executor,
        task_status=status,
    )


def require_outward_github_mutation(session_id: str, **kwargs: Any) -> OutwardAuthDecision:
    decision = authorize_outward_github_mutation(session_id, **kwargs)
    if not decision.allowed:
        raise OutwardAuthorizationError(decision.reason)
    return decision


def _default_gh_api(args: list[str]) -> Any:
    result = subprocess.run(
        ["gh", "api", *args],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"gh api failed: {detail}")
    try:
        return json.loads(result.stdout or "null")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"gh api returned invalid JSON: {exc}") from exc


def resolve_session_id(explicit: Optional[str] = None) -> str:
    """Resolve worker session for mutation auth (explicit > env > tmux)."""
    for candidate in (
        explicit,
        os.environ.get("ORCH_OUTWARD_SESSION"),
        os.environ.get("TAEY_SESSION"),
        os.environ.get("TMUX_SESSION"),
    ):
        value = str(candidate or "").strip()
        if value:
            return value
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "#S"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            value = (result.stdout or "").strip()
            if value:
                return value
    except OSError:
        pass
    return ""


def post_commit_status(
    *,
    session_id: str,
    repo: str,
    sha: str,
    state: str,
    context: str,
    description: str,
    target_url: Optional[str] = None,
    redis_client: Any = None,
    task_loader: Optional[TaskLoader] = None,
    config: Optional[OrchConfig] = None,
    gh_api: Optional[GhApi] = None,
) -> Dict[str, Any]:
    """Post a commit status only when live outward authorization allows it."""
    require_outward_github_mutation(
        session_id,
        redis_client=redis_client,
        task_loader=task_loader,
        config=config,
    )
    api = gh_api or _default_gh_api
    args = [
        "-X",
        "POST",
        f"repos/{repo}/statuses/{sha}",
        "-f",
        f"state={state}",
        "-f",
        f"context={context}",
        "-f",
        f"description={description}",
    ]
    if target_url:
        args.extend(["-f", f"target_url={target_url}"])
    payload = api(args)
    if not isinstance(payload, dict):
        raise RuntimeError("status POST returned non-object JSON")
    LOG.info(
        "outward github status authorized session=%s repo=%s sha=%s context=%s",
        session_id,
        repo,
        sha,
        context,
    )
    return payload


def post_issue_comment(
    *,
    session_id: str,
    repo: str,
    issue_number: int,
    body: str,
    redis_client: Any = None,
    task_loader: Optional[TaskLoader] = None,
    config: Optional[OrchConfig] = None,
    gh_api: Optional[GhApi] = None,
) -> Dict[str, Any]:
    """Post an issue/PR comment only when live outward authorization allows it."""
    require_outward_github_mutation(
        session_id,
        redis_client=redis_client,
        task_loader=task_loader,
        config=config,
    )
    api = gh_api or _default_gh_api
    payload = api(
        [
            "-X",
            "POST",
            f"repos/{repo}/issues/{int(issue_number)}/comments",
            "-f",
            f"body={body}",
        ]
    )
    if not isinstance(payload, dict):
        raise RuntimeError("comment POST returned non-object JSON")
    LOG.info(
        "outward github comment authorized session=%s repo=%s issue=%s id=%s",
        session_id,
        repo,
        issue_number,
        payload.get("id"),
    )
    return payload
