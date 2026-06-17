#!/usr/bin/env python3
"""Acceptance: allowed ref roots auto-derive from ORCH_SESSION_ROOTS.

Every supervisor's own repo root (its ORCH_SESSION_ROOTS value) is implicitly an
allowed ref root for its own plans — so adding a supervisor never requires hand-
syncing ORCH_REF_ALLOWED_ROOT. This is the root-cause fix for the recurring
"registered supervisor but can't ingest its own plan (422 source_path outside
ORCH_REF_ALLOWED_ROOT)" drift that blocked treasurer and taeys-hands.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fleet_orchestrator import orch_schema  # noqa: E402

FAILURES: list[str] = []


def _check(label: str, cond: bool, detail: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {detail}"))
    if not cond:
        FAILURES.append(label)


def main() -> int:
    orig_allow = os.environ.get("ORCH_REF_ALLOWED_ROOT")
    orig_roots = os.environ.get("ORCH_SESSION_ROOTS")
    try:
        # A supervisor root that is ONLY in ORCH_SESSION_ROOTS, never in the
        # explicit allow list, must be an allowed ref root.
        os.environ["ORCH_REF_ALLOWED_ROOT"] = "/home/op/the-conductor"
        os.environ["ORCH_SESSION_ROOTS"] = json.dumps({
            "weaver": "/home/op/isma",
            "taeys-hands": "/home/op/taeys-hands",
        })
        roots = set(orch_schema._allowed_ref_roots())
        _check("explicit ORCH_REF_ALLOWED_ROOT still honored",
               Path("/home/op/the-conductor").resolve() in roots, roots)
        _check("session root /home/op/isma (weaver) auto-allowed",
               Path("/home/op/isma").resolve() in roots, roots)
        _check("session root /home/op/taeys-hands auto-allowed",
               Path("/home/op/taeys-hands").resolve() in roots, roots)

        # comma key=value form also works
        os.environ["ORCH_SESSION_ROOTS"] = "infra=/home/op/infra-soul,x=/home/op/x"
        roots2 = set(orch_schema._allowed_ref_roots())
        _check("comma key=value session roots auto-allowed",
               Path("/home/op/infra-soul").resolve() in roots2 and Path("/home/op/x").resolve() in roots2, roots2)

        # shared dir (two supervisors, one dir — Jesse's note) de-dups, no crash
        os.environ["ORCH_SESSION_ROOTS"] = json.dumps({"a": "/home/op/shared", "b": "/home/op/shared"})
        roots3 = orch_schema._allowed_ref_roots()
        _check("shared session dir de-dups (appears once)",
               sum(1 for r in roots3 if r == Path("/home/op/shared").resolve()) == 1, roots3)

        # empty allow list + no session roots -> empty (no crash, refs stay disabled)
        os.environ["ORCH_REF_ALLOWED_ROOT"] = ""
        os.environ.pop("ORCH_SESSION_ROOTS", None)
        _check("no allow list + no session roots -> empty (refs disabled, no crash)",
               orch_schema._allowed_ref_roots() == [])
    finally:
        for k, v in (("ORCH_REF_ALLOWED_ROOT", orig_allow), ("ORCH_SESSION_ROOTS", orig_roots)):
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - allowed ref roots auto-derive from supervisor session roots.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
