#!/usr/bin/env python3
"""Acceptance: dispatch refuses to clobber another dispatcher's live current_task."""
from __future__ import annotations

import json
import io
import logging
import os
import stat
import sys
import tempfile
import uuid
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

FAILURES: list[str] = []
PFX = f"clobber-{uuid.uuid4().hex[:8]}"
WORKER = f"{PFX}-codex"
SUP_A = f"{PFX}-sup-a"
SUP_B = f"{PFX}-sup-b"


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _notify_stub(path: Path, log_path: Path) -> str:
    script = path / "notify-ok"
    script.write_text(
        "#!/bin/sh\n"
        f"printf 'CALL\\n' >> {str(log_path)!r}\n"
        "exit 0\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return str(script)


def main() -> int:
    from fleet_orchestrator.config import OrchConfig
    from fleet_orchestrator.orch_schema import (
        create_phase,
        create_project,
        create_task,
        get_neo4j_driver,
        update_task_status,
    )
    import fleet_orchestrator.dispatch as D

    cfg = OrchConfig()
    driver = get_neo4j_driver(cfg)
    redis_client = D._redis_connect()

    def task_id(name: str) -> str:
        return f"{PFX}::{name}"

    def task_status(tid: str) -> str | None:
        with driver.session(database=cfg.neo4j_db) as session:
            row = session.run("MATCH (t:OrchTask {id:$id}) RETURN t.status AS status", id=tid).single()
        return str(row["status"]) if row else None

    def task_record(tid: str) -> dict:
        with driver.session(database=cfg.neo4j_db) as session:
            row = session.run(
                """
                MATCH (t:OrchTask {id:$id})
                RETURN t.status AS status, t.owner AS owner, t.dispatched_to AS dispatched_to
                """,
                id=tid,
            ).single()
        return row.data() if row else {}

    def direct_task_status_change(tid: str, status: str) -> None:
        with driver.session(database=cfg.neo4j_db) as session:
            session.run(
                "MATCH (t:OrchTask {id:$id}) SET t.status = $status REMOVE t.dispatched_to",
                id=tid,
                status=status,
            ).consume()

    def phantom_in_progress(tid: str, owner: str) -> None:
        with driver.session(database=cfg.neo4j_db) as session:
            session.run(
                """
                MATCH (t:OrchTask {id:$id})
                SET t.status = 'in_progress',
                    t.owner = $owner,
                    t.blocked_on = 'stop_when_all_ready_tasks_dispatched'
                REMOVE t.dispatched_to
                """,
                id=tid,
                owner=owner,
            ).consume()

    def current_task() -> dict:
        raw = redis_client.get(D._state_key(WORKER, "current_task"))
        return json.loads(raw) if raw else {}

    def cleanup() -> None:
        with driver.session(database=cfg.neo4j_db) as session:
            session.run("MATCH (n) WHERE n.id STARTS WITH $p DETACH DELETE n", p=PFX)
        for session_id in (WORKER, SUP_A, SUP_B):
            for suffix in ("current_task", "last_outcome", "parent"):
                redis_client.delete(D._state_key(session_id, suffix))

    cleanup()
    old_notify_cli = os.environ.get("ORCH_NOTIFY_CLI")
    with tempfile.TemporaryDirectory(prefix="dispatch-clobber-") as raw_tmp:
        tmp = Path(raw_tmp)
        notify_log = tmp / "notify.log"
        os.environ["ORCH_NOTIFY_CLI"] = _notify_stub(tmp, notify_log)

        try:
            create_project(project_id=PFX, name="dispatch clobber guard", supervisor=SUP_A, config=cfg)
            create_phase(project_id=PFX, phase_id=task_id("phase"), name="phase", config=cfg)
            for name in ("first", "same-second", "stale-next", "second", "forced", "terminal-next", "force-recover"):
                create_task(
                    phase_id=task_id("phase"),
                    task_id=task_id(name),
                    description=name,
                    owner=WORKER,
                    wake_owner_if_ready=False,
                    config=cfg,
                )

            D.dispatch(WORKER, task_id("first"), "first", supervisor=SUP_A)
            first_binding = current_task()
            _check("first dispatch binds current_task", first_binding.get("task_id") == task_id("first"), first_binding)
            _check("first dispatch records dispatcher", first_binding.get("dispatcher") == SUP_A, first_binding)
            _check("first dispatch marks task in_progress", task_status(task_id("first")) == "in_progress", task_status(task_id("first")))

            same_dispatcher_refused = None
            try:
                D.dispatch(WORKER, task_id("same-second"), "same-second", supervisor=SUP_A)
            except D.WorkerBusy as exc:
                same_dispatcher_refused = str(exc)

            expected = f"worker busy with {SUP_A}:{task_id('first')} (in_progress)"
            _check("same dispatcher different task is refused", same_dispatcher_refused == expected, same_dispatcher_refused)
            preserved = current_task()
            _check("same dispatcher refusal preserves first current_task", preserved == first_binding, preserved)
            _check("same dispatcher refusal rolls task back to pending", task_status(task_id("same-second")) == "pending", task_status(task_id("same-second")))
            notify_lines = notify_log.read_text(encoding="utf-8").splitlines()
            _check("same dispatcher refusal does not notify worker", len(notify_lines) == 1, notify_lines)

            direct_task_status_change(task_id("first"), "pending")
            log_stream = io.StringIO()
            log_handler = logging.StreamHandler(log_stream)
            D.logger.addHandler(log_handler)
            try:
                D.dispatch(WORKER, task_id("stale-next"), "stale-next", supervisor=SUP_A)
            finally:
                D.logger.removeHandler(log_handler)
            rebound = current_task()
            expected = f"worker busy with {SUP_A}:{task_id('stale-next')} (in_progress)"
            _check("stale pending binding allows different task dispatch", rebound.get("task_id") == task_id("stale-next"), rebound)
            _check("stale binding clear is logged", "stale current_task binding cleared during dispatch" in log_stream.getvalue(), log_stream.getvalue())
            _check("stale dispatch updates nonce", rebound.get("started_at") != first_binding.get("started_at"), rebound)
            _check("stale next task is in_progress", task_status(task_id("stale-next")) == "in_progress", task_status(task_id("stale-next")))
            _check("stale previous task stays pending", task_status(task_id("first")) == "pending", task_status(task_id("first")))
            _check("stale clear dispatch notifies worker", len(notify_log.read_text(encoding="utf-8").splitlines()) == 2, notify_log.read_text(encoding="utf-8"))

            refused = None
            try:
                D.dispatch(WORKER, task_id("second"), "second", supervisor=SUP_B)
            except D.WorkerBusy as exc:
                refused = str(exc)

            _check("second dispatcher is refused with busy message", refused == expected, refused)
            preserved = current_task()
            _check("refused dispatch preserves current_task", preserved == rebound, preserved)
            _check("refused dispatch rolls second task back to pending", task_status(task_id("second")) == "pending", task_status(task_id("second")))
            notify_lines = notify_log.read_text(encoding="utf-8").splitlines()
            _check("refused dispatch does not notify worker", len(notify_lines) == 2, notify_lines)

            D.dispatch(WORKER, task_id("forced"), "forced", supervisor=SUP_B, force=True)
            forced = current_task()
            _check("force dispatch replaces current_task", forced.get("task_id") == task_id("forced"), forced)
            _check("force dispatch records new supervisor", forced.get("supervisor") == SUP_B, forced)
            _check("force dispatch notifies worker", len(notify_log.read_text(encoding="utf-8").splitlines()) == 3, notify_log.read_text(encoding="utf-8"))

            update_task_status(
                task_id("forced"),
                "completed",
                owner=WORKER,
                completion_evidence={"production_observation": "dispatch clobber guard completed forced task"},
                config=cfg,
            )
            D.dispatch(WORKER, task_id("terminal-next"), "terminal-next", supervisor=SUP_B)
            terminal_next = current_task()
            _check("terminal existing task allows next bind", terminal_next.get("task_id") == task_id("terminal-next"), terminal_next)
            _check("terminal next task is in_progress", task_status(task_id("terminal-next")) == "in_progress", task_status(task_id("terminal-next")))
            _check("terminal previous task stays completed", task_status(task_id("forced")) == "completed", task_status(task_id("forced")))
            _check("terminal next dispatch notifies worker", len(notify_log.read_text(encoding="utf-8").splitlines()) == 4, notify_log.read_text(encoding="utf-8"))

            phantom_in_progress(task_id("force-recover"), SUP_A)
            redis_client.set(
                D._state_key(SUP_A, "current_task"),
                json.dumps({"task_id": task_id("force-recover"), "description": "force-recover", "supervisor": SUP_A, "started_at": 321.0}),
            )

            not_ready = None
            try:
                D.dispatch(WORKER, task_id("force-recover"), "force-recover", supervisor=SUP_B)
            except D.OrchTaskNotReady as exc:
                not_ready = str(exc)
            _check("plain dispatch refuses in_progress phantom", bool(not_ready and "status=in_progress" in not_ready), not_ready)
            _check("plain phantom refusal does not notify worker", len(notify_log.read_text(encoding="utf-8").splitlines()) == 4, notify_log.read_text(encoding="utf-8"))

            D.dispatch(WORKER, task_id("force-recover"), "force-recover", supervisor=SUP_B, force=True)
            recovered = current_task()
            recovered_record = task_record(task_id("force-recover"))
            _check("force dispatch recovers in_progress phantom", recovered.get("task_id") == task_id("force-recover"), recovered)
            _check("force recovery binds target worker", recovered_record.get("dispatched_to") == WORKER, recovered_record)
            _check("force recovery leaves task in_progress", recovered_record.get("status") == "in_progress", recovered_record)
            _check("force recovery clears stale caller current_task", redis_client.get(D._state_key(SUP_A, "current_task")) is None)
            _check("force recovery notifies worker", len(notify_log.read_text(encoding="utf-8").splitlines()) == 5, notify_log.read_text(encoding="utf-8"))
        finally:
            if old_notify_cli is None:
                os.environ.pop("ORCH_NOTIFY_CLI", None)
            else:
                os.environ["ORCH_NOTIFY_CLI"] = old_notify_cli
            cleanup()

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - dispatch refuses cross-dispatcher current_task clobber and preserves binding.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
