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


def _run_with_tmux(callback, *, sessions: tuple[str, ...] = (), panes: dict[str, str] | None = None):
    commands: list[list[str]] = []
    panes = panes or {}

    def fake_run(cmd, **kwargs):
        del kwargs
        command = [str(part) for part in cmd]
        commands.append(command)
        if command == ["tmux", "list-sessions", "-F", "#{session_name}"]:
            return SimpleNamespace(returncode=0, stdout="\n".join(sessions) + ("\n" if sessions else ""), stderr="")
        if command[:4] == ["tmux", "capture-pane", "-p", "-t"]:
            session = command[4]
            return SimpleNamespace(returncode=0, stdout=panes.get(session, ""), stderr="")
        if command[:1] == ["tmux"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if command[:1] == ["notify-send"]:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        raise AssertionError(f"unexpected subprocess command: {command}")

    with mock.patch.object(watch.subprocess, "run", side_effect=fake_run):
        with mock.patch.object(watch.shutil, "which", return_value="/usr/bin/notify-send"):
            result = callback()
    return result, commands


def _tmux_commands(commands: list[list[str]]) -> list[list[str]]:
    return [cmd for cmd in commands if cmd[:1] == ["tmux"]]


def _notify_send_commands(commands: list[list[str]]) -> list[list[str]]:
    return [cmd for cmd in commands if cmd[:1] == ["notify-send"]]


def _submit_commands(commands: list[list[str]]) -> list[list[str]]:
    return [
        cmd for cmd in _tmux_commands(commands)
        if cmd[:4] == ["tmux", "send-keys", "-t"]
    ]


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


def test_stale_inbox_delivery_self_remediates_usage_limit_idle() -> None:
    r = FakeRedis()
    r.lpush(
        f"{watch.NOTIFY_KEY_PREFIX}:gatekeeper:inbox",
        json.dumps({
            "from": "conductor",
            "type": "command",
            "body": "please review",
            "timestamp": 100.0,
            "msg_id": "review-17",
        }),
    )

    result, commands = _run_with_tmux(
        lambda: watch.check_stuck_inbox_delivery(
            r,
            now=1000.0,
            max_age_sec=600,
            alert_target="conductor",
            dedup_ttl_sec=0,
        ),
        sessions=("gatekeeper",),
        panes={
            "gatekeeper": "\n".join([
                "working notes",
                "You've hit your session limit. It resets later today.",
            ])
        },
    )

    _check("stranded stale inbox is remediated", result["remediated"] is True, result)
    _check("remediation sets idle flag", r.get(watch.state_key("gatekeeper", "idle")) == "1", r.store)
    _check("remediation does not alert conductor inbox",
           r.lrange(f"{watch.NOTIFY_KEY_PREFIX}:conductor:inbox", 0, -1) == [],
           r.store)
    _check("remediation does not tmux-submit an alert", _submit_commands(commands) == [], commands)
    _check("remediation does not desktop alert", _notify_send_commands(commands) == [], commands)


def test_stale_inbox_delivery_alerts_conductor_once_without_desktop() -> None:
    r = FakeRedis()
    inbox_key = f"{watch.NOTIFY_KEY_PREFIX}:infra:inbox"
    payload = json.dumps({
        "from": "conductor",
        "type": "command",
        "body": "disk-96 follow-up",
        "timestamp": 100.0,
        "msg_id": "disk-96",
    })
    r.lpush(inbox_key, payload)

    first, first_commands = _run_with_tmux(
        lambda: watch.check_stuck_inbox_delivery(
            r,
            now=1000.0,
            max_age_sec=600,
            alert_target="conductor",
            dedup_ttl_sec=0,
        ),
        sessions=("infra",),
        panes={"infra": "Claude Code ready\n$"},
    )
    second, second_commands = _run_with_tmux(
        lambda: watch.check_stuck_inbox_delivery(
            r,
            now=1001.0,
            max_age_sec=600,
            alert_target="conductor",
            dedup_ttl_sec=0,
        ),
        sessions=("infra",),
        panes={"infra": "Claude Code ready\n$"},
    )

    conductor_alerts = r.lrange(f"{watch.NOTIFY_KEY_PREFIX}:conductor:inbox", 0, -1)
    _check("unhealable stuck inbox alerts conductor", first["alerted"] is True, first)
    _check("stuck handoff alert names recipient inbox", inbox_key in first["reason"], first)
    _check("same stuck message is deduped", second.get("deduped") is True and second["alerted"] is False, second)
    _check("stuck handoff creates exactly one conductor alert", len(conductor_alerts) == 1, conductor_alerts)
    _check("stuck handoff conductor alert is explicit",
           "CRITICAL NOTIFY DELIVERY SLO FAILURE" in json.loads(conductor_alerts[0])["body"],
           conductor_alerts)
    _check("stuck handoff does not tmux-submit an alert",
           _submit_commands(first_commands + second_commands) == [],
           first_commands + second_commands)
    _check("stuck handoff does not desktop alert",
           _notify_send_commands(first_commands + second_commands) == [],
           first_commands + second_commands)

    r.delete(inbox_key)
    _run_with_tmux(
        lambda: watch.check_stuck_inbox_delivery(
            r,
            now=1002.0,
            max_age_sec=600,
            alert_target="conductor",
            dedup_ttl_sec=0,
        ),
        sessions=("infra",),
        panes={"infra": "Claude Code ready\n$"},
    )
    r.lpush(inbox_key, payload)
    third, _third_commands = _run_with_tmux(
        lambda: watch.check_stuck_inbox_delivery(
            r,
            now=1003.0,
            max_age_sec=600,
            alert_target="conductor",
            dedup_ttl_sec=0,
        ),
        sessions=("infra",),
        panes={"infra": "Claude Code ready\n$"},
    )
    _check("draining inbox clears incident dedup", third["alerted"] is True, third)
    _check("new incident after drain alerts once more",
           len(r.lrange(f"{watch.NOTIFY_KEY_PREFIX}:conductor:inbox", 0, -1)) == 2,
           r.store)


def main() -> None:
    test_stderr_logging_is_line_buffered()
    test_killed_service_alerts()
    test_stale_heartbeat_alerts()
    test_healthy_daemon_no_alert()
    test_stale_inbox_delivery_self_remediates_usage_limit_idle()
    test_stale_inbox_delivery_alerts_conductor_once_without_desktop()
    if FAILURES:
        raise SystemExit("\nFAILURES:\n" + "\n".join(FAILURES))
    print("\nPASS -- notify-daemon watchdog catches dead service, stale heartbeat, and stuck handoff delivery.")


if __name__ == "__main__":
    main()
