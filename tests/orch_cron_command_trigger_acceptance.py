#!/usr/bin/env python3
"""Acceptance: orch-cron command triggers run deterministic scripts on cadence."""
from __future__ import annotations

import json
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
