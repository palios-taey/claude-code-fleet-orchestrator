"""Public import surface for claude-code-fleet-orchestrator."""

import importlib

from fleet_orchestrator.version import __version__

__all__ = [
    "check_previous_task",
    "clear_current_task",
    "dispatch",
    "record_outcome",
    "__version__",
]

_DISPATCH_EXPORTS = {
    "check_previous_task",
    "clear_current_task",
    "dispatch",
    "record_outcome",
}


def __getattr__(name: str):
    if name in _DISPATCH_EXPORTS:
        dispatch_module = importlib.import_module("fleet_orchestrator.dispatch")
        return getattr(dispatch_module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
