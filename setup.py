from pathlib import Path

from setuptools import find_packages, setup


ROOT = Path(__file__).resolve().parent


def load_version() -> str:
    namespace = {}
    version_path = ROOT / "fleet_orchestrator" / "version.py"
    exec(version_path.read_text(encoding="utf-8"), namespace)
    return namespace["__version__"]


def load_requirements() -> list[str]:
    return [
        line.strip()
        for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]


setup(
    name="fleet-orchestrator",
    version=load_version(),
    description="Standalone orchestration layer for supervised AI worker sessions",
    packages=find_packages(include=["fleet_orchestrator", "fleet_orchestrator.*", "ui", "ui.*"]),
    package_data={"ui": ["index.html", "static/*.css", "static/*.js"]},
    include_package_data=True,
    entry_points={
        "console_scripts": [
            "fleet-orchestrator-api = fleet_orchestrator.script_entrypoints:fleet_orchestrator_api_main",
            "orch = fleet_orchestrator.script_entrypoints:orch_main",
            "install = fleet_orchestrator.script_entrypoints:install_main",
            "orch-cron = fleet_orchestrator.script_entrypoints:orch_cron_main",
            "orch-watch = fleet_orchestrator.script_entrypoints:orch_watch_main",
            "taey-dispatch = fleet_orchestrator.script_entrypoints:taey_dispatch_main",
            "taey-plan = fleet_orchestrator.script_entrypoints:taey_plan_main",
            "taey-question = fleet_orchestrator.script_entrypoints:taey_question_main",
            "taey-receipts = fleet_orchestrator.script_entrypoints:taey_receipts_main",
            "taey-task = fleet_orchestrator.script_entrypoints:taey_task_main",
        ],
    },
    install_requires=load_requirements(),
    extras_require={
        # Running the acceptance suite needs the FastAPI TestClient transport,
        # which requires httpx. `pip install -e ".[test]"` to run tests/*.py.
        "test": ["httpx>=0.27,<1.0"],
    },
)
