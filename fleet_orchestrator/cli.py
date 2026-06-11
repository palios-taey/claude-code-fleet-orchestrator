from __future__ import annotations

import os

import uvicorn


def main() -> None:
    host = os.environ.get("ORCH_HOST", "0.0.0.0")
    port = int(os.environ.get("ORCH_PORT", "5002"))
    uvicorn.run("fleet_orchestrator.tasks_api:app", host=host, port=port)
