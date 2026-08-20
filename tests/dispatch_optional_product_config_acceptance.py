#!/usr/bin/env python3
"""Acceptance: optional dispatch product scoping does not require strict OrchConfig."""
from __future__ import annotations

import contextlib
import io
import os
import sys
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import fleet_orchestrator.cli_taey_task as cli_task  # noqa: E402
import fleet_orchestrator.dispatch as dispatch_module  # noqa: E402


FAILURES: list[str] = []
REQUIRED_ORCH_ENV = (
    "ORCH_DOTENV",
    "ORCH_REDIS_HOST",
    "ORCH_REDIS_PORT",
    "ORCH_NEO4J_URI",
    "ORCH_NEO4J_DB",
    "ORCH_PRODUCT_OWNER_MAP",
    "PRODUCT_OWNER_MAP",
    "ORCH_NOTIFY_CLI",
    "TAEY_NODE_ID",
)


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


@contextlib.contextmanager
def _dispatch_env(product_map: str | None = None):
    saved = {key: os.environ.get(key) for key in REQUIRED_ORCH_ENV}
    try:
        os.environ["ORCH_DOTENV"] = "empty"
        os.environ["TAEY_NODE_ID"] = "supervisor"
        os.environ["ORCH_NOTIFY_CLI"] = "taey-notify"
        for key in ("ORCH_REDIS_HOST", "ORCH_REDIS_PORT", "ORCH_NEO4J_URI", "ORCH_NEO4J_DB", "PRODUCT_OWNER_MAP"):
            os.environ.pop(key, None)
        if product_map is None:
            os.environ.pop("ORCH_PRODUCT_OWNER_MAP", None)
        else:
            os.environ["ORCH_PRODUCT_OWNER_MAP"] = product_map
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


class FakeRedis:
    def __init__(self, values: dict[str, str] | None = None):
        self.values = values or {}
        self.reads: list[str] = []

    def get(self, key: str) -> str | None:
        self.reads.append(key)
        return self.values.get(key)


def _assert_product_lookup_is_optional() -> None:
    with _dispatch_env():
        product_id = dispatch_module._resolve_product_id("jd-reader-gemini")
    _check("missing product map resolves to no product without ORCH_REDIS_HOST", product_id is None, product_id)

    with _dispatch_env('{"jd-reader":"careers"}'):
        product_id = dispatch_module._resolve_product_id("jd-reader-gemini")
    _check("product map still parses without ORCH_REDIS_HOST", product_id == "careers", product_id)


def _assert_cli_dispatch_without_orch_redis() -> None:
    task_id = "dispatch-optional-config::task"
    api_calls: list[tuple[str, str, object]] = []

    def fake_api(method: str, endpoint: str, data=None):
        api_calls.append((method, endpoint, data))
        if method == "GET" and endpoint == f"/api/tasks/{task_id}":
            return {"id": task_id, "description": "dispatch with optional product config"}
        if method == "POST" and endpoint == "/api/sessions/jd-reader-gemini/notify":
            return {"ok": True}
        raise AssertionError((method, endpoint, data))

    args = SimpleNamespace(task_id=task_id, peer="jd-reader-gemini", priority="normal", force=False)
    with _dispatch_env(), \
         mock.patch.object(cli_task, "api_call", side_effect=fake_api), \
         mock.patch.object(cli_task, "conflicting_binding", return_value=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            cli_task.cmd_dispatch(args)

    _check("taey-task dispatch succeeds without ORCH_REDIS_HOST", "OK: dispatched" in out.getvalue(), out.getvalue())
    _check("dispatch mutation routes through the API", api_calls[-1] == (
        "POST",
        "/api/sessions/jd-reader-gemini/notify",
        {
            "dispatch": True,
            "task_id": task_id,
            "from": "supervisor",
            "priority": "normal",
            "force": False,
        },
    ), api_calls)


def _assert_bug_lock_still_enforced_when_product_map_exists() -> None:
    fake_redis = FakeRedis({
        "support:product:careers:bug_lock": "true",
        "support:product:careers:bug_lock_reason": "acceptance lock",
    })
    with _dispatch_env('{"jd-reader":"careers"}'), \
         mock.patch.object(dispatch_module, "_redis_connect", return_value=fake_redis), \
         mock.patch.object(dispatch_module, "hook_installation_status", return_value=SimpleNamespace(ok=True, detail="hooked")):
        refused = None
        try:
            dispatch_module.dispatch("jd-reader-gemini", "task-locked", "locked")
        except dispatch_module.BugLockActive as exc:
            refused = str(exc)

    _check("bug lock remains enforced with product map and no ORCH_REDIS_HOST", refused is not None and "careers" in refused, refused)


def main() -> int:
    _assert_product_lookup_is_optional()
    _assert_cli_dispatch_without_orch_redis()
    _assert_bug_lock_still_enforced_when_product_map_exists()
    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - dispatch optional product lookup does not require strict OrchConfig.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
