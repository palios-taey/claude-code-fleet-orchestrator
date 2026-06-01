from setuptools import find_packages, setup


setup(
    name="fleet-orchestrator",
    version="1.2.1",
    description="Standalone orchestration layer for supervised AI worker sessions",
    packages=find_packages(include=["lib", "lib.*", "ui", "ui.*", "fleet_orchestrator", "fleet_orchestrator.*"]),
    package_data={"ui": ["index.html", "static/*.css", "static/*.js"]},
    include_package_data=True,
    scripts=[
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
