"""Ship-gate e2e — generic operator env contract stays minimal and explicit.

Env required to instantiate OrchConfig should be only the core Redis + Neo4j
connectivity values. Dashboard URL, notify library path, and refs root are
supported optional modes.

Run this in a clean/default environment. Operator `.env` files intentionally set
site-specific values such as `ORCH_DASHBOARD_URL`, so live-fleet production runs
should treat this as a clean-env contract test rather than an operator-env probe.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
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
from fleet_orchestrator.config import OrchConfig, REQUIRED_ENV, OPTIONAL_ENV
cfg = OrchConfig()
print(json.dumps({
    "required": list(REQUIRED_ENV),
    "optional_names": [item[0] for item in OPTIONAL_ENV],
    "dashboard_url": cfg.dashboard_url,
    "notify_lib_root": cfg.notify_lib_root,
}))
"""
    with tempfile.NamedTemporaryFile("w", encoding="utf-8") as clean_env:
        env["ORCH_DOTENV"] = clean_env.name
        output = subprocess.check_output([sys.executable, "-c", code], env=env, text=True)
    return json.loads(output.strip().splitlines()[-1])


def _notify_autoresolve_probe() -> dict:
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
import json, tempfile
from pathlib import Path
from unittest import mock
from fleet_orchestrator import config
root = Path(tempfile.mkdtemp()) / "claude-code-fleet-notify"
root.mkdir()
with mock.patch.object(config, "_notify_root_candidates", return_value=[root]):
    print(json.dumps({"resolved": str(config.resolve_notify_lib_root())}))
"""
    with tempfile.NamedTemporaryFile("w", encoding="utf-8") as clean_env:
        env["ORCH_DOTENV"] = clean_env.name
        output = subprocess.check_output([sys.executable, "-c", code], env=env, text=True)
    return json.loads(output.strip().splitlines()[-1])


def _dotenv_quote_probe() -> dict:
    """Loader must apply standard dotenv semantics to quoted/exported lines.

    Operators quote values so one .env stays BOTH shell-sourceable and
    loader-parseable (unquoted JSON braces brace-expand under
    `set -a; . .env`). Live finding 2026-06-11: surrounding quotes reached
    consumers, ORCH_SESSION_ROOTS failed JSON parse, every wake packet
    rendered empty while the selection code was correct.
    """
    import tempfile
    content = (
        "PLAIN=1\n"
        "export SINGLE_QUOTED_JSON='{\"conductor\":\"/tmp/x\"}'\n"
        'DOUBLE_QUOTED="hello world"\n'
        "INNER_QUOTE=it's-kept\n"
    )
    with tempfile.NamedTemporaryFile("w", suffix=".env", delete=False) as fh:
        fh.write(content)
        env_file = fh.name
    env = {"PYTHONPATH": str(ROOT), "ORCH_DOTENV": env_file}
    for key, value in os.environ.items():
        if key.startswith("PYTHON") and key != "PYTHONPATH":
            env[key] = value
    code = """
import json, os
import fleet_orchestrator.config  # triggers _load_dotenv_candidates()
print(json.dumps({k: os.environ.get(k) for k in
    ("PLAIN", "SINGLE_QUOTED_JSON", "DOUBLE_QUOTED", "INNER_QUOTE")}))
"""
    output = subprocess.check_output([sys.executable, "-c", code], env=env, text=True)
    os.unlink(env_file)
    return json.loads(output.strip().splitlines()[-1])


def main() -> int:
    probe = _minimal_config_probe()
    required = set(probe["required"])
    optional = set(probe["optional_names"])

    quotes = _dotenv_quote_probe()
    _check("plain values pass through", quotes["PLAIN"] == "1", quotes)
    _check("export-prefixed single-quoted JSON is unwrapped and parseable",
           json.loads(quotes["SINGLE_QUOTED_JSON"] or "null") == {"conductor": "/tmp/x"}, quotes)
    _check("double-quoted values are unwrapped", quotes["DOUBLE_QUOTED"] == "hello world", quotes)
    _check("interior quotes are preserved", quotes["INNER_QUOTE"] == "it's-kept", quotes)

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
    notify = _notify_autoresolve_probe()
    _check("notify root auto-resolves sibling checkout", notify["resolved"].endswith("claude-code-fleet-notify"), notify)

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - generic operator env contract is minimal and explicit.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
