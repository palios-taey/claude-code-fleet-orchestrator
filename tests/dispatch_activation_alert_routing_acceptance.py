#!/usr/bin/env python3
"""Acceptance: dispatch activation/stuck alerts do not route to a host identity."""
from __future__ import annotations

from fnmatch import fnmatch
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
        self.expiry: dict[str, int] = {}

    def set(self, key: str, value: object, ex: int | None = None) -> bool:
        self.store[key] = value
        if ex is None:
            self.expiry.pop(key, None)
        else:
            self.expiry[key] = ex
        return True

    def lpush(self, key: str, value: object) -> int:
        self.store.setdefault(key, [])
        self.store[key].insert(0, value)
        return len(self.store[key])

    def get(self, key: str) -> object:
        return self.store.get(key)

    def exists(self, key: str) -> int:
        return 1 if key in self.store else 0

    def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            self.expiry.pop(key, None)
            if key in self.store:
                del self.store[key]
                deleted += 1
        return deleted

    def srem(self, key: str, *members: object) -> int:
        existing = self.store.get(key)
        if not isinstance(existing, set):
            return 0
        removed = 0
        for member in members:
            if member in existing:
                existing.remove(member)
                removed += 1
        return removed

    def scan_iter(self, match: str | None = None, count: int | None = None):
        del count
        for key in list(self.store):
            if match is None or fnmatch(key, match):
                yield key

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


def _run_wedged_composer_alert(composer_payload: dict[str, object]) -> list[tuple[str, str]]:
    r = FakeRedis()
    worker = "conductor-codex"
    task_id = "dispatch-activation-alert-task"
    msg_id = "activation-failed-msg"
    r.set(watch.notify_key("mira:inbox", prefix=watch.NOTIFY_KEY_PREFIX), "stale dead-letter")
    r.set(watch.state_key(worker, "parent"), "mira")
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
    r.set(watch.state_key(worker, "composer_occupancy"), json.dumps(composer_payload))
    for record_msg_id in (msg_id, f"{msg_id}-second"):
        r.set(
            f"{watch.NOTIFY_KEY_PREFIX}:handoff:mira:{record_msg_id}",
            json.dumps({
                "kind": "explicit_handoff",
                "dispatcher_session_id": "mira",
                "target_session_id": worker,
                "dispatcher_task_id": task_id,
                "msg_id": record_msg_id,
                "activation_state": "failed",
                "activation_failed_at": 990,
            }),
        )
    sent: list[tuple[str, str]] = []

    def fake_send(_r, target: str, body: str, **_kwargs) -> bool:
        sent.append((target, body))
        return True

    with mock.patch.object(watch, "_local_hostname", return_value="mira"), \
            mock.patch.object(watch, "_local_tmux_sessions", return_value={"conductor"}), \
            mock.patch.object(watch, "_load_task_state", return_value={"status": "in_progress"}), \
            mock.patch.object(watch, "_send_wake", side_effect=fake_send):
        watch._process_wedged_composer_liveness(
            r,
            dedup_ttl_sec=60,
            max_age_sec=120,
        )
        watch._process_wedged_composer_liveness(
            r,
            dedup_ttl_sec=60,
            max_age_sec=120,
        )
    return sent


def _run_wedged_composer_resolution_recurrence() -> list[tuple[str, str]]:
    r = FakeRedis()
    worker = "conductor-codex"
    task_id = "dispatch-activation-alert-task"
    msg_id = "activation-failed-msg"
    r.set(watch.state_key(worker, "parent"), "mira")
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
    r.set(
        f"{watch.NOTIFY_KEY_PREFIX}:handoff:mira:{msg_id}",
        json.dumps({
            "kind": "explicit_handoff",
            "dispatcher_session_id": "mira",
            "target_session_id": worker,
            "dispatcher_task_id": task_id,
            "msg_id": msg_id,
            "activation_state": "failed",
            "activation_failed_at": 990,
        }),
    )
    sent: list[tuple[str, str]] = []

    def fake_send(_r, target: str, body: str, **_kwargs) -> bool:
        sent.append((target, body))
        return True

    with mock.patch.object(watch, "_local_hostname", return_value="mira"), \
            mock.patch.object(watch, "_local_tmux_sessions", return_value={"conductor"}), \
            mock.patch.object(watch, "_load_task_state", return_value={"status": "in_progress"}), \
            mock.patch.object(watch, "_send_wake", side_effect=fake_send):
        r.set(watch.state_key(worker, "composer_occupancy"), json.dumps({
            "occupied": True,
            "observed_at": 999,
            "machine": "notify-host",
            "excerpt": "Click Post on LinkedIn",
        }))
        watch._process_wedged_composer_liveness(
            r,
            dedup_ttl_sec=60,
            max_age_sec=120,
        )
        r.set(watch.state_key(worker, "composer_occupancy"), json.dumps({
            "occupied": True,
            "observed_at": 1000,
            "machine": "notify-host",
            "excerpt": "Click Post on LinkedIn",
        }))
        watch._process_wedged_composer_liveness(
            r,
            dedup_ttl_sec=60,
            max_age_sec=120,
        )
        r.set(watch.state_key(worker, "composer_occupancy"), json.dumps({
            "occupied": False,
            "observed_at": 1000,
            "machine": "notify-host",
        }))
        watch._process_wedged_composer_liveness(
            r,
            dedup_ttl_sec=60,
            max_age_sec=120,
        )
        r.set(watch.state_key(worker, "composer_occupancy"), json.dumps({
            "occupied": True,
            "observed_at": 1000,
            "machine": "notify-host",
            "excerpt": "Click Post on LinkedIn",
        }))
        watch._process_wedged_composer_liveness(
            r,
            dedup_ttl_sec=60,
            max_age_sec=120,
        )
    return sent


def _run_wedged_composer_terminal_task() -> tuple[list[tuple[str, str]], bool]:
    r = FakeRedis()
    worker = "conductor-codex"
    task_id = "dispatch-activation-alert-task"
    msg_id = "activation-failed-msg"
    key = f"{watch.NOTIFY_KEY_PREFIX}:handoff:mira:{msg_id}"
    r.set(watch.state_key(worker, "parent"), "mira")
    r.set(watch.state_key(worker, "composer_occupancy"), json.dumps({
        "occupied": True,
        "observed_at": 999,
        "machine": "notify-host",
        "excerpt": "Click Post on LinkedIn",
    }))
    r.set(
        key,
        json.dumps({
            "kind": "explicit_handoff",
            "dispatcher_session_id": "mira",
            "target_session_id": worker,
            "dispatcher_task_id": task_id,
            "msg_id": msg_id,
            "activation_state": "failed",
            "activation_failed_at": 990,
        }),
    )
    sent: list[tuple[str, str]] = []

    def fake_send(_r, target: str, body: str, **_kwargs) -> bool:
        sent.append((target, body))
        return True

    with mock.patch.object(watch, "_local_hostname", return_value="mira"), \
            mock.patch.object(watch, "_local_tmux_sessions", return_value={"conductor"}), \
            mock.patch.object(watch, "_load_task_state", return_value={"status": "completed"}), \
            mock.patch.object(watch, "_send_wake", side_effect=fake_send):
        watch._process_wedged_composer_liveness(
            r,
            dedup_ttl_sec=60,
            max_age_sec=120,
        )
    return sent, key not in r.store


def _run_changing_composer_no_alert() -> list[tuple[str, str]]:
    r = FakeRedis()
    worker = "conductor-codex"
    task_id = "dispatch-activation-alert-task"
    msg_id = "activation-failed-msg"
    r.set(watch.state_key(worker, "parent"), "mira")
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
    r.set(
        f"{watch.NOTIFY_KEY_PREFIX}:handoff:mira:{msg_id}",
        json.dumps({
            "kind": "explicit_handoff",
            "dispatcher_session_id": "mira",
            "target_session_id": worker,
            "dispatcher_task_id": task_id,
            "msg_id": msg_id,
            "activation_state": "failed",
            "activation_failed_at": 990,
        }),
    )
    sent: list[tuple[str, str]] = []

    def fake_send(_r, target: str, body: str, **_kwargs) -> bool:
        sent.append((target, body))
        return True

    with mock.patch.object(watch, "_local_hostname", return_value="mira"), \
            mock.patch.object(watch, "_local_tmux_sessions", return_value={"conductor"}), \
            mock.patch.object(watch, "_load_task_state", return_value={"status": "in_progress"}), \
            mock.patch.object(watch, "_send_wake", side_effect=fake_send):
        for index, excerpt in enumerate(("typing first thought", "typing revised thought")):
            r.set(watch.state_key(worker, "composer_occupancy"), json.dumps({
                "occupied": True,
                "observed_at": 999 + index,
                "machine": "notify-host",
                "excerpt": excerpt,
            }))
            watch._process_wedged_composer_liveness(
                r,
                dedup_ttl_sec=60,
                max_age_sec=120,
            )
    return sent


def _run_placeholder_composer_no_alert() -> list[tuple[str, str]]:
    return _run_wedged_composer_alert({
        "occupied": True,
        "observed_at": 999,
        "machine": "notify-host",
        "content_fingerprint": "placeholder-fingerprint",
        "excerpt": "use /skills to list available skills",
    })


def _run_wedged_composer_candidate_ttl_floor() -> int | None:
    r = FakeRedis()
    worker = "conductor-codex"
    with mock.patch.dict(
        watch.os.environ,
        {"ORCH_WEDGED_COMPOSER_STABILITY_WINDOW_SEC": "5"},
    ):
        matched = watch._composer_candidate_matches(
            r,
            worker,
            "stable-fingerprint",
            current_time=1000.0,
            ttl_sec=2,
        )
    if matched:
        return None
    return r.expiry.get(watch._wedged_composer_candidate_key(worker))


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
    composer_sent = _run_wedged_composer_alert({
        "occupied": True,
        "observed_at": 999,
        "machine": "notify-host",
        "excerpt": "Click Post on LinkedIn",
    })
    _check(
        "wedged composer persistent failed handoffs alert once per active transition",
        len(composer_sent) == 1 and composer_sent[0][0] == "conductor",
        composer_sent,
    )
    _check(
        "wedged composer alert names failed activation and non-empty composer",
        bool(composer_sent)
        and "[WEDGED_COMPOSER]" in composer_sent[0][1]
        and "dispatch_activation_failed" in composer_sent[0][1]
        and "composer is still non-empty" in composer_sent[0][1],
        composer_sent,
    )
    _check(
        "wedged composer alert omits raw composer text",
        bool(composer_sent)
        and "composer_excerpt" not in composer_sent[0][1]
        and "Click Post on LinkedIn" not in composer_sent[0][1],
        composer_sent,
    )
    composer_empty_sent = _run_wedged_composer_alert({
        "occupied": False,
        "observed_at": 999,
        "machine": "notify-host",
    })
    _check(
        "activation failure without occupied composer does not alert",
        composer_empty_sent == [],
        composer_empty_sent,
    )
    composer_recurrence_sent = _run_wedged_composer_resolution_recurrence()
    _check(
        "wedged composer rearm survives empty occupancy flap",
        len(composer_recurrence_sent) == 1,
        composer_recurrence_sent,
    )
    terminal_sent, terminal_record_deleted = _run_wedged_composer_terminal_task()
    _check(
        "terminal failed-activation handoff record does not alert",
        terminal_sent == [],
        terminal_sent,
    )
    _check(
        "terminal failed-activation handoff record is deleted",
        terminal_record_deleted,
        terminal_record_deleted,
    )
    changing_sent = _run_changing_composer_no_alert()
    _check(
        "changing composer content does not alert",
        changing_sent == [],
        changing_sent,
    )
    placeholder_sent = _run_placeholder_composer_no_alert()
    _check(
        "ignored composer placeholder does not alert",
        placeholder_sent == [],
        placeholder_sent,
    )
    candidate_ttl = _run_wedged_composer_candidate_ttl_floor()
    _check(
        "wedged composer candidate ttl keeps stability floor",
        candidate_ttl == watch.DEFAULT_WEDGED_COMPOSER_STABILITY_WINDOW_SEC,
        candidate_ttl,
    )
    if FAILURES:
        print("\nFAIL -- " + "; ".join(FAILURES))
        return 1
    print("\nPASS -- dispatch activation alert routing ignores phantom host target.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
