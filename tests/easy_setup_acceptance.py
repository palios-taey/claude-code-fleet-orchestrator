#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import tempfile
from unittest import mock
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for candidate in (ROOT / ".env", Path.home() / "claude-code-fleet-orchestrator/.env"):
    if "ORCH_DOTENV" not in os.environ and candidate.is_file():
        os.environ["ORCH_DOTENV"] = str(candidate)
        break
os.environ.setdefault("ORCH_REDIS_HOST", "127.0.0.1")
os.environ.setdefault("ORCH_REDIS_PORT", "6379")
os.environ.setdefault("ORCH_NEO4J_URI", "bolt://127.0.0.1:7687")
os.environ.setdefault("ORCH_NEO4J_DB", "neo4j")
os.environ.setdefault("ORCH_DASHBOARD_URL", "http://127.0.0.1:5002")

from lib.easy_setup import (  # noqa: E402
    MANAGED_DENIES,
    apply_claude_permission_guard,
    compose_scope,
    package_version,
    remove_claude_permission_guard,
)
from lib.tasks_api import app  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

FAILURES: list[str] = []


def _assert(label: str, condition: bool, detail: object) -> None:
    if condition:
        print(f"PASS {label}")
    else:
        FAILURES.append(label)
        print(f"FAIL {label} {detail}")


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        settings_path = Path(td) / "settings.json"
        settings_path.write_text(json.dumps({"permissions": {"deny": ["ExistingDeny"]}}, indent=2) + "\n", encoding="utf-8")

        first = apply_claude_permission_guard(settings_path, apply=True)
        second = apply_claude_permission_guard(settings_path, apply=True)
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        deny = settings["permissions"]["deny"]
        marker = settings["_managedBy"]["claude-code-fleet-orchestrator"]["permissions.deny"]
        _assert(
            "deny-exactly-once",
            all(deny.count(entry) == 1 for entry in MANAGED_DENIES) and marker == MANAGED_DENIES and first["changed"] and not second["changed"],
            deny,
        )

        removed = remove_claude_permission_guard(settings_path, apply=True)
        settings_removed = json.loads(settings_path.read_text(encoding="utf-8"))
        deny_removed = settings_removed["permissions"]["deny"]
        _assert(
            "deny-removal-reversible",
            removed["changed"] and all(entry not in deny_removed for entry in MANAGED_DENIES) and "ExistingDeny" in deny_removed,
            deny_removed,
        )

        with mock.patch("lib.tasks_api.get_ready_tasks", return_value=[]):
            client = TestClient(app)
            health = client.get("/health")
            payload = health.json()
        _assert(
            "health-version-identity",
            health.status_code == 200 and payload.get("version") == package_version(),
            payload,
        )

        scope = compose_scope()
        _assert(
            "doctor-scope-shape",
            "127.0.0.1:7687" in scope["ports"] and "orch_neo4j_data" in scope["volumes"] and any(path.endswith("settings.json") for path in scope["files"]),
            scope,
        )

        if os.environ.get("EASY_SETUP_ACCEPTANCE_INJECT_FAIL") == "1":
            _assert("injected-fail", False, "EASY_SETUP_ACCEPTANCE_INJECT_FAIL=1")

    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
