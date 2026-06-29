#!/usr/bin/env python3
"""Acceptance: orch-cron command triggers run deterministic scripts on cadence."""
from __future__ import annotations

import json
import logging
import shlex
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fleet_orchestrator import cli_orch_cron as cron  # noqa: E402


FAILURES: list[str] = []


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def exists(self, key: str) -> bool:
        return key in self.values

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.values[key] = value

    def delete(self, key: str) -> None:
        self.values.pop(key, None)


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _write_registry(path: Path, trigger: dict) -> None:
    path.write_text(json.dumps({"triggers": [trigger]}, indent=2), encoding="utf-8")


def _records(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="orch-cron-command-"))
    registry = tmp / "registry.json"
    state = tmp / "command.jsonl"
    timeout_state = tmp / "timeout.jsonl"
    redis = FakeRedis()
    now = datetime(2026, 6, 28, 8, 59, tzinfo=ZoneInfo("America/New_York"))
    python = shlex.quote(sys.executable)

    command = f"{python} -c \"import os; print(os.getcwd())\""
    trigger = {
        "id": "command-cycle",
        "command": command,
        "cwd": str(tmp),
        "timeout_sec": 5,
        "tz": "America/New_York",
        "minute": now.minute,
        "hours": [now.hour],
        "state_file": str(state),
        "enabled": True,
    }
    weekdays_trigger = {**trigger, "weekdays": [1, 2, 3, 4, 5]}
    week = [
        datetime(2026, 6, 22 + offset, now.hour, now.minute, tzinfo=ZoneInfo("America/New_York"))
        for offset in range(7)
    ]
    _check(
        "weekday trigger fires Monday through Friday",
        all(cron.should_fire(weekdays_trigger, day) for day in week[:5]),
        [day.isoweekday() for day in week[:5]],
    )
    _check(
        "weekday trigger skips Saturday and Sunday",
        not any(cron.should_fire(weekdays_trigger, day) for day in week[5:]),
        [day.isoweekday() for day in week[5:]],
    )
    _check(
        "trigger without weekdays remains every day",
        all(cron.should_fire(trigger, day) for day in week),
        [day.isoweekday() for day in week],
    )

    _write_registry(registry, {**trigger, "minute": (now.minute + 1) % 60})
    _check("command trigger does not fire off cadence", cron.tick(str(registry), redis, now_override=now) == 0, _records(state))

    _write_registry(registry, trigger)
    fires = cron.tick(str(registry), redis, now_override=now)
    records = _records(state)
    _check("command trigger fires on TZ-aware cadence", fires == 1, fires)
    _check("command trigger records exit code", records[-1].get("result") == "command:exit_0" and records[-1].get("exit_code") == 0, records)
    _check("command trigger captures stdout", str(tmp) in records[-1].get("stdout", ""), records[-1])
    _check("command trigger writes meta sidecar", (Path(str(state) + ".meta.json")).is_file(), str(state) + ".meta.json")

    duplicate = cron.tick(str(registry), redis, now_override=now)
    _check("command trigger dedups within same minute", duplicate == 0 and len(_records(state)) == 1, _records(state))

    timeout_trigger = {
        "id": "hung-command",
        "command": f"{python} -c \"import time; time.sleep(2)\"",
        "timeout_sec": 0.2,
        "tz": "America/New_York",
        "minute": now.minute,
        "hours": [now.hour],
        "state_file": str(timeout_state),
        "enabled": True,
    }
    _write_registry(registry, timeout_trigger)
    timeout_fires = cron.tick(str(registry), redis, now_override=now)
    timeout_records = _records(timeout_state)
    _check("hung command times out without wedging cron", timeout_fires == 1, timeout_fires)
    _check("hung command records timeout state", timeout_records[-1].get("result") == "command:timeout", timeout_records)

    bad_hours_state = tmp / "bad-hours.jsonl"
    valid_after_bad_state = tmp / "valid-after-bad.jsonl"
    malformed_registry = tmp / "malformed-registry.json"
    malformed_registry.write_text(
        json.dumps(
            {
                "triggers": [
                    {
                        **trigger,
                        "id": "bad-hours",
                        "hours": now.hour,
                        "state_file": str(bad_hours_state),
                    },
                    {
                        **trigger,
                        "id": "valid-after-bad",
                        "state_file": str(valid_after_bad_state),
                    },
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    malformed_fires = cron.tick(str(malformed_registry), FakeRedis(), now_override=now)
    _check("malformed trigger does not crash other triggers", malformed_fires == 1, malformed_fires)
    _check(
        "valid trigger still fires after malformed trigger",
        len(_records(valid_after_bad_state)) == 1,
        _records(valid_after_bad_state),
    )
    _check("malformed trigger writes no state record", _records(bad_hours_state) == [], _records(bad_hours_state))

    bad_weekday_state = tmp / "bad-weekday.jsonl"
    bad_weekday_type_state = tmp / "bad-weekday-type.jsonl"
    bad_weekday_registry = tmp / "bad-weekday-registry.json"
    bad_weekday_registry.write_text(
        json.dumps(
            {
                "triggers": [
                    {
                        **trigger,
                        "id": "bad-weekday-values",
                        "weekdays": [8],
                        "state_file": str(bad_weekday_state),
                    },
                    {
                        **trigger,
                        "id": "bad-weekday-type",
                        "weekdays": 5,
                        "state_file": str(bad_weekday_type_state),
                    },
                ],
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    warning_records: list[str] = []

    class _WarningCapture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            warning_records.append(record.getMessage())

    handler = _WarningCapture(level=logging.WARNING)
    cron.log.addHandler(handler)
    try:
        bad_weekday_redis = FakeRedis()
        bad_weekday_fires = cron.tick(str(bad_weekday_registry), bad_weekday_redis, now_override=now)
        bad_weekday_duplicate = cron.tick(str(bad_weekday_registry), bad_weekday_redis, now_override=now)
    finally:
        cron.log.removeHandler(handler)

    warning_text = "\n".join(warning_records)
    _check("bad weekday values log and fire all days", bad_weekday_fires == 2, bad_weekday_fires)
    _check("bad weekday values dedup after fail-open fire", bad_weekday_duplicate == 0, bad_weekday_duplicate)
    _check("bad weekday warning logs once", warning_text.count("bad weekdays in bad-weekday-values") == 1, warning_text)
    _check("bad weekday type warning logs once", warning_text.count("bad weekdays in bad-weekday-type") == 1, warning_text)
    _check("bad weekday values write state record", len(_records(bad_weekday_state)) == 1, _records(bad_weekday_state))
    _check(
        "bad weekday type writes state record",
        len(_records(bad_weekday_type_state)) == 1,
        _records(bad_weekday_type_state),
    )

    ambiguous = cron.fire_trigger(
        FakeRedis(),
        {"id": "ambiguous", "command": "echo no", "task_id": "task-1", "enabled": True},
        now,
    )
    _check("ambiguous trigger modes skip loud", ambiguous == "skipped:ambiguous_trigger", ambiguous)

    empty = cron.fire_trigger(FakeRedis(), {"id": "empty-command", "command": "", "enabled": True}, now)
    _check("empty command has no fallback", empty == "skipped:no_command", empty)

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - orch-cron command triggers run, dedup, timeout, and skip invalid registry shapes.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
