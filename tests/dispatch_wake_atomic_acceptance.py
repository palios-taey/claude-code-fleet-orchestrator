"""Ship-gate e2e — a failed wake delivery must NOT leave a phantom live resolver.

dispatch() claims the OrchTask (status=in_progress) and binds Redis current_task BEFORE
the wake (taey-notify) is delivered. If the wake fails, the task must revert to pending
(ready) and the binding must clear -- otherwise it lingers as a _LIVE_RESOLVER_STATUSES
member with nothing working it, and a supervisor blocked_on it stops (GAIA dispatched-wake
gap). A SUCCESSFUL wake must leave it in_progress (no regression).

Env: ORCH_NEO4J_URI, ORCH_NEO4J_DB, ORCH_REDIS_HOST/PORT, ORCH_DASHBOARD_URL,
ORCH_NOTIFY_LIB_ROOT (dispatch binds via the fleet-notify identity module).
"""
from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

_FAILURES: list[str] = []
_PFX = f"wakeatomic-{uuid.uuid4().hex[:8]}"
_WORKER = f"{_PFX}-codex"


def _check(label: str, cond: bool, extra: str = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        _FAILURES.append(label)


def _stub(tmp: str, name: str, rc: int) -> str:
    p = os.path.join(tmp, name)
    with open(p, "w") as f:
        f.write(f"#!/bin/sh\nexit {rc}\n")
    os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return p


def main() -> int:
    from lib.config import OrchConfig
    from lib.orch_schema import create_project, create_phase, create_task, get_neo4j_driver
    from lib import dispatch as D

    cfg = OrchConfig()
    drv = get_neo4j_driver(cfg)

    def task_status(tid: str):
        with drv.session(database=cfg.neo4j_db) as s:
            r = s.run("MATCH (t:OrchTask {id:$i}) RETURN t.status AS st", i=tid).single()
            return r["st"] if r else None

    def current_task(worker: str):
        return D._redis_connect().get(D._state_key(worker, "current_task"))

    def clean():
        with drv.session(database=cfg.neo4j_db) as s:
            s.run("MATCH (n) WHERE n.id STARTS WITH $p DETACH DELETE n", p=_PFX)
        try:
            D._redis_connect().delete(D._state_key(_WORKER, "current_task"))
        except Exception:
            pass

    clean()
    tmp = tempfile.mkdtemp(prefix="wakeatomic-")
    fail_cli = _stub(tmp, "notify-fail", 1)
    ok_cli = _stub(tmp, "notify-ok", 0)
    try:
        create_project(project_id=_PFX, name=_PFX, config=cfg)
        create_phase(project_id=_PFX, phase_id=f"{_PFX}::ph", name="ph", config=cfg)

        # --- FAILURE PATH: wake fails -> task reverts to pending, binding cleared ---
        tfail = f"{_PFX}::tfail"
        create_task(phase_id=f"{_PFX}::ph", task_id=tfail, description="fail", owner=_WORKER,
                    wake_owner_if_ready=False, config=cfg)
        os.environ["ORCH_NOTIFY_CLI"] = fail_cli
        raised = False
        try:
            D.dispatch(worker=_WORKER, task_id=tfail, description="fail", supervisor=f"{_PFX}-sup")
        except RuntimeError:
            raised = True
        _check("failed wake raises RuntimeError", raised)
        _check("failed wake REVERTS task to pending (no phantom live resolver)",
               task_status(tfail) == "pending", f"status={task_status(tfail)}")
        _check("failed wake CLEARS current_task binding", current_task(_WORKER) is None,
               f"current_task={current_task(_WORKER)}")

        # --- SUCCESS PATH: wake ok -> task in_progress, binding set (no regression) ---
        tok = f"{_PFX}::tok"
        create_task(phase_id=f"{_PFX}::ph", task_id=tok, description="ok", owner=_WORKER,
                    wake_owner_if_ready=False, config=cfg)
        os.environ["ORCH_NOTIFY_CLI"] = ok_cli
        D.dispatch(worker=_WORKER, task_id=tok, description="ok", supervisor=f"{_PFX}-sup")
        _check("successful wake leaves task in_progress", task_status(tok) == "in_progress",
               f"status={task_status(tok)}")
        ct = current_task(_WORKER)
        _check("successful wake sets current_task to the task",
               bool(ct) and json.loads(ct).get("task_id") == tok, f"current_task={ct}")
    finally:
        clean()
        os.environ.pop("ORCH_NOTIFY_CLI", None)
    if _FAILURES:
        print(f"\nFAIL — {len(_FAILURES)}: {_FAILURES}")
        return 1
    print("\nPASS — failed wake reverts to ready; successful wake claims. Dispatch is wake-atomic.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
