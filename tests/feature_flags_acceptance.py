"""Acceptance: promised feature flags default on unless explicitly disabled."""

from __future__ import annotations

import logging
import os
import sys
from contextlib import contextmanager
from io import StringIO
from pathlib import Path
from typing import Iterator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fleet_orchestrator import feature_flags  # noqa: E402


FLAG_KEYS = (
    "ORCH_CHAT_ENABLED",
    "ORCH_DECISION_RECEIPTS_ENABLED",
    "ORCH_LOOPS_ENABLED",
    "ORCH_GATE_TEMPLATE_ENABLED",
    "ORCH_WAKE_PACKET_ENDPOINT_ENABLED",
    "ORCH_WAKE_PACKET_ENABLED",
)


@contextmanager
def _clean_env() -> Iterator[None]:
    old = {key: os.environ.get(key) for key in FLAG_KEYS}
    try:
        for key in FLAG_KEYS:
            os.environ.pop(key, None)
        yield
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextmanager
def _capture_feature_flag_warnings() -> Iterator[StringIO]:
    logger = logging.getLogger(feature_flags.__name__)
    stream = StringIO()
    handler = logging.StreamHandler(stream)
    old_level = logger.level
    old_propagate = logger.propagate
    try:
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)
        logger.propagate = False
        yield stream
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        logger.propagate = old_propagate


def _check(label: str, cond: bool, detail: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {detail}"))
    if not cond:
        raise AssertionError(label)


def main() -> int:
    with _clean_env(), _capture_feature_flag_warnings() as warnings:
        _check("all promised features default on", all((
            feature_flags.chat_enabled(),
            feature_flags.decision_receipts_enabled(),
            feature_flags.loops_enabled(),
            feature_flags.gate_template_enabled(),
            feature_flags.wake_packet_endpoint_enabled(),
        )))
        _check("unset flags do not warn", warnings.getvalue() == "", warnings.getvalue())

        os.environ["ORCH_CHAT_ENABLED"] = ""
        os.environ["ORCH_DECISION_RECEIPTS_ENABLED"] = "   "
        _check("empty values keep promised default on", feature_flags.chat_enabled() and feature_flags.decision_receipts_enabled())

        os.environ["ORCH_LOOPS_ENABLED"] = "definitely"
        _check("typo value keeps promised default on", feature_flags.loops_enabled())
        _check("typo value logs one-line warning", "ORCH_LOOPS_ENABLED='definitely'" in warnings.getvalue(), warnings.getvalue())

        os.environ["ORCH_GATE_TEMPLATE_ENABLED"] = "off"
        os.environ["ORCH_WAKE_PACKET_ENDPOINT_ENABLED"] = "0"
        _check("recognized false values disable", not feature_flags.gate_template_enabled() and not feature_flags.wake_packet_endpoint_enabled())

        os.environ.pop("ORCH_WAKE_PACKET_ENDPOINT_ENABLED", None)
        os.environ["ORCH_WAKE_PACKET_ENABLED"] = "0"
        _check("deprecated alias still explicitly disables wake endpoint", not feature_flags.wake_packet_endpoint_enabled())

        os.environ["ORCH_WAKE_PACKET_ENDPOINT_ENABLED"] = "garbage"
        _check("canonical typo stays on even when alias is false", feature_flags.wake_packet_endpoint_enabled())

    print("\nPASS - default-on feature flags ignore blanks/typos and honor explicit false.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
