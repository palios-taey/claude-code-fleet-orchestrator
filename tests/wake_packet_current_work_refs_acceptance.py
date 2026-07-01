#!/usr/bin/env python3
"""Acceptance: implicit wake-packet context resolves current work before next-ready.

The notify hook calls the wake-packet endpoint without a task_id. A session that
is already mid-task must still get that task's refs, even when other pending
work exists or no pending work is ready.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import uuid
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _require_test_namespace() -> str:
    raw = (os.environ.get("ORCH_TEST_NAMESPACE") or "").strip()
    if not raw:
        raise SystemExit("ORCH_TEST_NAMESPACE is required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{5,80}", raw):
        raise SystemExit("ORCH_TEST_NAMESPACE must be 6-80 chars of letters/digits/._:-")
    if not any(marker in raw.lower() for marker in ("test", "ci", "acceptance")):
        raise SystemExit("ORCH_TEST_NAMESPACE must include test, ci, or acceptance")
    if raw.lower() in {"prod", "production", "neo4j", "default"}:
        raise SystemExit("ORCH_TEST_NAMESPACE must not name a production/default namespace")
    return raw


PFX = f"{_require_test_namespace()}-wake-current-{uuid.uuid4().hex[:8]}"
os.environ["NOTIFY_KEY_PREFIX"] = PFX
os.environ.setdefault("ORCH_REDIS_HOST", "127.0.0.1")
os.environ.setdefault("ORCH_REDIS_PORT", "6379")

from fleet_orchestrator import context_assembler as assembler  # noqa: E402
from fleet_orchestrator.config import OrchConfig, get_neo4j_driver  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    create_phase,
    create_project,
    create_task,
    init_schema,
    update_task_status,
)


CFG = OrchConfig()
WORKER = f"{PFX}-worker"
PROJECT = f"{PFX}-project"
PHASE = f"{PROJECT}::phase"
CURRENT = f"{PROJECT}::current"
READY = f"{PROJECT}::ready"
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _cleanup() -> None:
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=PFX)


def _select(worker: str, root: Path) -> dict[str, object]:
    with mock.patch.object(assembler, "get_overall_refs", return_value={"ref_context": {"refs": []}}), \
         mock.patch.object(assembler, "get_supervisor_refs", return_value={"ref_context": {"refs": []}}):
        return assembler.select_context(worker, cli="codex", session_roots={worker: str(root)})


def main() -> int:
    old_allowed_root = os.environ.get("ORCH_REF_ALLOWED_ROOT")
    old_session_ids = os.environ.get("ORCH_SESSION_IDS")
    try:
        _cleanup()
        with tempfile.TemporaryDirectory(prefix="wake-current-") as raw:
            root = Path(raw)
            os.environ["ORCH_REF_ALLOWED_ROOT"] = str(root)
            os.environ["ORCH_SESSION_IDS"] = WORKER
            source_path = root / "plan.md"
            source_path.write_text("# wake current refs fixture\n", encoding="utf-8")
            (root / "current_ref.md").write_text("CURRENT_TASK_REF_SURVIVES\n", encoding="utf-8")
            (root / "ready_ref.md").write_text("READY_TASK_REF_SURVIVES\n", encoding="utf-8")

            init_schema(config=CFG)
            create_project(PROJECT, "wake current refs", supervisor=WORKER, priority=1, config=CFG)
            create_phase(PROJECT, PHASE, "phase", config=CFG)
            create_task(
                PHASE,
                CURRENT,
                "current in-progress task",
                owner=WORKER,
                priority=50,
                refs=[{"path": "current_ref.md", "l_start": 1, "l_end": 1, "label": "current"}],
                source_path=str(source_path),
                wake_owner_if_ready=False,
                config=CFG,
            )
            create_task(
                PHASE,
                READY,
                "pending next-ready task",
                owner=WORKER,
                priority=0,
                refs=[{"path": "ready_ref.md", "l_start": 1, "l_end": 1, "label": "ready"}],
                source_path=str(source_path),
                wake_owner_if_ready=False,
                config=CFG,
            )
            update_task_status(CURRENT, "in_progress", owner=WORKER, config=CFG)

            current_context = _select(WORKER, root)
            current_work = (current_context.get("snapshot") or {}).get("resolved_work") or {}
            current_refs = current_context.get("task_refs") or []
            _check("implicit select_context resolves current work first", current_work.get("task_id") == CURRENT, current_work)
            _check("current work source is in_progress_own", current_work.get("source") == "in_progress_own", current_work)
            _check(
                "current work task-tier refs are populated",
                any("CURRENT_TASK_REF_SURVIVES" in str(ref.get("content") or "") for ref in current_refs),
                current_refs,
            )
            _check(
                "pending task refs do not replace current task refs",
                all("READY_TASK_REF_SURVIVES" not in str(ref.get("content") or "") for ref in current_refs),
                current_refs,
            )

            update_task_status(
                CURRENT,
                "completed",
                completion_evidence={"production_observation": "wake current refs acceptance completed current work"},
                config=CFG,
            )
            ready_context = _select(WORKER, root)
            ready_work = (ready_context.get("snapshot") or {}).get("resolved_work") or {}
            ready_refs = ready_context.get("task_refs") or []
            _check("pending-next-ready path remains available after current completes", ready_work.get("task_id") == READY, ready_work)
            _check("pending fallback source remains pending", ready_work.get("source") == "pending", ready_work)
            _check(
                "pending fallback task-tier refs are populated",
                any("READY_TASK_REF_SURVIVES" in str(ref.get("content") or "") for ref in ready_refs),
                ready_refs,
            )
    finally:
        if old_allowed_root is None:
            os.environ.pop("ORCH_REF_ALLOWED_ROOT", None)
        else:
            os.environ["ORCH_REF_ALLOWED_ROOT"] = old_allowed_root
        if old_session_ids is None:
            os.environ.pop("ORCH_SESSION_IDS", None)
        else:
            os.environ["ORCH_SESSION_IDS"] = old_session_ids
        _cleanup()

    if FAILURES:
        print(f"\nFAIL -- {len(FAILURES)}: {FAILURES}")
        return 1
    print("\nPASS -- implicit wake-packet context injects current task refs before pending fallback.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
