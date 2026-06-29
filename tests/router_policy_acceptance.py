#!/usr/bin/env python3
from __future__ import annotations

import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fleet_orchestrator.lane_state import LaneState  # noqa: E402
from fleet_orchestrator.router import choose_worker, should_reroute  # noqa: E402


def _state(lane_id: str, *, available: bool = True, confidence: float = 0.96, cooldown: float = 0.0) -> LaneState:
    return LaneState(
        lane_id=lane_id,
        available_now=available,
        ttl_distribution={
            "expired": 0.02,
            "lt_5m": 0.08,
            "5m_30m": 0.25,
            "gt_30m": 0.55,
            "unknown": 0.10,
        },
        cooldown_remaining=cooldown,
        confidence=confidence,
        observed_at=time.time(),
        signals={},
        source_freshness={},
    )


def _check(label: str, condition: bool, detail=None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def _lanes():
    return [
        {
            "worker_id": "local-worker",
            "competence": 0.42,
            "fidelity": 0.42,
            "cost": 0.05,
            "supports_tools": True,
        },
        {
            "worker_id": "worker-codex",
            "competence": 0.72,
            "fidelity": 0.72,
            "cost": 0.30,
            "supports_tools": True,
        },
        {
            "worker_id": "worker-grok",
            "competence": 0.84,
            "fidelity": 0.84,
            "cost": 0.55,
            "supports_tools": True,
        },
        {
            "worker_id": "worker-claude",
            "competence": 0.93,
            "fidelity": 0.93,
            "cost": 0.90,
            "supports_tools": True,
            "reserved_for": ["weekly_hmm"],
        },
    ]


def _states(*workers: str):
    return {worker: _state(worker) for worker in workers}


def main() -> int:
    lanes = _lanes()
    states = _states("local-worker", "worker-codex", "worker-grok", "worker-claude")

    routine = choose_worker(
        "Small deterministic code edit",
        {"competence_required": 0.65},
        lanes=lanes,
        lane_states=states,
    )
    _check("routine task chooses cheapest competent lane", routine.selected_worker == "worker-codex", routine.to_dict())
    _check("receipt exposes no-overkill order", routine.receipt["no_overkill_order"][0] == "worker-codex", routine.receipt)
    _check(
        "reserved premium lane is protected for routine task",
        any(item["worker_id"] == "worker-claude" and "reserved_lane_protected" in item["reasons"] for item in routine.receipt["rejected_lanes"]),
        routine.receipt,
    )

    reserved = choose_worker(
        "Weekly HMM bulk calibration run",
        {"competence_required": 0.90, "reservation_tags": ["weekly_hmm"]},
        lanes=lanes,
        lane_states=states,
    )
    _check("matching reservation may use reserved lane", reserved.selected_worker == "worker-claude", reserved.to_dict())

    sacred = choose_worker(
        "Child protection evidence audit across public records",
        {"competence_required": 0.50},
        lanes=lanes,
        lane_states=states,
    )
    _check("sacred task bypasses cheap competent lanes", sacred.selected_worker == "worker-claude", sacred.to_dict())
    _check("sacred receipt marks constitutional pin", sacred.receipt["pin"]["active"] is True, sacred.receipt)
    _check(
        "low-fidelity lanes are structurally rejected under pin",
        all(
            item["worker_id"] != "worker-codex" or "fidelity_below_sacred_trust_threshold" in item["reasons"]
            for item in sacred.receipt["rejected_lanes"]
        ),
        sacred.receipt,
    )

    closed = choose_worker(
        "Anti-trafficking victim lifeline triage",
        {"competence_required": 0.50},
        lanes=lanes,
        lane_states={**states, "worker-claude": _state("worker-claude", available=False)},
    )
    _check("sacred task fails closed when no high-fidelity viable lane exists", closed.no_route and closed.selected_worker is None, closed.to_dict())

    busy = should_reroute(
        "Continue the implementation",
        "worker-codex",
        {"competence_required": 0.65},
        lanes=lanes,
        lane_states={**states, "worker-codex": _state("worker-codex", available=False)},
    )
    _check("unavailable current lane triggers reroute", busy.reroute and busy.selected_worker == "worker-grok", busy.to_dict())

    stable = should_reroute(
        "Continue the implementation",
        "worker-codex",
        {"competence_required": 0.65},
        lanes=lanes,
        lane_states=states,
    )
    _check("healthy current lane stays put", stable.action == "stay" and stable.reroute is False, stable.to_dict())

    precompact = should_reroute(
        "PreCompact: context window is near the limit; send a handoff.",
        "worker-codex",
        {"competence_required": 0.65},
        lanes=lanes,
        lane_states=states,
    )
    _check("precompact prompt-risk triggers reroute", precompact.reroute and precompact.selected_worker == "worker-grok", precompact.to_dict())

    sacred_current = should_reroute(
        "Child protection evidence audit across public records",
        "worker-grok",
        {"competence_required": 0.50},
        lanes=lanes,
        lane_states=states,
    )
    _check("sacred current lane reroutes to top fidelity", sacred_current.reroute and sacred_current.selected_worker == "worker-claude", sacred_current.to_dict())

    print("\nPASS router policy enforces no-route pins, reservations, survival filters, and reroute advice.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
