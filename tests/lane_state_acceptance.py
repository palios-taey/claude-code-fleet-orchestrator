#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import uuid
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PREFIX = f"laneacc-{uuid.uuid4().hex[:8]}"
os.environ["NOTIFY_KEY_PREFIX"] = PREFIX

from fleet_orchestrator.lane_state import (  # noqa: E402
    LaneRef,
    UISignal,
    calibration_stream_key,
    discover_chat_lanes,
    estimate_lane,
    estimate_lanes,
    record_calibration,
    record_exit_code,
    state_key,
)


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.streams: dict[str, list[dict[str, str]]] = {}

    def get(self, key: str):
        return self.store.get(key)

    def set(self, key: str, value):
        self.store[key] = str(value)
        return True

    def delete(self, *keys: str):
        count = 0
        for key in keys:
            if key in self.store:
                count += 1
                del self.store[key]
        return count

    def scan_iter(self, match: str):
        import fnmatch

        for key in sorted(self.store):
            if fnmatch.fnmatch(key, match):
                yield key

    def xadd(self, key: str, fields: dict):
        entry_id = f"{len(self.streams.get(key, [])) + 1}-0"
        stored = {str(k): str(v) for k, v in fields.items()}
        stored["_id"] = entry_id
        self.streams.setdefault(key, []).append(stored)
        return entry_id


def _check(label: str, condition: bool, detail=None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def main() -> int:
    now = time.time()
    redis_client = FakeRedis()
    worker = "worker-codex"

    redis_client.set(state_key(worker, "idle"), "1")
    redis_client.set(state_key(worker, "last_activity"), str(now - 5))
    redis_client.set(state_key(worker, "last_tool_activity"), str(now - 6))
    fresh = estimate_lane(worker, redis_client=redis_client, now=now)
    _check("fresh session lane is available", fresh.available_now is True, fresh.to_dict())
    _check("fresh session confidence high", fresh.confidence >= 0.95, fresh.to_dict())
    _check("ttl distribution is normalized", abs(sum(fresh.ttl_distribution.values()) - 1.0) < 0.00001, fresh.ttl_distribution)

    redis_client.set(state_key(worker, "tool_running"), "1")
    busy = estimate_lane(worker, redis_client=redis_client, now=now)
    _check("tool-running lane unavailable now", busy.available_now is False, busy.to_dict())
    _check("tool-running is not classified as expired quota", busy.ttl_distribution["expired"] < 0.2, busy.ttl_distribution)
    redis_client.delete(state_key(worker, "tool_running"))

    redis_client.set(state_key(worker, "last_tool_activity"), str(now - 2000))
    redis_client.set(state_key(worker, "last_activity"), str(now - 2000))
    stale = estimate_lane(worker, redis_client=redis_client, now=now)
    _check("stale heartbeats reduce confidence", stale.confidence <= 0.25, stale.to_dict())
    _check("stale estimate increases unknown TTL mass", stale.ttl_distribution["unknown"] > fresh.ttl_distribution["unknown"], stale.ttl_distribution)

    victim = "victim-codex"
    healthy_a = "healthy-a-codex"
    healthy_b = "healthy-b-codex"
    huge_int_json = '{"task_id": ' + ("9" * 5000) + "}"
    redis_client.set(state_key(victim, "current_task"), huge_int_json)
    redis_client.set(state_key(victim, "last_outcome"), '{"outcome": ' + ("8" * 5000) + "}")
    redis_client.set(state_key(victim, "last_activity"), str(now - 15))
    redis_client.set(state_key(healthy_a, "last_activity"), str(now - 4))
    redis_client.set(state_key(healthy_b, "last_activity"), str(now - 3))
    victim_state = estimate_lane(victim, redis_client=redis_client, now=now)
    _check(
        "huge-int current_task falls back to raw instead of raising",
        "raw" in (victim_state.signals["session"]["current_task"] or {}),
        victim_state.to_dict(),
    )
    _check(
        "huge-int last_outcome falls back to raw instead of raising",
        "raw" in (victim_state.signals["session"]["last_outcome"] or {}),
        victim_state.to_dict(),
    )
    batch = estimate_lanes([healthy_a, victim, healthy_b], redis_client=redis_client, now=now)
    _check("one malformed lane does not abort estimate_lanes batch", set(batch) == {healthy_a, victim, healthy_b}, batch)
    _check("healthy lanes remain available in malformed batch", batch[healthy_a].available_now and batch[healthy_b].available_now, batch)

    chat_lane = LaneRef(lane_id="chat:claude:display:3", kind="chat", session_id="chat:claude:display:3", platform="claude", display="3")

    def ui_limit(_lane: LaneRef, observed_at: float) -> UISignal:
        return UISignal(
            observed_at=observed_at,
            available=False,
            send_button_disabled=True,
            limit_banner="Usage limit reached. Try again in 12 minutes.",
            cooldown_remaining=720,
            current_model="Sonnet",
        )

    limited = estimate_lane(chat_lane, redis_client=redis_client, now=now, ui_probe=ui_limit)
    _check("UI limit makes chat lane unavailable", limited.available_now is False, limited.to_dict())
    _check("UI cooldown is surfaced", limited.cooldown_remaining == 720, limited.to_dict())
    _check("UI limit biases TTL toward expired", limited.ttl_distribution["expired"] >= 0.65, limited.ttl_distribution)

    calibration_id = record_calibration(
        worker,
        {"source": "notify", "tool_running": False},
        "success",
        redis_client=redis_client,
        ts=now,
        ttl_remaining=1800,
        metadata={"task": "acceptance"},
    )
    stream = redis_client.streams[calibration_stream_key()]
    _check("calibration appends to Redis stream", calibration_id == "1-0" and len(stream) == 1, stream)
    _check("calibration stores structured signal JSON", json.loads(stream[0]["signal"])["source"] == "notify", stream)

    exit_id = record_exit_code(chat_lane, 137, redis_client=redis_client, ts=now)
    _check("exit code outcome is logged", exit_id == "2-0" and stream[-1]["outcome"] == "exit_error", stream)

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        user_dir = root / "systemd" / "user"
        user_dir.mkdir(parents=True)
        (user_dir / "taey-display-3.service").write_text(
            "ExecStart=/usr/bin/firefox --profile ff-profile-mira-claude https://claude.ai/new\n",
            encoding="utf-8",
        )
        (root / "systemd" / "machine.env.template").write_text(
            'TAEY_DISPLAY_4="gemini:ff-profile-mira-gemini:https://gemini.google.com/app"\n',
            encoding="utf-8",
        )
        lanes = {lane.lane_id: lane for lane in discover_chat_lanes(taeys_hands_root=root)}
        _check("service display creates chat lane", "chat:claude:display:3" in lanes, lanes)
        _check("machine env display creates chat lane", "chat:gemini:display:4" in lanes, lanes)

    print("\nPASS lane-state estimator reads passive signals and records calibration.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
