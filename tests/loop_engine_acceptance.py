"""Ship-gate acceptance for the additive orchestrator loop engine."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from lib.loop_engine import (  # noqa: E402
    ArtifactNotObservedError,
    ArtifactStore,
    Loop,
    LoopDeclarationError,
    adversarial_meetable_cases,
    advance_loop_step,
    declare_loop,
)


def _check(label: str, condition: bool, detail: Any = None) -> None:
    if not condition:
        raise AssertionError(f"{label} failed: {detail!r}")
    print(f"ok - {label}")


def _raises(label: str, fn: Callable[[], Any], exc_type: type[BaseException]) -> None:
    try:
        fn()
    except exc_type:
        print(f"ok - {label}")
        return
    raise AssertionError(f"{label} did not raise {exc_type.__name__}")


def _base_loop(artifact_path: str = "/tmp/orch-loop-artifact") -> dict[str, Any]:
    return {
        "id": "acceptance-loop",
        "owner": "conductor",
        "trigger": {"kind": "clock", "clock_signal": "orch-watch-tick"},
        "step_bundle": [
            {
                "step": "observe",
                "requires_artifact": {"kind": "file", "path": artifact_path},
                "writes_state": [
                    {"var": "cycle_state.counters.done", "mode": "increment", "amount": 1},
                ],
            }
        ],
        "cycle_state": {"counters": {"done": 0}},
        "swap_slots": {},
        "stop_condition": {"var": "cycle_state.counters.done", "op": ">=", "value": 1},
    }


def _loop_from_adversarial(name: str) -> dict[str, Any]:
    cases = {
        "external-approval-no-timeout": {
            "stop_condition": {"var": "external.approval", "op": "==", "value": True},
            "step_bundle": [{"step": "gate", "human_gate": True, "writes_state": []}],
            "cycle_state": {},
        },
        "increment-vs-floating": {
            "stop_condition": {
                "var": "cycle_state.counters.surface.used",
                "op": ">=",
                "other_var": "cycle_state.levers.dynamic_threshold",
            },
            "step_bundle": [
                {"step": "post", "writes_state": [{"var": "cycle_state.counters.surface.used", "mode": "increment"}]},
                {"step": "tune", "writes_state": [{"var": "cycle_state.levers.dynamic_threshold", "mode": "external"}]},
            ],
            "cycle_state": {"counters": {"surface": {"used": 0}}, "levers": {"dynamic_threshold": 3}},
        },
        "var-no-step-writes": {
            "stop_condition": {"var": "cycle_state.counters.surface.used", "op": ">=", "value": 3},
            "step_bundle": [{"step": "observe", "writes_state": []}],
            "cycle_state": {"counters": {"surface": {"used": 0}}},
        },
    }
    raw = dict(cases[name])
    raw.update(
        {
            "id": f"bad-{name}",
            "owner": "conductor",
            "trigger": {"kind": "clock", "clock_signal": "orch-watch-tick"},
            "swap_slots": {},
        }
    )
    return raw


def main() -> int:
    os.environ.pop("ORCH_LOOPS_ENABLED", None)
    _check("ORCH_LOOPS_ENABLED defaults off", declare_loop(_base_loop()) == {"ok": True, "enabled": False})

    adversarial = adversarial_meetable_cases()
    for name in ("external-approval-no-timeout", "increment-vs-floating", "var-no-step-writes"):
        _check(f"meetable rejects {name}", adversarial.get(name) is False, adversarial)
        _raises(f"declare rejects {name}", lambda name=name: Loop.declare(_loop_from_adversarial(name)), LoopDeclarationError)

    bad_clock = _base_loop()
    bad_clock["trigger"] = {"kind": "clock", "clock_signal": "worker-sleep"}
    _raises("clock trigger rejects worker self-timing", lambda: Loop.declare(bad_clock), LoopDeclarationError)

    with tempfile.TemporaryDirectory() as tmp:
        artifact = Path(tmp) / "validated-artifact.txt"
        raw = _base_loop(str(artifact))
        loop = Loop.declare(raw)
        _raises(
            "advance fails closed when artifact absent",
            lambda: loop.advance_step("observe", ArtifactStore()),
            ArtifactNotObservedError,
        )
        _check("absent artifact did not mutate counter", loop.cycle_state["counters"]["done"] == 0, loop.cycle_state)

        artifact.write_text("observed\n", encoding="utf-8")
        sent: list[tuple[str, str, str]] = []
        os.environ["ORCH_LOOPS_ENABLED"] = "1"
        result = advance_loop_step(
            loop,
            "observe",
            artifact_store=ArtifactStore(),
            wake_target="conductor",
            notify_sender=lambda target, message, typ: sent.append((target, message, typ)),
        )
        _check("meetable loop advances on validated artifact", result["loop"]["cycle_state"]["counters"]["done"] == 1, result)
        _check("advance wake routes through notification sender", sent == [("conductor", "LOOP ADVANCE [acceptance-loop]: step=observe", "command")], sent)
        _check("should_stop true at threshold", result["should_stop"] is True, result)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
