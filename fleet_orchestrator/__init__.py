"""Public import surface for claude-code-fleet-orchestrator."""

from lib.dispatch import check_previous_task, clear_current_task, dispatch, record_outcome
from fleet_orchestrator.version import __version__

__all__ = [
    "check_previous_task",
    "clear_current_task",
    "dispatch",
    "record_outcome",
    "__version__",
]
