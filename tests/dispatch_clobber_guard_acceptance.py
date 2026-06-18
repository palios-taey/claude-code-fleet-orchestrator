#!/usr/bin/env python3
"""Acceptance: dispatch refuses to clobber another dispatcher's live current_task."""
from __future__ import annotations

import json
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
    from fleet_orchestrator.orch_schema import create_phase, create_project, create_task, get_neo4j_driver
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

    def current_task() -> dict:
        raw = redis_client.get(D._state_key(WORKER, "current_task"))
        return json.loads(raw) if raw else {}

    def cleanup() -> None:
        with driver.session(database=cfg.neo4j_db) as session:
            session.run("MATCH (n) WHERE n.id STARTS WITH $p DETACH DELETE n", p=PFX)
        for suffix in ("current_task", "last_outcome", "parent"):
            redis_client.delete(D._state_key(WORKER, suffix))

    cleanup()
    old_notify_cli = os.environ.get("ORCH_NOTIFY_CLI")
    with tempfile.TemporaryDirectory(prefix="dispatch-clobber-") as raw_tmp:
        tmp = Path(raw_tmp)
        notify_log = tmp / "notify.log"
        os.environ["ORCH_NOTIFY_CLI"] = _notify_stub(tmp, notify_log)

        try:
            create_project(project_id=PFX, name="dispatch clobber guard", supervisor=SUP_A, config=cfg)
            create_phase(project_id=PFX, phase_id=task_id("phase"), name="phase", config=cfg)
            for name in ("first", "second", "forced"):
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

            refused = None
            try:
                D.dispatch(WORKER, task_id("second"), "second", supervisor=SUP_B)
            except D.WorkerBusy as exc:
                refused = str(exc)

            expected = f"worker busy with {SUP_A}:{task_id('first')} (in_progress)"
            _check("second dispatcher is refused with busy message", refused == expected, refused)
            preserved = current_task()
            _check("refused dispatch preserves first current_task", preserved == first_binding, preserved)
            _check("refused dispatch rolls second task back to pending", task_status(task_id("second")) == "pending", task_status(task_id("second")))
            notify_lines = notify_log.read_text(encoding="utf-8").splitlines()
            _check("refused dispatch does not notify worker", len(notify_lines) == 1, notify_lines)

            D.dispatch(WORKER, task_id("forced"), "forced", supervisor=SUP_B, force=True)
            forced = current_task()
            _check("force dispatch replaces current_task", forced.get("task_id") == task_id("forced"), forced)
            _check("force dispatch records new supervisor", forced.get("supervisor") == SUP_B, forced)
            _check("force dispatch notifies worker", len(notify_log.read_text(encoding="utf-8").splitlines()) == 2, notify_log.read_text(encoding="utf-8"))
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
