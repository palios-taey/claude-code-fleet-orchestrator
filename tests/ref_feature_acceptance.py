#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from lib.orch_schema import _REF_READ_BYTE_CAP, _read_ref_context, resolve_ref_path  # noqa: E402


def _assert(label: str, condition: bool, detail) -> None:
    print(f"PASS {label}" if condition else f"FAIL {label} {detail}")


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td)
        plan_dir = root / "plans"
        plan_dir.mkdir()
        plan_path = plan_dir / "plan.md"
        plan_path.write_text("# stub\n", encoding="utf-8")

        source_dir = plan_dir / "src"
        source_dir.mkdir()
        in_root = source_dir / "module.py"
        in_root.write_text("line1\nline2\nline3\n", encoding="utf-8")

        resolved, warning = resolve_ref_path("/etc/passwd", str(plan_path))
        _assert("absolute-path-rejected", resolved is None and warning == "ref outside allowed root: /etc/passwd", (resolved, warning))

        resolved, warning = resolve_ref_path("../secrets.txt", str(plan_path))
        _assert("dotdot-escape-rejected", resolved is None and warning == "ref outside allowed root: ../secrets.txt", (resolved, warning))

        outside = root / "outside.txt"
        outside.write_text("outside\n", encoding="utf-8")
        symlink_path = plan_dir / "escape.txt"
        os.symlink(outside, symlink_path)
        resolved, warning = resolve_ref_path("escape.txt", str(plan_path))
        _assert("symlink-escape-rejected", resolved is None and warning == "ref outside allowed root: escape.txt", (resolved, warning))

        ctx = _read_ref_context(
            [{"path": "src/module.py", "l_start": 2, "l_end": 3}],
            source_path=str(plan_path),
            line_cap=200,
        )
        first = ctx["refs"][0]
        ok_first = first.get("content") == "line2\nline3" and not first.get("warning")
        in_root.write_text("line1\nupdated2\nupdated3\n", encoding="utf-8")
        ctx_updated = _read_ref_context(
            [{"path": "src/module.py", "l_start": 2, "l_end": 3}],
            source_path=str(plan_path),
            line_cap=200,
        )
        second = ctx_updated["refs"][0]
        ok_second = second.get("content") == "updated2\nupdated3" and not second.get("warning")
        _assert("in-root-fresh-read", ok_first and ok_second, (first, second))

        big_path = plan_dir / "too-big.txt"
        big_path.write_bytes(b"x" * (_REF_READ_BYTE_CAP + 1))
        oversize = _read_ref_context(
            [{"path": "too-big.txt", "l_start": 1, "l_end": 5}],
            source_path=str(plan_path),
            line_cap=200,
        )
        oversize_first = oversize["refs"][0]
        expected_warning = f"ref unreadable: too-big.txt:1-5 (file exceeds byte cap {_REF_READ_BYTE_CAP})"
        _assert(
            "oversize-file-refused",
            oversize_first.get("warning") == expected_warning and "content" not in oversize_first,
            oversize_first,
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
