#!/usr/bin/env python3
"""Acceptance: operator CLI HTTP calls survive transient :5002 restarts."""
from __future__ import annotations

import contextlib
import io
import json
from pathlib import Path
import sys
import urllib.error
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fleet_orchestrator.cli_http import api_json_or_exit

FAILURES: list[str] = []


class _Response:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def _check(label: str, condition: bool, extra: object = "") -> None:
    print(("PASS " if condition else "FAIL ") + label + ("" if condition else f" -> {extra}"))
    if not condition:
        FAILURES.append(label)


def main() -> int:
    refused = urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))
    calls = [refused, _Response({"ok": True})]

    def flaky_urlopen(_request, timeout=0):
        item = calls.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    with mock.patch("fleet_orchestrator.cli_http.urllib.request.urlopen", side_effect=flaky_urlopen) as urlopen, \
         mock.patch("fleet_orchestrator.cli_http.time.sleep") as sleep:
        result = api_json_or_exit("GET", "http://127.0.0.1:5002", "/api/health", retry_delays=(0.01, 0.02))
    _check("Connection refused is retried", result == {"ok": True} and urlopen.call_count == 2 and sleep.call_count == 1, {"result": result, "calls": urlopen.call_count})

    err = io.StringIO()
    with mock.patch("fleet_orchestrator.cli_http.urllib.request.urlopen", side_effect=refused) as urlopen, \
         mock.patch("fleet_orchestrator.cli_http.time.sleep") as sleep:
        try:
            with contextlib.redirect_stderr(err):
                api_json_or_exit("GET", "http://127.0.0.1:5002", "/api/health", retry_delays=(0.01, 0.02))
            code = 0
        except SystemExit as exc:
            code = int(exc.code or 0)
    message = err.getvalue()
    _check("Persistent refused exits nonzero after all attempts", code == 1 and urlopen.call_count == 3 and sleep.call_count == 2, {"code": code, "calls": urlopen.call_count})
    _check("Persistent refused prints restart guidance", "mid-restart" in message and "retry in a moment" in message, message)
    _check("Persistent refused avoids raw urlopen-only error", "urlopen error" not in message, message)

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - CLI HTTP helper retries refused connections and teaches on persistent flaps.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
