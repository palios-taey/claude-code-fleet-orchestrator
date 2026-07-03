#!/usr/bin/env python3
"""Acceptance: dispatch activation/stuck alerts do not route to a host identity."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fleet_orchestrator import cli_orch_watch as watch  # noqa: E402


FAILURES: list[str] = []


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, object] = {}

    def set(self, key: str, value: object, ex: int | None = None) -> bool:
        del ex
        self.store[key] = value
        return True

    def get(self, key: str) -> object:
        return self.store.get(key)

    def exists(self, key: str) -> int:
        return 1 if key in self.store else 0

    def time(self) -> tuple[int, int]:
        return (1000, 0)


def _check(label: str, condition: bool, detail: object = "") -> None:
    print(("PASS " if condition else "FAIL ") + label)
    if not condition:
        FAILURES.append(f"{label}: {detail}")


def _run_activation_alert() -> list[tuple[str, str]]:
    r = FakeRedis()
    worker = "conductor-codex"
    task_id = "dispatch-activation-alert-task"
    r.set(watch.notify_key("mira:inbox", prefix=watch.NOTIFY_KEY_PREFIX), "stale dead-letter")
    r.set(watch.state_key(worker, "parent"), "mira")
    r.set(watch.state_key(worker, "idle"), "1")
    r.set(watch.state_key(worker, "last_activity"), "900")
    r.set(
        watch.state_key(worker, "current_task"),
        json.dumps({
            "task_id": task_id,
            "description": "worker should have activated",
            "supervisor": "mira",
            "dispatcher": "mira",
            "started_at": 900,
        }),
    )
    sent: list[tuple[str, str]] = []

    def fake_send(_r, target: str, body: str, **_kwargs) -> bool:
        sent.append((target, body))
        return True

    with mock.patch.object(watch, "_local_hostname", return_value="mira"), \
            mock.patch.object(watch, "_local_tmux_sessions", return_value={"conductor"}), \
            mock.patch.object(watch, "_load_task_state", return_value=None), \
            mock.patch.object(watch, "_target_stop_decision_allows_stop", return_value=False), \
            mock.patch.object(watch, "_build_peer_idle_body", return_value="[dispatch_activation_failed] worker did not activate"), \
            mock.patch.object(watch, "_send_wake", side_effect=fake_send):
        watch.investigate(
            r,
            worker,
            "current_task_set",
            stuck_threshold_sec=1,
            dedup_ttl_sec=60,
            readiness_checker=None,
        )
    return sent


def main() -> int:
    sent = _run_activation_alert()
    _check(
        "activation failure alert reaches dispatching supervisor from worker session",
        len(sent) == 1 and sent[0][0] == "conductor",
        sent,
    )
    _check(
        "phantom host identity is not used as alert target",
        all(target != "mira" for target, _body in sent),
        sent,
    )
    if FAILURES:
        print("\nFAIL -- " + "; ".join(FAILURES))
        return 1
    print("\nPASS -- dispatch activation alert routing ignores phantom host target.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
