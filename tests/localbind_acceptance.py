"""Ship-gate e2e — mutable API documents and defaults to loopback binding.

The product is local and single-user. The localhost bind is the security
boundary for the mutable API; non-loopback exposure is an explicit operator
decision, not a documented default.
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _read(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _env_value(text: str, name: str) -> str | None:
    match = re.search(rf"^{re.escape(name)}=(.*)$", text, re.MULTILINE)
    return match.group(1).strip() if match else None


def main() -> int:
    env_example = _read(".env.example")
    readme = _read("README.md")
    readme_flat = " ".join(readme.split())
    security = _read("SECURITY.md")
    walkthrough = _read("docs/WALKTHROUGH.md")
    all_interface = ".".join(["0", "0", "0", "0"])

    _check(".env.example binds dashboard host to loopback", _env_value(env_example, "ORCH_HOST") == "127.0.0.1")
    _check(".env.example advertises loopback dashboard URL",
           _env_value(env_example, "ORCH_DASHBOARD_URL") == "http://127.0.0.1:5002")
    _check(".env.example contains no all-interface bind example", all_interface not in env_example)
    _check(".env.example contains no hardcoded LAN dashboard URL",
           not re.search(r"ORCH_DASHBOARD_URL=http://(?:10|172\\.(?:1[6-9]|2\\d|3[01])|192\\.168)\\.", env_example))
    _check(".env.example documents tokenless non-loopback fail-closed override",
           "startup fails closed" in env_example and "ORCH_ALLOW_UNAUTH_NON_LOOPBACK=1" in env_example)

    _check("README documents loopback as default", "By default the dashboard binds `127.0.0.1`" in readme)
    _check("README states localhost bind is the security boundary",
           "localhost bind is the security boundary" in readme)
    _check("README requires non-loopback exposure to be explicit opt-in",
           "explicit, deliberate operator opt-in" in readme_flat and "must not accept untrusted callers" in readme_flat)
    _check("README documents tokenless non-loopback override as acknowledgement",
           "fails closed unless `ORCH_ALLOW_UNAUTH_NON_LOOPBACK=1`" in readme and "override is not authentication" in readme)
    _check("Walkthrough documents loopback security boundary",
           "default `ORCH_HOST=127.0.0.1` is the security boundary" in walkthrough)
    _check("Walkthrough documents non-loopback override",
           "ORCH_ALLOW_UNAUTH_NON_LOOPBACK=1" in walkthrough)
    _check("SECURITY documents override is not auth",
           "ORCH_ALLOW_UNAUTH_NON_LOOPBACK=1" in security and "not authentication" in security)

    saved = os.environ.pop("ORCH_HOST", None)
    try:
        from fleet_orchestrator.easy_setup import api_host  # noqa: E402

        _check("easy_setup api_host defaults to loopback", api_host() == "127.0.0.1")
    finally:
        if saved is not None:
            os.environ["ORCH_HOST"] = saved

    cli_orch = _read("fleet_orchestrator/cli_orch.py")
    _check("orch serve launches uvicorn with explicit --host", '"--host", host' in cli_orch)
    _check("orch public dashboard stays loopback-bound", '"127.0.0.1"' in _read("scripts/orch-public"))

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - mutable API defaults and docs lock localhost as the security boundary.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
