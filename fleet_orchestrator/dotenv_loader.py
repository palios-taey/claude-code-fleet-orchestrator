"""Dependency-free dotenv loading shared by config and console entrypoints."""

from __future__ import annotations

import os
from pathlib import Path


DOTENV_SUPPRESS_VALUES = {"empty"}


def _candidate_paths() -> list[Path]:
    explicit = os.environ.get("ORCH_DOTENV")
    if explicit and explicit.strip().lower() in DOTENV_SUPPRESS_VALUES:
        return []
    candidates: list[Path] = []
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(Path.cwd() / ".env")
    candidates.append(Path(__file__).resolve().parent.parent / ".env")
    candidates.append(Path.home() / "claude-code-fleet-orchestrator" / ".env")
    return candidates


def load_dotenv_candidates() -> None:
    """Load the first configured/found dotenv file using conservative semantics."""
    for env_path in _candidate_paths():
        if not env_path.is_file():
            continue
        with env_path.open(encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.replace("export ", "").strip()
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                    value = value[1:-1]
                os.environ.setdefault(key, value)
        break
