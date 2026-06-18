"""Acceptance: unauthenticated non-loopback mutable API startup fails closed."""
from __future__ import annotations

import io
import logging
import os
import sys
from contextlib import contextmanager
from types import SimpleNamespace
from typing import Iterator
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import fleet_orchestrator.tasks_api as tasks_api  # noqa: E402


FAILURES: list[str] = []
ENV_KEYS = ("ORCH_AUTH_TOKEN", "ORCH_ALLOW_UNAUTH_NON_LOOPBACK", "ORCH_HOST")


def _check(label: str, cond: bool, detail: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {detail}"))
    if not cond:
        FAILURES.append(label)


@contextmanager
def _preserved_env() -> Iterator[None]:
    original = {key: os.environ.get(key) for key in ENV_KEYS}
    try:
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        yield
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextmanager
def _captured_task_api_logs() -> Iterator[io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    logger = tasks_api.LOGGER
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.WARNING)
    try:
        yield stream
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)


def _startup_result(host: str, *, token: str | None = None, override: str | None = None) -> dict[str, object]:
    with _preserved_env(), _captured_task_api_logs() as logs:
        if token is not None:
            os.environ["ORCH_AUTH_TOKEN"] = token
        if override is not None:
            os.environ["ORCH_ALLOW_UNAUTH_NON_LOOPBACK"] = override

        with mock.patch.object(tasks_api, "api_host", return_value=host), \
             mock.patch.object(tasks_api, "_cfg", return_value=SimpleNamespace()), \
             mock.patch.object(tasks_api, "init_schema", return_value={"errors": []}) as init_schema, \
             mock.patch.object(tasks_api, "notify_redis_connect", return_value=object()), \
             mock.patch.object(tasks_api, "ensure_handoff_index_backfilled", return_value=0):
            try:
                tasks_api._init_schema_on_startup()
                outcome = "started"
                error = ""
            except SystemExit as exc:
                outcome = "refused"
                error = str(exc)
            return {
                "outcome": outcome,
                "error": error,
                "init_schema_called": init_schema.called,
                "logs": logs.getvalue(),
            }


def main() -> int:
    refused = _startup_result("0.0.0.0")
    _check("non-loopback without token or override refuses startup", refused["outcome"] == "refused", refused)
    _check("refusal happens before schema startup continues", refused["init_schema_called"] is False, refused)
    _check(
        "refusal names override and safer alternatives",
        all(term in str(refused["error"]) for term in ("ORCH_ALLOW_UNAUTH_NON_LOOPBACK", "ORCH_AUTH_TOKEN", "ORCH_HOST=127.0.0.1")),
        refused,
    )

    overridden = _startup_result("0.0.0.0", override="1")
    _check("override acknowledges non-loopback tokenless startup", overridden["outcome"] == "started" and overridden["init_schema_called"] is True, overridden)
    _check("override logs explicit acknowledgement", "ORCH_ALLOW_UNAUTH_NON_LOOPBACK" in str(overridden["logs"]) and "explicitly acknowledged" in str(overridden["logs"]), overridden)

    loopback = _startup_result("127.0.0.1")
    _check("loopback without token starts without override", loopback["outcome"] == "started" and loopback["init_schema_called"] is True, loopback)

    token = _startup_result("0.0.0.0", token="dev-token")
    _check("non-loopback with token starts without override", token["outcome"] == "started" and token["init_schema_called"] is True, token)

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - unauthenticated non-loopback startup fails closed unless explicitly acknowledged.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
