"""Ship-gate e2e — dispatch is wake-atomic AND rollback is identity-guarded.

dispatch() claims the OrchTask (status=in_progress) and binds Redis current_task BEFORE
the wake (taey-notify). If the wake fails, the task must revert to pending (ready) and the
binding clear -- but ONLY this dispatch's binding, never a concurrent same-worker dispatch's
(grok PR#25 audit V1/V2), and rollback failures must be observable not silent (V4).

Cases:
  1. failed wake  -> RuntimeError + task reverts to pending + binding cleared      (happy revert)
  2. successful wake -> in_progress + binding set                                  (no regression)
  3. worker moved to T2 (overlap): rollback(T1) reverts T1 (safe) but PRESERVES T2 binding (V2)
  4. T1 re-claimed by a newer nonce: rollback(old) leaves status AND binding alone (V1)
  5. rollback neo failure is LOGGED, never raised                                   (V4)

Env: ORCH_NEO4J_URI, ORCH_NEO4J_DB, ORCH_REDIS_HOST/PORT, ORCH_DASHBOARD_URL, ORCH_NOTIFY_LIB_ROOT.
"""
from __future__ import annotations

import json
import logging
import os
import stat
import sys
import tempfile
import uuid
import importlib

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
    from fleet_orchestrator.config import OrchConfig
    from fleet_orchestrator.orch_schema import create_project, create_phase, create_task, get_neo4j_driver
    D = importlib.import_module("fleet_orchestrator.dispatch")

    cfg = OrchConfig()
    drv = get_neo4j_driver(cfg)

    def task_status(tid: str):
        with drv.session(database=cfg.neo4j_db) as s:
            r = s.run("MATCH (t:OrchTask {id:$i}) RETURN t.status AS st", i=tid).single()
            return r["st"] if r else None

    def set_status(tid: str, st: str, binding_nonce: float):
        with drv.session(database=cfg.neo4j_db) as s:
            s.run(
                """
                MATCH (t:OrchTask {id:$i})
                SET t.status=$st,
                    t.owner=$w,
                    t.dispatched_to=$w,
                    t.dispatch_claim_worker=$w,
                    t.dispatch_claim_started_at=$binding_nonce
                """,
                i=tid,
                st=st,
                w=_WORKER,
                binding_nonce=binding_nonce,
            )

    def get_ct(worker: str):
        return D._redis_connect().get(D._state_key(worker, "current_task"))

    def set_ct(worker: str, task_id: str, started_at: float):
        D._redis_connect().set(D._state_key(worker, "current_task"),
                               json.dumps({"task_id": task_id, "description": "x",
                                           "supervisor": f"{_PFX}-sup", "started_at": started_at}))

    def clean():
        with drv.session(database=cfg.neo4j_db) as s:
            s.run("MATCH (n) WHERE n.id STARTS WITH $p DETACH DELETE n", p=_PFX)
        try:
            redis_client = D._redis_connect()
            prefix = os.environ.get("NOTIFY_KEY_PREFIX", "taey")
            patterns = (
                D._state_key(_WORKER, "*"),
                f"{prefix}:worker-task-liveness:{_PFX}*",
                f"{prefix}:worker-task-liveness-escalated:{_PFX}*",
            )
            for pattern in patterns:
                cursor = 0
                while True:
                    cursor, keys = redis_client.scan(cursor=cursor, match=pattern, count=100)
                    if keys:
                        redis_client.delete(*keys)
                    if cursor == 0:
                        break
        except Exception:
            pass

    clean()
    tmp = tempfile.mkdtemp(prefix="wakeatomic-")
    fail_cli = _stub(tmp, "notify-fail", 1)
    ok_cli = _stub(tmp, "notify-ok", 0)
    try:
        create_project(project_id=_PFX, name=_PFX, config=cfg)
        create_phase(project_id=_PFX, phase_id=f"{_PFX}::ph", name="ph", config=cfg)
        for n in ("tfail", "tok", "t1", "t2", "tlog"):
            create_task(phase_id=f"{_PFX}::ph", task_id=f"{_PFX}::{n}", description=n,
                        owner=_WORKER, wake_owner_if_ready=False, config=cfg)

        # 1. FAILURE PATH (happy revert)
        tfail = f"{_PFX}::tfail"
        os.environ["ORCH_NOTIFY_CLI"] = fail_cli
        raised = False
        try:
            D.dispatch(worker=_WORKER, task_id=tfail, description="fail", supervisor=f"{_PFX}-sup")
        except RuntimeError:
            raised = True
        _check("failed wake raises RuntimeError", raised)
        _check("failed wake REVERTS task to pending", task_status(tfail) == "pending", task_status(tfail))
        _check("failed wake CLEARS our binding", get_ct(_WORKER) is None, get_ct(_WORKER))

        # 2. SUCCESS PATH (no regression)
        tok = f"{_PFX}::tok"
        os.environ["ORCH_NOTIFY_CLI"] = ok_cli
        D.dispatch(worker=_WORKER, task_id=tok, description="ok", supervisor=f"{_PFX}-sup")
        _check("successful wake -> in_progress", task_status(tok) == "in_progress", task_status(tok))
        ct = get_ct(_WORKER)
        _check("successful wake sets current_task", bool(ct) and json.loads(ct)["task_id"] == tok, ct)

        # 3. OVERLAP (V2): worker moved to T2; rollback(T1) reverts T1 but keeps T2's binding
        t1, t2 = f"{_PFX}::t1", f"{_PFX}::t2"
        set_status(t1, "in_progress", 111.0)
        set_ct(_WORKER, t2, 222.0)          # worker is now bound to T2
        D._rollback_claim(_WORKER, t1, 111.0)  # T1's failed dispatch (nonce 111 != live 222/T2)
        _check("overlap: T1 reverted to pending (T1-specific, safe)", task_status(t1) == "pending", task_status(t1))
        ct = get_ct(_WORKER)
        _check("overlap: T2 binding PRESERVED (not nuked)",
               bool(ct) and json.loads(ct)["task_id"] == t2, ct)

        # 4. RE-CLAIM (V1): T1 re-dispatched with a newer nonce; rollback(old nonce) is a no-op
        set_status(t1, "in_progress", 999.0)
        set_ct(_WORKER, t1, 999.0)          # T1 re-bound with a NEW nonce
        D._rollback_claim(_WORKER, t1, 111.0)  # the OLD dispatch's stale rollback
        _check("reclaim: T1 stays in_progress (newer claim not clobbered)",
               task_status(t1) == "in_progress", task_status(t1))
        ct = get_ct(_WORKER)
        _check("reclaim: newer T1 binding untouched",
               bool(ct) and json.loads(ct)["started_at"] == 999.0, ct)

        # 5. OBSERVABILITY (V4): a neo failure during rollback is LOGGED, never raised
        tlog = f"{_PFX}::tlog"
        set_status(tlog, "in_progress", 333.0)
        set_ct(_WORKER, tlog, 333.0)
        orig = D.get_neo4j_session
        D.get_neo4j_session = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("forced neo down"))
        cap = []
        h = logging.Handler(); h.emit = lambda rec: cap.append(rec.getMessage())
        D.logger.addHandler(h); D.logger.setLevel(logging.WARNING)
        try:
            D._rollback_claim(_WORKER, tlog, 333.0)  # must NOT raise
            _check("observability: rollback does not raise on neo failure", True)
        except Exception as e:
            _check("observability: rollback does not raise on neo failure", False, repr(e))
        finally:
            D.get_neo4j_session = orig
            D.logger.removeHandler(h)
        _check("observability: neo revert failure is LOGGED",
               any("neo revert" in m for m in cap), str(cap))
    finally:
        clean()
        os.environ.pop("ORCH_NOTIFY_CLI", None)
    if _FAILURES:
        print(f"\nFAIL — {len(_FAILURES)}: {_FAILURES}")
        return 1
    print("\nPASS — wake-atomic + identity-guarded rollback (V1/V2/V4 closed).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
