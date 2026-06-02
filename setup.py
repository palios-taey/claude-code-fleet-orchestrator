from pathlib import Path

from setuptools import find_packages, setup


def load_version() -> str:
    namespace = {}
    version_path = Path(__file__).resolve().parent / "fleet_orchestrator" / "version.py"
    exec(version_path.read_text(encoding="utf-8"), namespace)
    return namespace["__version__"]


setup(
    name="fleet-orchestrator",
    version=load_version(),
    description="Standalone orchestration layer for supervised AI worker sessions",
    packages=find_packages(include=["lib", "lib.*", "ui", "ui.*", "fleet_orchestrator", "fleet_orchestrator.*"]),
    package_data={"ui": ["index.html", "static/*.css", "static/*.js"]},
    include_package_data=True,
    scripts=[
        "scripts/orch",
        "scripts/install",
        "scripts/orch-cron",
        "scripts/orch-watch",
        "scripts/taey-plan",
        "scripts/taey-task",
    ],
    install_requires=[
        "fastapi",
        "neo4j",
        "redis",
        "uvicorn",
    ],
)
