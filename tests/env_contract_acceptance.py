"""Ship-gate e2e — generic operator env contract stays minimal and explicit.

Env required to instantiate OrchConfig should be only the core Redis + Neo4j
connectivity values. Dashboard URL, notify library path, and refs root are
supported optional modes.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _minimal_config_probe() -> dict:
    env = {
        "PYTHONPATH": str(ROOT),
        "ORCH_REDIS_HOST": "127.0.0.1",
        "ORCH_REDIS_PORT": "6379",
        "ORCH_NEO4J_URI": "bolt://127.0.0.1:7687",
        "ORCH_NEO4J_DB": "neo4j",
    }
    for key, value in os.environ.items():
        if key.startswith("PYTHON") and key != "PYTHONPATH":
            env[key] = value
    code = """
import json
from lib.config import OrchConfig, REQUIRED_ENV, OPTIONAL_ENV
cfg = OrchConfig()
print(json.dumps({
    "required": list(REQUIRED_ENV),
    "optional_names": [item[0] for item in OPTIONAL_ENV],
    "dashboard_url": cfg.dashboard_url,
    "notify_lib_root": cfg.notify_lib_root,
}))
"""
    output = subprocess.check_output([sys.executable, "-c", code], env=env, text=True)
    return json.loads(output.strip().splitlines()[-1])


def main() -> int:
    probe = _minimal_config_probe()
    required = set(probe["required"])
    optional = set(probe["optional_names"])

    _check(
        "REQUIRED_ENV is only core Redis + Neo4j connectivity",
        required == {"ORCH_REDIS_HOST", "ORCH_REDIS_PORT", "ORCH_NEO4J_URI", "ORCH_NEO4J_DB"},
        probe["required"],
    )
    _check("ORCH_DASHBOARD_URL is optional", "ORCH_DASHBOARD_URL" in optional and "ORCH_DASHBOARD_URL" not in required, probe)
    _check("ORCH_NOTIFY_LIB_ROOT is optional", "ORCH_NOTIFY_LIB_ROOT" in optional and "ORCH_NOTIFY_LIB_ROOT" not in required, probe)
    _check("ORCH_REF_ALLOWED_ROOT is optional", "ORCH_REF_ALLOWED_ROOT" in optional and "ORCH_REF_ALLOWED_ROOT" not in required, probe)
    _check("minimal generic config defaults dashboard URL", probe["dashboard_url"] == "http://127.0.0.1:5002", probe)
    _check("minimal generic config leaves notify root unset", probe["notify_lib_root"] is None, probe)

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - generic operator env contract is minimal and explicit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
