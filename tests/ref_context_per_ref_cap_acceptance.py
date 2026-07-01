#!/usr/bin/env python3
"""Acceptance: required refs do not starve each other.

Wake packets can carry several required refs in the same tier. A large first
ref must not consume a shared line budget and leave later refs empty; each ref
gets its own line-cap slice, while whole-packet trimming remains the assembler's
job.
"""
from __future__ import annotations

import json
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


NAMESPACE = _require_test_namespace()
PFX = f"{NAMESPACE}-refcap-{uuid.uuid4().hex[:8]}"
os.environ["NOTIFY_KEY_PREFIX"] = PFX
from fleet_orchestrator.test_isolation import assert_acceptance_redis_isolated  # noqa: E402

assert_acceptance_redis_isolated()

from fleet_orchestrator import context_assembler as assembler  # noqa: E402
from fleet_orchestrator.config import OrchConfig, get_neo4j_driver  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    _read_ref_context,
    create_phase,
    create_project,
    create_task,
    init_schema,
)


CFG = OrchConfig()
SUP = f"{PFX}-sup"
LINKEDIN = f"{PFX}-linkedin"
PROJECT = f"{PFX}-project"
PHASE = f"{PROJECT}::phase"
TASK = f"{PROJECT}::step-1-comment"
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _cleanup() -> None:
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=PFX)


def _write_refs(root: Path) -> list[dict[str, object]]:
    names = [
        "linkedin_surfaces.yaml",
        "comment_guidelines.md",
        "linkedin_comment_research.yaml",
        "linkedin_feed_comment.yaml",
        "voice_examples.md",
        "risk_filters.md",
        "reply_process.md",
    ]
    refs: list[dict[str, object]] = []
    first_lines = [f"surface-{idx:03d}: choose the exact surface" for idx in range(1, 221)]
    (root / names[0]).write_text("\n".join(first_lines) + "\n", encoding="utf-8")
    refs.append({"path": names[0], "l_start": 1, "l_end": 220, "label": "surfaces"})
    for idx, name in enumerate(names[1:], start=2):
        content = [
            f"voice-ref-{idx}: required guidance for human-sounding LinkedIn comments",
            f"voice-ref-{idx}: process detail must survive context selection",
        ]
        (root / name).write_text("\n".join(content) + "\n", encoding="utf-8")
        refs.append({"path": name, "l_start": 1, "l_end": 2, "label": f"voice-{idx}"})
    return refs


def _all_have_content(refs: list[dict[str, object]]) -> bool:
    return all(str(ref.get("content") or "").strip() for ref in refs)


def main() -> int:
    old_allowed_root = os.environ.get("ORCH_REF_ALLOWED_ROOT")
    try:
        _cleanup()
        with tempfile.TemporaryDirectory(prefix="refcap-") as raw:
            root = Path(raw)
            os.environ["ORCH_REF_ALLOWED_ROOT"] = str(root)
            source_path = root / "plan.md"
            source_path.write_text("# ref cap fixture\n", encoding="utf-8")
            refs = _write_refs(root)

            direct = _read_ref_context(refs, source_path=str(source_path), line_cap=200)
            direct_refs = direct.get("refs") or []
            _check("direct resolver returns all seven refs", len(direct_refs) == 7, direct)
            _check("direct resolver gives every ref content", _all_have_content(direct_refs), direct_refs)
            _check("first oversized ref is truncated per-ref", direct_refs[0].get("truncated") is True, direct_refs[0])
            _check("later voice refs are not starved", all(not ref.get("warning") for ref in direct_refs[1:]), direct_refs)
            _check("aggregate-cap warning is gone", "aggregate line cap" not in json.dumps(direct), direct)

            init_schema(config=CFG)
            create_project(PROJECT, "ref cap project", supervisor=SUP, priority=1, config=CFG)
            create_phase(PROJECT, PHASE, "phase", config=CFG)
            create_task(
                PHASE,
                TASK,
                "LinkedIn step 1 comment with required voice refs",
                owner=LINKEDIN,
                priority=10,
                refs=refs,
                source_path=str(source_path),
                wake_owner_if_ready=False,
                config=CFG,
            )

            with mock.patch.object(assembler, "get_overall_refs", return_value={"ref_context": {"refs": []}}), \
                 mock.patch.object(assembler, "get_supervisor_refs", return_value={"ref_context": {"refs": []}}):
                context = assembler.select_context(LINKEDIN, task_id=TASK, cli="codex", session_roots={})

            task_refs = context.get("task_refs") or []
            _check("select_context exposes all seven task refs", len(task_refs) == 7, task_refs)
            _check("select_context gives every task ref content", _all_have_content(task_refs), task_refs)
            _check("select_context preserves later voice guidance", all("voice-ref-" in str(ref.get("content") or "") for ref in task_refs[1:]), task_refs)
    finally:
        if old_allowed_root is None:
            os.environ.pop("ORCH_REF_ALLOWED_ROOT", None)
        else:
            os.environ["ORCH_REF_ALLOWED_ROOT"] = old_allowed_root
        _cleanup()

    if FAILURES:
        print(f"\nFAIL -- {len(FAILURES)}: {FAILURES}")
        return 1
    print("\nPASS -- ref context line cap is per-ref and all required refs survive selection.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
