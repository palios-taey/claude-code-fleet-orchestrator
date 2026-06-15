#!/usr/bin/env python3
"""Focused version-identity acceptance: the package version and the live /health
endpoint must both equal the single source of truth (fleet_orchestrator/version.py).

This exists because version.py once silently sat at 1.6.0 across the v1.7.0/v1.8.0/
v1.8.1 release tags, so every install misreported its version. This check is
deliberately self-contained — it has NO notify-helper / easy-setup install
dependencies (those make the broader easy_setup_acceptance.py unrunnable in the
CI stub environment) so it can run as a ship-gate step on every PR.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault("ORCH_REDIS_HOST", "127.0.0.1")
os.environ.setdefault("ORCH_REDIS_PORT", "6379")
os.environ.setdefault("ORCH_NEO4J_URI", "bolt://127.0.0.1:7687")
os.environ.setdefault("ORCH_NEO4J_DB", "neo4j")
os.environ.setdefault("ORCH_DASHBOARD_URL", "http://127.0.0.1:5002")

from fastapi.testclient import TestClient  # noqa: E402

from fleet_orchestrator.version import __version__ as SOURCE_OF_TRUTH  # noqa: E402
from fleet_orchestrator.easy_setup import package_version  # noqa: E402
from fleet_orchestrator.tasks_api import app  # noqa: E402

FAILURES: list[str] = []


def _assert(label: str, condition: bool, detail: object) -> None:
    if condition:
        print(f"PASS {label}")
    else:
        FAILURES.append(label)
        print(f"FAIL {label} {detail}")


def main() -> int:
    # package_version() reads version.py from disk via exec — a different code path
    # than the module import above; they must agree.
    _assert("release-version-identity", package_version() == SOURCE_OF_TRUTH, package_version())

    with TestClient(app) as client:
        health = client.get("/health")
        payload = health.json() if health.status_code == 200 else {}
    _assert(
        "health-version-identity",
        health.status_code == 200 and payload.get("version") == SOURCE_OF_TRUTH,
        {"status": health.status_code, "payload": payload},
    )

    if FAILURES:
        print(f"\nFAILURES: {FAILURES}")
        return 1
    print("\nPASS — package version and /health both equal version.py.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
