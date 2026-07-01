#!/usr/bin/env python3
"""Acceptance: registered sessions missing ORCH_SESSION_ROOTS fail loud in wake packets."""
from __future__ import annotations

import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fleet_orchestrator import context_assembler as assembler  # noqa: E402


FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


@contextmanager
def _preserved_env() -> Iterator[None]:
    keys = ("ORCH_SESSION_IDS", "ORCH_SESSION_ROOTS", "ORCH_RULES_ROOT")
    original = {key: os.environ.get(key) for key in keys}
    try:
        for key in keys:
            os.environ.pop(key, None)
        yield
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _summary() -> dict:
    return {
        "project": {"id": "root-gap", "name": "Root Gap", "source_path": ""},
        "phases": [
            {
                "phase": {"id": "root-gap::phase", "name": "Phase"},
                "tasks": [
                    {
                        "id": "root-gap::task",
                        "description": "registered session root gap",
                        "status": "in_progress",
                        "owner": "linkedin",
                    }
                ],
            }
        ],
        "ref_tiers": {
            "overall": {"ref_context": {"refs": []}},
            "project": {"ref_context": {"refs": []}},
            "phases": [],
            "tasks": [],
        },
    }


def _select(session: str, roots: dict[str, str]) -> dict:
    empty_refs = {"ref_context": {"refs": []}}
    with mock.patch.object(assembler, "get_task_project", return_value={"project_id": "root-gap", "project_name": "Root Gap"}), \
         mock.patch.object(assembler, "get_project_summary", return_value=_summary()), \
         mock.patch.object(assembler, "get_task_step_governance", return_value={}), \
         mock.patch.object(assembler, "get_overall_refs", return_value=empty_refs), \
         mock.patch.object(assembler, "get_supervisor_refs", return_value=empty_refs):
        return assembler.select_context(session, task_id="root-gap::task", cli="codex", session_roots=roots)


def _render(session: str, context: dict) -> str:
    return assembler.assemble(assembler.build_packet(session, context), "codex")


def main() -> int:
    with _preserved_env(), tempfile.TemporaryDirectory(prefix="session-root-") as raw:
        root = Path(raw)
        os.environ["ORCH_SESSION_IDS"] = "linkedin,conductor"
        missing = _select("linkedin", roots={})
        missing_rendered = _render("linkedin", missing)

        _check("missing root creates task warning ref", len(missing.get("task_refs") or []) == 1, missing.get("task_refs"))
        _check("warning names missing ORCH_SESSION_ROOTS", "has no ORCH_SESSION_ROOTS entry" in str(missing["task_refs"][0].get("warning")), missing["task_refs"])
        _check("packet renders task ref instead of silent none", "### task\n- ref 1" in missing_rendered and "### task\n- none" not in missing_rendered, missing_rendered)
        _check("packet teaches the repair", "task refs cannot resolve reliably" in missing_rendered and "linkedin=<repo-root>" in missing_rendered, missing_rendered)

        present = _select("linkedin", roots={"linkedin": str(root)})
        present_rendered = _render("linkedin", present)
        _check("configured root suppresses missing-root warning", assembler.MISSING_SESSION_ROOT_MARKER not in present_rendered, present_rendered)

        peer = _select("linkedin-codex", roots={"linkedin": str(root)})
        peer_rendered = _render("linkedin-codex", peer)
        _check("peer session inherits parent root without warning", assembler.MISSING_SESSION_ROOT_MARKER not in peer_rendered, peer_rendered)

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - missing registered session roots are visible in wake packets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
