"""Shared accessors for fleet-notify Redis state.

The notify namespace is owned by claude-code-fleet-notify. Writers such as
dispatch and the Stop hook use ``identity.redis_connect`` (``REDIS_*``), so
orchestrator readers of ``${NOTIFY_KEY_PREFIX}:...`` keys must use the same
connection source instead of ``ORCH_REDIS_*``.
"""

from __future__ import annotations

import os
from typing import Any, Optional

from .config import ensure_notify_importable


def redis_connect() -> Any:
    ensure_notify_importable()
    from identity import redis_connect as connect  # type: ignore

    return connect()


def key_prefix() -> str:
    return os.environ.get("NOTIFY_KEY_PREFIX", "taey")


def state_key(node_id: str, suffix: str, *, prefix: Optional[str] = None) -> str:
    return f"{prefix or key_prefix()}:{node_id}:{suffix}"


def key(suffix: str, *, prefix: Optional[str] = None) -> str:
    return f"{prefix or key_prefix()}:{suffix}"
