"""GitHub outward mutations — thin facade over shared outward capability.

All authorization is ``authorize_outward_capability`` in
``fleet_orchestrator.outward_capability``. This module re-exports the GitHub
helpers and back-compat names so existing imports keep working.
"""
from __future__ import annotations

from .outward_capability import (  # noqa: F401
    OutwardAuthDecision,
    OutwardAuthorizationError,
    authorize_outward_capability,
    authorize_outward_github_mutation,
    post_commit_status,
    post_issue_comment,
    require_outward_capability,
    require_outward_github_mutation,
    resolve_session_id,
    send_outward_notify,
)

__all__ = [
    "OutwardAuthDecision",
    "OutwardAuthorizationError",
    "authorize_outward_capability",
    "authorize_outward_github_mutation",
    "post_commit_status",
    "post_issue_comment",
    "require_outward_capability",
    "require_outward_github_mutation",
    "resolve_session_id",
    "send_outward_notify",
]
