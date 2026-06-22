#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
from fnmatch import fnmatch
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fleet_orchestrator import cli_orch_watch as watch  # noqa: E402


FAILURES: list[str] = []


class FakeRedis:
    def __init__(self):
        self.store: dict[str, object] = {}

    def set(self, key: str, value: object, ex: int | None = None):
        del ex
        self.store[key] = value
        return True

    def get(self, key: str):
        return self.store.get(key)

    def exists(self, key: str) -> int:
        return 1 if key in self.store else 0

    def delete(self, *keys: str) -> int:
        count = 0
        for key in keys:
            if key in self.store:
                del self.store[key]
                count += 1
        return count

    def lpush(self, key: str, value: object) -> int:
        self.store.setdefault(key, [])
        assert isinstance(self.store[key], list)
        self.store[key].insert(0, value)
        return len(self.store[key])

    def lrange(self, key: str, start: int, end: int):
        values = list(self.store.get(key, []))
        length = len(values)
        if start < 0:
            start = max(length + start, 0)
        if end < 0:
            end = length + end
        return values[start:end + 1]

    def scan_iter(self, match: str | None = None):
        for key in list(self.store):
            if match is None or fnmatch(key, match):
                yield key

    def time(self):
        return (1000, 0)


def _check(label: str, condition: bool, detail: object = "") -> None:
    print(("PASS " if condition else "FAIL ") + label)
    if not condition:
        FAILURES.append(f"{label}: {detail}")


def _run_with_service(service_status: str, callback):
    commands: list[list[str]] = []
    delays: list[float] = []

    def fake_run(cmd, **kwargs):
        del kwargs
        command = [str(part) for part in cmd]
        commands.append(command)
        if command[:3] == ["systemctl", "--user", "is-active"]:
            return SimpleNamespace(
                returncode=0 if service_status == "active" else 3,
                stdout=f"{service_status}\n",
                stderr="",
            )
        if command[:1] == ["tmux"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected subprocess command: {command}")

    with mock.patch.object(watch.subprocess, "run", side_effect=fake_run):
        with mock.patch.object(watch.shutil, "which", return_value=None):
            with mock.patch.object(watch.time, "sleep", side_effect=lambda seconds: delays.append(seconds)):
                result = callback()
    return result, commands, delays


def _tmux_commands(commands: list[list[str]]) -> list[list[str]]:
    return [cmd for cmd in commands if cmd[:1] == ["tmux"]]


def _assert_oob_submit_sequence(label: str, commands: list[list[str]], delays: list[float]) -> None:
    tmux_commands = _tmux_commands(commands)
    _check(f"{label}: tmux has clear/write/submit steps", len(tmux_commands) == 3, tmux_commands)
    if len(tmux_commands) != 3:
        return
    _check(f"{label}: clear input first",
           tmux_commands[0] == ["tmux", "send-keys", "-t", "conductor", "C-u"],
           tmux_commands)
    _check(f"{label}: write body as literal text",
           tmux_commands[1][:5] == ["tmux", "send-keys", "-t", "conductor", "-l"]
           and "CRITICAL" in tmux_commands[1][5],
           tmux_commands)
    _check(f"{label}: submit with separate Enter",
           tmux_commands[2] == ["tmux", "send-keys", "-t", "conductor", "Enter"],
           tmux_commands)
    _check(f"{label}: delay before Enter", delays == [0.3], delays)


def test_stderr_logging_is_line_buffered() -> None:
    class FakeStderr:
        def __init__(self):
            self.kwargs: dict[str, object] | None = None

        def reconfigure(self, **kwargs):
            self.kwargs = kwargs

    fake = FakeStderr()
    _check("orch-watch configures stderr line buffering",
           watch._configure_realtime_stderr(fake) is True and fake.kwargs == {"line_buffering": True},
           fake.kwargs)


def test_killed_service_alerts() -> None:
    r = FakeRedis()
    r.set(watch.notify_daemon_heartbeat_key(), "995.000000+notify-host")

    result, commands, delays = _run_with_service(
        "failed",
        lambda: watch.check_notify_daemon_liveness(
            r,
            now=1000.0,
            heartbeat_max_age_sec=15,
            alert_target="conductor",
            dedup_ttl_sec=0,
        ),
    )

    _check("killed service fires OOB alert", result["alerted"] is True, result)
    _assert_oob_submit_sequence("killed service alert", commands, delays)
    _check("alert does not route through taey-notify", not any("taey-notify" in cmd[0] for cmd in commands), commands)


def test_stale_heartbeat_alerts() -> None:
    r = FakeRedis()
    r.set(watch.notify_daemon_heartbeat_key(), "900.000000+notify-host")

    result, commands, delays = _run_with_service(
        "active",
        lambda: watch.check_notify_daemon_liveness(
            r,
            now=1000.0,
            heartbeat_max_age_sec=15,
            alert_target="conductor",
            dedup_ttl_sec=0,
        ),
    )

    _check("stale heartbeat fires OOB alert", result["alerted"] is True, result)
    _check("stale heartbeat names heartbeat key", watch.notify_daemon_heartbeat_key() in result["reason"], result)
    _assert_oob_submit_sequence("stale heartbeat alert", commands, delays)


def test_healthy_daemon_no_alert() -> None:
    r = FakeRedis()
    r.set(watch.notify_daemon_heartbeat_key(), "997.000000+notify-host")

    result, commands, delays = _run_with_service(
        "active",
        lambda: watch.check_notify_daemon_liveness(
            r,
            now=1000.0,
            heartbeat_max_age_sec=15,
            alert_target="conductor",
            dedup_ttl_sec=0,
        ),
    )

    _check("healthy service and fresh heartbeat is OK", result["ok"] is True and result["alerted"] is False, result)
    _check("healthy check does not tmux-inject", not _tmux_commands(commands), commands)
    _check("healthy check does not sleep for alert submit", delays == [], delays)


def test_stale_inbox_delivery_alerts_even_when_daemon_healthy() -> None:
    r = FakeRedis()
    r.lpush(
        f"{watch.NOTIFY_KEY_PREFIX}:infra:inbox",
        json.dumps({
            "from": "conductor",
            "type": "command",
            "body": "disk-96 follow-up",
            "timestamp": 100.0,
            "msg_id": "disk-96",
        }),
    )

    result, commands, delays = _run_with_service(
        "active",
        lambda: watch.check_stuck_inbox_delivery(
            r,
            now=1000.0,
            max_age_sec=600,
            alert_target="conductor",
            dedup_ttl_sec=0,
        ),
    )

    _check("old queued inbox message fires OOB alert", result["alerted"] is True, result)
    _check("stuck handoff alert names recipient inbox", f"{watch.NOTIFY_KEY_PREFIX}:infra:inbox" in result["reason"], result)
    _assert_oob_submit_sequence("stuck handoff alert", commands, delays)


def main() -> None:
    test_stderr_logging_is_line_buffered()
    test_killed_service_alerts()
    test_stale_heartbeat_alerts()
    test_healthy_daemon_no_alert()
    test_stale_inbox_delivery_alerts_even_when_daemon_healthy()
    if FAILURES:
        raise SystemExit("\nFAILURES:\n" + "\n".join(FAILURES))
    print("\nPASS -- notify-daemon watchdog catches dead service, stale heartbeat, and stuck handoff delivery.")


if __name__ == "__main__":
    main()
