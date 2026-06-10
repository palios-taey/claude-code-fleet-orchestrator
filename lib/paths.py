"""Lightweight, dependency-free path helpers for a self-contained install.

No operator-specific (``/home/mira/...``) defaults live anywhere in the product. Runtime
state resolves under the user's data dir (or an explicit override) and gate targets resolve
to the install root, so the orchestrator runs on ANY user's machine with zero config.

Kept import-light on purpose: ``lib.accountability_ledger`` (a low-level module) must not be
forced to pull in the heavy ``lib.config`` (which requires the full ORCH_* environment) just
to decide where its file lives.
"""

from __future__ import annotations

import os
from pathlib import Path


def data_dir() -> Path:
    """Base directory for orchestrator runtime state (ledger, etc.).

    Resolution order (first that is set wins), never a hardcoded operator path:
      1. ``ORCH_DATA_DIR``
      2. ``$XDG_DATA_HOME/claude-code-fleet-orchestrator``
      3. ``~/.local/share/claude-code-fleet-orchestrator``
    """
    explicit = os.environ.get("ORCH_DATA_DIR")
    if explicit and explicit.strip():
        return Path(explicit).expanduser()
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg).expanduser() if xdg and xdg.strip() else Path.home() / ".local" / "share"
    return base / "claude-code-fleet-orchestrator"


def repo_root() -> Path:
    """The orchestrator install/repo root (the directory containing ``lib/``)."""
    return Path(__file__).resolve().parent.parent
