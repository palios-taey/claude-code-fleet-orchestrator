#!/usr/bin/env python3
"""Acceptance: record_outcome notifies while its live binding still exists.

The worker-originated taey-notify gate denies response_ready after unbind by
checking the sender's current_task at enqueue time. record_outcome must
therefore emit the supervisor response while its captured binding is still live,
then clear/revert terminal state.

This is hermetic: fake Redis, fake notify sink, no Neo4j, no real GitHub, no live
Redis mutation.
"""
from __future__ import annotations

import json
import os
import sys
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import fleet_orchestrator.current_task_binding as current_task_binding  # noqa: E402
import fleet_orchestrator.dispatch as dispatch_module  # noqa: E402
import fleet_orchestrator.worker_liveness as worker_liveness  # noqa: E402
from fleet_orchestrator.dispatch import record_outcome  # noqa: E402
from fleet_orchestrator.notify_state import state_key  # noqa: E402


FAILURES: list[str] = []


def _check(label: str, condition: bool, detail: object = "") -> None:
    print(("PASS " if condition else "FAIL ") + label + ("" if condition else f" -> {detail}"))
    if not condition:
        FAILURES.append(label)


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key: str):
        return self.store.get(key)

    def set(self, key: str, value: str) -> None:
        self.store[key] = value

    def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            if key in self.store:
                del self.store[key]
                deleted += 1
        return deleted

    def pipeline(self):
        return FakePipeline(self)


class FakePipeline:
    def __init__(self, redis_client: FakeRedis) -> None:
        self.redis = redis_client
        self.pending: list[tuple[str, str]] = []

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        self.pending.clear()

    def watch(self, *_keys: str) -> None:
        return None

    def unwatch(self) -> None:
        self.pending.clear()

    def get(self, key: str):
        return self.redis.get(key)

    def multi(self) -> None:
        self.pending.clear()

    def set(self, key: str, value: str) -> None:
        self.pending.append((key, value))

    def execute(self):
        for key, value in self.pending:
            self.redis.set(key, value)
        self.pending.clear()
        return [True]


def main() -> int:
    worker = "worker-grok"
    supervisor = "taey-ed-codex"
    redis_client = FakeRedis()
    default_redis = redis_client
    notify_observations: list[dict[str, object]] = []
    clear_observations: list[dict[str, object]] = []
    reverted: list[str] = []
    liveness_cleared: list[str] = []

    def fake_notify_run(args, **_kwargs):
        key = state_key(worker, "current_task")
        notify_observations.append(
            {
                "args": list(args),
                "current_task_present": bool(redis_client.get(key)),
            }
        )
        if not redis_client.get(key):
            return SimpleNamespace(returncode=1, stdout="", stderr="SAFETY DENY: no live current_task binding")
        return SimpleNamespace(returncode=0, stdout="OK", stderr="")

    def fake_clear_matching_current_task(clear_worker, task_id, *, redis_client=None, **_kwargs):
        r = redis_client or default_redis
        key = state_key(clear_worker, "current_task")
        clear_observations.append(
            {
                "task_id": task_id,
                "current_task_present": bool(r.get(key)),
            }
        )
        r.delete(key)
        return True

    def fake_revert(worker_arg: str, task_id: str) -> None:
        reverted.append(f"{worker_arg}:{task_id}")

    def fake_clear_liveness(task_id: str) -> None:
        liveness_cleared.append(task_id)

    def fake_write_receipt(_redis, _worker, _task_id, _payload) -> None:
        return None

    with mock.patch.object(dispatch_module, "_redis_connect", return_value=redis_client), \
         mock.patch.object(dispatch_module, "_append_worker_outcome_causal_event", return_value="event:test"), \
         mock.patch.object(dispatch_module, "_revert_outcome_claim", side_effect=fake_revert), \
         mock.patch.object(dispatch_module, "_write_completion_receipt", side_effect=fake_write_receipt), \
         mock.patch.object(dispatch_module, "notify_cli", return_value="taey-notify"), \
         mock.patch.object(dispatch_module.subprocess, "run", side_effect=fake_notify_run), \
         mock.patch.object(current_task_binding, "clear_matching_current_task", side_effect=fake_clear_matching_current_task), \
         mock.patch.object(worker_liveness, "clear_worker_task_liveness", side_effect=fake_clear_liveness):
        for outcome in ("done", "error", "interrupted"):
            task_id = f"task-f396305d-{outcome}"
            key = state_key(worker, "current_task")
            redis_client.set(
                key,
                json.dumps(
                    {
                        "task_id": task_id,
                        "description": f"{outcome} order fixture",
                        "supervisor": supervisor,
                        "started_at": 123.0,
                    }
                ),
            )
            notify_observations.clear()
            clear_observations.clear()
            record_outcome(worker, outcome, f"{outcome} gate order")

            _check(f"{outcome}: response_ready emitted once", len(notify_observations) == 1, notify_observations)
            _check(
                f"{outcome}: response_ready saw live current_task",
                bool(notify_observations[0].get("current_task_present")) if notify_observations else False,
                notify_observations,
            )
            _check(f"{outcome}: clear ran after notify", len(clear_observations) == 1, clear_observations)
            _check(
                f"{outcome}: clear still saw live current_task",
                bool(clear_observations[0].get("current_task_present")) if clear_observations else False,
                clear_observations,
            )
            _check(f"{outcome}: current_task cleared after outcome", redis_client.get(key) is None, redis_client.get(key))

    _check("error/interrupted reverted graph claim", len(reverted) == 2, reverted)
    _check("error/interrupted cleared liveness", len(liveness_cleared) == 2, liveness_cleared)

    if FAILURES:
        print(f"FAIL: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("PASS record_outcome_notify_gate_order_acceptance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
