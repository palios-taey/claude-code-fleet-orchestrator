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
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fleet_orchestrator.test_isolation import assert_acceptance_redis_isolated  # noqa: E402

assert_acceptance_redis_isolated()
os.environ.setdefault("ORCH_NEO4J_URI", "bolt://127.0.0.1:7687")
os.environ.setdefault("ORCH_NEO4J_DB", "neo4j")
os.environ.setdefault("ORCH_DASHBOARD_URL", "http://127.0.0.1:5002")

from fastapi.testclient import TestClient  # noqa: E402

from fleet_orchestrator.version import __version__ as SOURCE_OF_TRUTH  # noqa: E402
from fleet_orchestrator.easy_setup import package_version  # noqa: E402
from fleet_orchestrator.tasks_api import app  # noqa: E402

FAILURES: list[str] = []
VERSION_PATH = ROOT / "fleet_orchestrator" / "version.py"
DISK_DRIFT_SENTINEL = "999.999.999-request-time-disk"


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

    original_version_py = VERSION_PATH.read_text(encoding="utf-8")
    drifted_version_py = re.sub(
        r'__version__\s*=\s*"[^"]+"',
        f'__version__ = "{DISK_DRIFT_SENTINEL}"',
        original_version_py,
        count=1,
    )
    _assert("disk-drift-sentinel-applied", drifted_version_py != original_version_py, VERSION_PATH)
    try:
        VERSION_PATH.write_text(drifted_version_py, encoding="utf-8")
        disk_version = package_version()
        with TestClient(app) as client:
            drift_health = client.get("/health")
            drift_payload = drift_health.json() if drift_health.status_code == 200 else {}
    finally:
        VERSION_PATH.write_text(original_version_py, encoding="utf-8")

    _assert("package-version-sees-disk-drift", disk_version == DISK_DRIFT_SENTINEL, disk_version)
    _assert(
        "health-version-is-running-process-version",
        drift_health.status_code == 200 and drift_payload.get("version") == SOURCE_OF_TRUTH,
        {"status": drift_health.status_code, "payload": drift_payload, "disk_version": disk_version},
    )
    _assert(
        "health-version-not-request-time-disk",
        drift_payload.get("version") != DISK_DRIFT_SENTINEL,
        drift_payload,
    )

    if FAILURES:
        print(f"\nFAILURES: {FAILURES}")
        return 1
    print("\nPASS — package version equals version.py and /health reports the loaded process version.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
