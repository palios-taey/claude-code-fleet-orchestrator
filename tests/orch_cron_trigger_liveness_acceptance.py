"""Acceptance test: orch-cron project trigger verifies session liveness and respawns."""
from __future__ import annotations

import os
import sys
import uuid
import subprocess
from datetime import datetime
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet_orchestrator.cli_orch_cron import _fire_project_trigger
from fleet_orchestrator.orch_schema import init_schema, get_neo4j_driver
from fleet_orchestrator.config import OrchConfig, get_redis_sync

CFG = OrchConfig()
_PFX = f"cron-liveness-ci-{uuid.uuid4().hex[:8]}"
_FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: str = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        _FAILURES.append(label)


def _cleanup() -> None:
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as s:
        s.run("MATCH (n) WHERE n.id STARTS WITH $p DETACH DELETE n", p=_PFX)


def main() -> int:
    init_schema(config=CFG)
    _cleanup()
    r = get_redis_sync(CFG)
    
    sess_id = f"{_PFX}-sess"
    
    try:
        from fleet_orchestrator.orch_schema import create_project, create_phase, create_task
        create_project(project_id=_PFX, name=_PFX, config=CFG)
        create_phase(phase_id=f"{_PFX}::phase", project_id=_PFX, name="P1", order=1, config=CFG)
        create_task(task_id=f"{_PFX}::t1", phase_id=f"{_PFX}::phase", description="Task", owner=sess_id, config=CFG)

        trig = {
            "id": f"{_PFX}-trig",
            "project": _PFX,
            "session": sess_id,
            "mode": "reset"
        }
        
        now = datetime.now()

        # Let's use a side_effect to control subprocess.run
        def mock_subprocess_run(cmd, *args, **kwargs):
            import subprocess
            if cmd[0] == "peer-respawn.sh":
                if "mock_respawn_fail" in os.environ:
                    return subprocess.CompletedProcess(args=cmd, returncode=1, stdout="", stderr="")
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")
            else:
                return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

        with mock.patch("fleet_orchestrator.cli_orch_watch._local_tmux_sessions", return_value={"other-session"}), \
             mock.patch("subprocess.run", side_effect=mock_subprocess_run) as mock_run:
            
            # 1. Mock respawn failing
            os.environ["mock_respawn_fail"] = "1"
            result = _fire_project_trigger(r, trig.copy(), now)
            _check("Trigger fails if respawn fails", result == "failed:session_dead_respawn_failed", f"Got: {result}")
            mock_run.assert_any_call(["peer-respawn.sh", sess_id], capture_output=True, text=True, check=False)
            
            # 2. Mock respawn succeeding
            del os.environ["mock_respawn_fail"]
            import time
            time.sleep(0.1)
            now2 = datetime.now()
            
            # Mock dispatch itself so we don't try to call the missing notify hooks
            
            trig2 = trig.copy(); trig2["id"] = "different_trig_id"; result2 = _fire_project_trigger(r, trig2, now2)
            
            _check("Trigger dispatches if respawn succeeds", result2 == "dispatched", f"Got: {result2}")
            mock_run.assert_any_call(["peer-respawn.sh", sess_id], capture_output=True, text=True, check=False)

    finally:
        _cleanup()
        
    if _FAILURES:
        print(f"FAIL: {len(_FAILURES)} assertion(s) failed: {_FAILURES}")
        return 1
        
    print("PASS: _fire_project_trigger verifies session liveness and respawns as expected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
