"""Shared outward-capability authorization at mutation side-effect boundaries.

P0 safety (task-f396305d): unbind/revocation must invalidate the capability for
*all* worker outward mutations — GitHub commit statuses, GitHub issue/PR comments,
and taey-notify inbox enqueues — even if the worker process is still running.

Process halt is cleanup, not the authorization gate. One function,
``authorize_outward_capability``, is the shared boundary; status/comment/notify
helpers all require it before touching their sinks.
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
NotifySink = Callable[[str, str, str, str], Any]

# Worker-originated notify types that must not fire after unbind/revocation.
WORKER_OUTWARD_NOTIFY_TYPES = frozenset(
    {"response_ready", "result", "defect", "status"}
)


class OutwardAuthorizationError(RuntimeError):
    """Raised when an outward mutation is denied."""


@dataclass(frozen=True)
class OutwardAuthDecision:
    allowed: bool
    reason: str
    session_id: str = ""
    task_id: str = ""
    supervisor: str = ""
    executor: str = ""
    task_status: str = ""
    channel: str = ""


def _default_task_loader(task_id: str, *, config: Optional[OrchConfig] = None) -> Optional[Dict[str, Any]]:
    from .orch_schema import get_task

    return get_task(task_id, config=config)


def _executor_identity(task: Dict[str, Any]) -> str:
    dispatched = str(task.get("dispatched_to") or "").strip()
    if dispatched:
        return dispatched
    return str(task.get("owner") or "").strip()


def authorize_outward_capability(
    session_id: str,
    *,
    channel: str = "",
    redis_client: Any = None,
    task_loader: Optional[TaskLoader] = None,
    config: Optional[OrchConfig] = None,
) -> OutwardAuthDecision:
    """Fail-closed live authorization for worker outward mutations.

    Requires:
    - Redis ``current_task`` binding for ``session_id``
    - Bound task_id + supervisor present
    - OrchTask exists with live status (in_progress/dispatched)
    - Executor identity (dispatched_to or owner) equals ``session_id``
    """
    session = str(session_id or "").strip()
    ch = str(channel or "").strip()
    if not session:
        return OutwardAuthDecision(
            False,
            "session_id is required for outward mutation",
            channel=ch,
        )

    try:
        r = redis_client or redis_connect()
        raw = r.get(state_key(session, "current_task"))
    except RedisError as exc:
        return OutwardAuthDecision(
            False,
            f"redis current_task read failed (fail-closed): {exc}",
            session_id=session,
            channel=ch,
        )

    current = decode_current_task(raw)
    if not current:
        return OutwardAuthDecision(
            False,
            f"no live current_task binding for session {session}; "
            "mutation denied after unbind/revocation",
            session_id=session,
            channel=ch,
        )

    task_id = str(current.get("task_id") or "").strip()
    supervisor = str(current.get("supervisor") or "").strip()
    if not task_id:
        return OutwardAuthDecision(
            False,
            f"current_task for {session} lacks task_id; mutation denied",
            session_id=session,
            channel=ch,
        )
    if not supervisor:
        return OutwardAuthDecision(
            False,
            f"current_task for {session} lacks supervisor; mutation denied",
            session_id=session,
            task_id=task_id,
            channel=ch,
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
            channel=ch,
        )
    if not task:
        return OutwardAuthDecision(
            False,
            f"OrchTask {task_id} not found; mutation denied",
            session_id=session,
            task_id=task_id,
            supervisor=supervisor,
            channel=ch,
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
            channel=ch,
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
            channel=ch,
        )
    if executor != session:
        return OutwardAuthDecision(
            False,
            f"session {session} is not the live executor for {task_id} "
            f"(executor={executor}); mutation denied",
            session_id=session,
            task_id=task_id,
            supervisor=supervisor,
            executor=executor,
            task_status=status,
            channel=ch,
        )

    return OutwardAuthDecision(
        True,
        "authorized",
        session_id=session,
        task_id=task_id,
        supervisor=supervisor,
        executor=executor,
        task_status=status,
        channel=ch,
    )


def require_outward_capability(session_id: str, **kwargs: Any) -> OutwardAuthDecision:
    decision = authorize_outward_capability(session_id, **kwargs)
    if not decision.allowed:
        raise OutwardAuthorizationError(decision.reason)
    return decision


# Back-compat aliases used by earlier PR revision / call sites.
authorize_outward_github_mutation = authorize_outward_capability
require_outward_github_mutation = require_outward_capability


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


_GH_WRITE_METHODS = frozenset({"POST", "PATCH", "PUT", "DELETE"})


def _gh_api_method_and_path(api_args: list[str]) -> tuple[str, str]:
    method = "GET"
    path = ""
    idx = 0
    while idx < len(api_args):
        token = str(api_args[idx])
        if token in {"-X", "--method"} and idx + 1 < len(api_args):
            method = str(api_args[idx + 1]).upper()
            idx += 2
            continue
        if token.startswith("-X") and len(token) > 2:
            method = token[2:].upper()
            idx += 1
            continue
        if token.startswith("--method="):
            method = token.split("=", 1)[1].upper()
            idx += 1
            continue
        if not token.startswith("-") and not path:
            path = token
        idx += 1
    return method, path


def github_argv_requires_outward_capability(argv: list[str]) -> bool:
    """True for GitHub status/comment writes — the incident mutation surface.

    Direct ``gh api -X POST .../statuses|comments`` and ``gh pr/issue comment``
    must hit the same live-binding gate as the helper CLIs. Reads and other
    gh subcommands pass through.
    """
    if not argv:
        return False
    cmd = str(argv[0] or "").strip()
    rest = [str(item) for item in argv[1:]]
    if cmd == "api":
        method, path = _gh_api_method_and_path(rest)
        if method not in _GH_WRITE_METHODS:
            return False
        lowered = path.lower()
        return "/statuses" in lowered or "/comments" in lowered
    if cmd == "pr" and rest and rest[0] == "comment":
        return True
    if cmd == "issue" and rest and rest[0] == "comment":
        return True
    return False


def require_github_argv_capability(
    argv: list[str],
    *,
    session_id: str = "",
    **kwargs: Any,
) -> Optional[OutwardAuthDecision]:
    """Authorize mutating GitHub status/comment argv, or return None if a no-op."""
    if not github_argv_requires_outward_capability(argv):
        return None
    session = resolve_session_id(session_id)
    return require_outward_capability(session, channel="github_cli", **kwargs)


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
    """Post a commit status only when shared outward authorization allows it."""
    require_outward_capability(
        session_id,
        channel="github_status",
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
    """Post an issue/PR comment only when shared outward authorization allows it."""
    require_outward_capability(
        session_id,
        channel="github_comment",
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


def _default_notify_sink(target: str, body: str, msg_type: str, from_node: str) -> Dict[str, Any]:
    result = subprocess.run(
        [
            "taey-notify",
            target,
            body,
            "--type",
            msg_type,
            "--from",
            from_node,
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        raise RuntimeError(f"taey-notify failed: {detail}")
    return {"ok": True, "stdout": (result.stdout or "").strip()}


def send_outward_notify(
    *,
    session_id: str,
    target: str,
    body: str,
    msg_type: str = "response_ready",
    redis_client: Any = None,
    task_loader: Optional[TaskLoader] = None,
    config: Optional[OrchConfig] = None,
    notify_sink: Optional[NotifySink] = None,
) -> Dict[str, Any]:
    """Enqueue taey-notify only when shared outward authorization allows it.

    This is the orch-side mutation boundary for worker notify. The released
    ``taey-notify`` CLI also fail-closes worker outward types through the same
    ``authorize_outward_capability`` when fleet_orchestrator is importable.
    """
    require_outward_capability(
        session_id,
        channel="taey_notify",
        redis_client=redis_client,
        task_loader=task_loader,
        config=config,
    )
    sink = notify_sink or _default_notify_sink
    payload = sink(str(target), str(body), str(msg_type), str(session_id))
    if not isinstance(payload, dict):
        payload = {"ok": True, "result": payload}
    LOG.info(
        "outward taey-notify authorized session=%s target=%s type=%s",
        session_id,
        target,
        msg_type,
    )
    return payload


def notify_type_requires_outward_capability(msg_type: str) -> bool:
    return str(msg_type or "").strip().lower() in WORKER_OUTWARD_NOTIFY_TYPES
