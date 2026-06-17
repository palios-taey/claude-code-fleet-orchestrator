from __future__ import annotations

import sys
from typing import Any

from fleet_orchestrator.version import __version__


def _version_requested() -> bool:
    return sys.argv[1:] in (["--version"], ["-V"])


def _coerce_exit_code(code: Any) -> Any:
    if code is None:
        return 0
    return code


def _run_callable(import_path: str) -> Any:
    if _version_requested():
        print(__version__)
        return 0

    module_name, func_name = import_path.rsplit(":", 1)
    module = __import__(module_name, fromlist=[func_name])
    result = getattr(module, func_name)()
    return _coerce_exit_code(result)


def fleet_orchestrator_api_main() -> Any:
    return _run_callable("fleet_orchestrator.cli:main")


def orch_main() -> Any:
    return _run_callable("fleet_orchestrator.cli_orch:main")


def install_main() -> Any:
    return _run_callable("fleet_orchestrator.cli_install:main")


def orch_cron_main() -> Any:
    return _run_callable("fleet_orchestrator.cli_orch_cron:main")


def orch_watch_main() -> Any:
    return _run_callable("fleet_orchestrator.cli_orch_watch:main")


def taey_dispatch_main() -> Any:
    return _run_callable("fleet_orchestrator.cli_taey_dispatch:main")


def taey_plan_main() -> Any:
    return _run_callable("fleet_orchestrator.cli_taey_plan:main")


def taey_question_main() -> Any:
    return _run_callable("fleet_orchestrator.cli_taey_question:main")


def taey_task_main() -> Any:
    return _run_callable("fleet_orchestrator.cli_taey_task:main")
