#!/usr/bin/env python3
from __future__ import annotations

import contextlib
import io
import os
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

FAILURES: list[str] = []


def _check(label: str, condition: bool, detail: object = "") -> None:
    print(("PASS " if condition else "FAIL ") + label + ("" if condition else f" -> {detail}"))
    if not condition:
        FAILURES.append(label)


@contextlib.contextmanager
def _patched_argv(args: list[str]):
    original = sys.argv[:]
    sys.argv = args[:]
    try:
        yield
    finally:
        sys.argv = original


@contextlib.contextmanager
def _patched_env(values: dict[str, str]):
    saved = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _capture_call(func):
    stdout = io.StringIO()
    stderr = io.StringIO()
    with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
        result = func()
    return result, stdout.getvalue(), stderr.getvalue()


def _assert_api_help() -> None:
    from fleet_orchestrator.script_entrypoints import fleet_orchestrator_api_main

    with _patched_argv(["fleet-orchestrator-api", "--help"]):
        result, stdout, stderr = _capture_call(fleet_orchestrator_api_main)
    _check("fleet-orchestrator-api --help exits zero", result == 0, result)
    _check("fleet-orchestrator-api --help prints usage", "Usage: fleet-orchestrator-api" in stdout, stdout)
    _check("fleet-orchestrator-api --help explains ORCH env", "ORCH_*" in stdout and "127.0.0.1:5002" in stdout, stdout)
    _check("fleet-orchestrator-api --help does not start server", "Started server process" not in stdout + stderr, stdout + stderr)

    with _patched_argv(["fleet-orchestrator-api", "-h"]):
        short_result, short_stdout, _ = _capture_call(fleet_orchestrator_api_main)
    _check("fleet-orchestrator-api -h exits zero", short_result == 0, short_result)
    _check("fleet-orchestrator-api -h prints same usage family", "Run the Fleet Orchestrator FastAPI server" in short_stdout, short_stdout)


def _assert_orch_serve_port_conflict() -> None:
    from fleet_orchestrator.cli_orch import cmd_serve

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as occupied:
        occupied.bind(("127.0.0.1", 0))
        occupied.listen(1)
        port = occupied.getsockname()[1]
        with _patched_env({"ORCH_DOTENV": "empty", "ORCH_HOST": "127.0.0.1", "ORCH_PORT": str(port)}):
            result, stdout, stderr = _capture_call(lambda: cmd_serve(None))

    _check("orch serve port conflict exits one", result == 1, result)
    _check("orch serve port conflict names occupied address", f"127.0.0.1:{port} is already in use" in stderr, stderr)
    _check("orch serve port conflict teaches ORCH_PORT", "set ORCH_PORT to a free port" in stderr, stderr)
    _check("orch serve port conflict does not print UI banner", "Orchestrator UI ->" not in stdout, stdout)
    _check("orch serve port conflict does not print foreground banner", "Foreground server" not in stdout, stdout)


def _assert_config_read_auth_note() -> None:
    text = (ROOT / "docs/CONFIGURATION.md").read_text(encoding="utf-8")
    _check("CONFIGURATION says token gates mutation only", "gates mutation only" in text, text[:400])
    _check("CONFIGURATION says read endpoints remain open", "Read endpoints remain open by design" in text, text[:400])
    _check("CONFIGURATION names wake-packet read confidentiality", "GET /api/sessions/{id}/wake-packet" in text, text[:400])
    _check("CONFIGURATION says token does not make reads private", "token does not make read APIs private" in text, text[:400])


def main() -> int:
    _assert_api_help()
    _assert_orch_serve_port_conflict()
    _assert_config_read_auth_note()
    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - first-run hardening catches API help, port conflicts, and read-auth docs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
