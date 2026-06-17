#!/usr/bin/env python3
from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION_PATH = ROOT / "fleet_orchestrator" / "version.py"

ENTRYPOINT_CLIS = (
    "fleet-orchestrator-api",
    "orch",
    "install",
    "orch-cron",
    "orch-watch",
    "taey-dispatch",
    "taey-plan",
    "taey-question",
    "taey-task",
)


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts" if os.name == "nt" else "bin") / ("python.exe" if os.name == "nt" else "python")


def _venv_bin(venv: Path, command: str) -> Path:
    suffix = ".exe" if os.name == "nt" else ""
    return venv / ("Scripts" if os.name == "nt" else "bin") / f"{command}{suffix}"


def _source_version() -> str:
    text = VERSION_PATH.read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not match:
        raise AssertionError(f"could not parse {VERSION_PATH}")
    return match.group(1)


def _run(command: list[str | Path]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(item) for item in command],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=90,
    )


def _assert_cli_versions(venv: Path, expected: str) -> None:
    for command in ENTRYPOINT_CLIS:
        exe = _venv_bin(venv, command)
        result = _run([exe, "--version"])
        assert result.returncode == 0, f"{command} --version failed: {result.stderr or result.stdout}"
        assert result.stdout.strip() == expected, f"{command} version={result.stdout!r}, expected {expected!r}"


def _assert_entrypoint_wrappers(venv: Path) -> None:
    for command in ENTRYPOINT_CLIS:
        wrapper = _venv_bin(venv, command)
        text = wrapper.read_text(encoding="utf-8")
        assert "fleet_orchestrator.script_entrypoints" in text, f"{command} is not a package entry point"


def _write_version(version: str) -> None:
    original = VERSION_PATH.read_text(encoding="utf-8")
    updated = re.sub(r'__version__\s*=\s*"[^"]+"', f'__version__ = "{version}"', original, count=1)
    if updated == original:
        raise AssertionError(f"could not update {VERSION_PATH}")
    VERSION_PATH.write_text(updated, encoding="utf-8")


def main() -> None:
    original_text = VERSION_PATH.read_text(encoding="utf-8")
    original_version = _source_version()
    bumped_version = f"{original_version}.post999"

    with tempfile.TemporaryDirectory(prefix="orch-cli-entrypoint-drift-") as tmp:
        venv = Path(tmp) / "venv"
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        python = _venv_python(venv)
        subprocess.run(
            [str(python), "-m", "pip", "install", "--no-deps", "-e", str(ROOT)],
            check=True,
            cwd=str(ROOT),
        )

        _assert_entrypoint_wrappers(venv)
        _assert_cli_versions(venv, original_version)

        try:
            _write_version(bumped_version)
            _assert_cli_versions(venv, bumped_version)
        finally:
            VERSION_PATH.write_text(original_text, encoding="utf-8")

    print("cli_entrypoint_drift_acceptance: PASS")


if __name__ == "__main__":
    main()
