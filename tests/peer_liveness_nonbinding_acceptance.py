"""Ship-gate e2e — tool-heartbeat-based peer liveness (the codex/grok busy-loop fix).

Empirically mapped 2026-06-11 (/tmp/codex_signal_lifecycle.log): while codex/grok work,
their `last_tool_activity` HEARTBEAT refreshes per tool-call and goes monotonically stale
the instant they stop; their `idle` is useless (stuck=1) and `current_task` binds only
inconsistently. So `_peer_actively_working_task` keys on the tool-only heartbeat:
  - last_outcome terminal for this task     -> awaits gate           -> NOT working
  - current_task bound to a DIFFERENT task  -> on other work         -> NOT working
  - heartbeat FRESH (< _PEER_HEARTBEAT_STALE_SEC) + not terminal     -> WORKING (ALLOW)
  - heartbeat STALE/absent                  -> stopped/dropped/dead  -> NOT working
This subsumes PR#39 (a stopped/dead peer's heartbeat goes stale -> BLOCK) AND fixes the
CLI peers (heartbeat fresh while working, even though idle is stuck). Production oracle:
live Neo4j 10.0.0.163:7689 + Redis 127.0.0.1.
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet_orchestrator.orch_schema import (  # noqa: E402
    _state_key, _peer_actively_working_task, _PEER_HEARTBEAT_STALE_SEC,
)
from fleet_orchestrator.config import OrchConfig, get_redis_sync  # noqa: E402

CFG = OrchConfig()
_R = get_redis_sync(CFG)
_PFX = f"hbliven-ci-{uuid.uuid4().hex[:8]}"
_PEER = f"{_PFX}-sup-codex"   # a CLI peer
T = f"{_PFX}::codexwork"
_FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: str = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        _FAILURES.append(label)


def _reset(worker: str) -> None:
    for k in ("current_task", "idle", "last_activity", "last_tool_activity", "last_outcome"):
        _R.delete(_state_key(worker, k))


def _hb(worker: str, age: float) -> None:
    _R.set(_state_key(worker, "last_tool_activity"), str(time.time() - age))


def _outcome(worker: str, outcome: str, task_id: str) -> None:
    _R.set(_state_key(worker, "last_outcome"),
           json.dumps({"outcome": outcome, "details": f"{outcome.upper()} [{task_id}]"}))


def _working(worker: str) -> bool:
    return _peer_actively_working_task([worker], T, config=CFG)


def main() -> int:
    try:
        _reset(_PEER)
        # codex reality: idle STUCK=1, current_task ABSENT -- must NOT block by themselves.
        _R.set(_state_key(_PEER, "idle"), "1")

        # 1. THE FIX: fresh heartbeat + no terminal (idle stuck, current_task absent) -> WORKING
        _hb(_PEER, 5)
        _check("fresh heartbeat + no terminal (idle stuck=1, ct absent) -> WORKING (busy-loop fix)",
               _working(_PEER) is True)

        # 2. prompt-only last_activity is NOT a working signal.
        _reset(_PEER); _R.set(_state_key(_PEER, "idle"), "1")
        _R.set(_state_key(_PEER, "last_activity"), str(time.time()))
        _check("fresh prompt-only last_activity -> NOT working (daemon wake false-window)",
               _working(_PEER) is False)

        # 3. reported DONE -> awaits gate -> NOT working (even with fresh heartbeat)
        _hb(_PEER, 5)
        _outcome(_PEER, "done", T)
        _check("last_outcome=done(this task) -> NOT working (gate)", _working(_PEER) is False)

        # 4. reported ERROR -> investigate -> NOT working
        _outcome(_PEER, "error", T)
        _check("last_outcome=error(this task) -> NOT working (investigate)", _working(_PEER) is False)

        # 5. last_outcome for a DIFFERENT task + fresh heartbeat -> WORKING (still on T)
        _outcome(_PEER, "done", f"{_PFX}::othertask")
        _check("last_outcome=done(OTHER task) + fresh hb -> WORKING", _working(_PEER) is True)

        # 6. STALE heartbeat + no terminal -> stopped/dropped/dead -> NOT working
        _reset(_PEER); _R.set(_state_key(_PEER, "idle"), "1")
        _hb(_PEER, _PEER_HEARTBEAT_STALE_SEC + 60)
        _check("stale heartbeat + no terminal -> NOT working (stopped/dropped/dead, subsumes PR#39)",
               _working(_PEER) is False)

        # 7. ABSENT heartbeat -> cannot confirm working -> NOT working (fail-closed)
        _reset(_PEER)
        _check("absent heartbeat -> NOT working (fail-closed)", _working(_PEER) is False)

        # 8. current_task bound to a DIFFERENT task + fresh hb -> NOT working THIS one
        _hb(_PEER, 2)
        _R.set(_state_key(_PEER, "current_task"), json.dumps({"task_id": f"{_PFX}::other"}))
        _check("current_task bound to DIFFERENT task -> NOT working THIS one", _working(_PEER) is False)

        # 9. current_task bound to THIS task + fresh hb -> WORKING (bonus binding precision)
        _R.set(_state_key(_PEER, "current_task"), json.dumps({"task_id": T}))
        _check("current_task bound to THIS task + fresh hb -> WORKING", _working(_PEER) is True)
    finally:
        _reset(_PEER)
    if _FAILURES:
        print(f"\nFAIL — {len(_FAILURES)}: {_FAILURES}")
        return 1
    print("\nPASS — tool-heartbeat liveness: fresh tool-hb+not-terminal=working; prompt-only activity ignored; stale-hb subsumes PR#39.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
