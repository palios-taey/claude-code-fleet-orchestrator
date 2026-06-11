from __future__ import annotations

import uvicorn

from fleet_orchestrator.easy_setup import api_host, api_port


def main() -> None:
    # Bind via the canonical security boundary: api_host() defaults to
    # 127.0.0.1 (this machine only); a non-loopback ORCH_HOST is an explicit
    # operator opt-in. The entrypoint must NOT reimplement this with a
    # 0.0.0.0 default — that would expose the unauthenticated mutable API.
    uvicorn.run("fleet_orchestrator.tasks_api:app", host=api_host(), port=api_port())
