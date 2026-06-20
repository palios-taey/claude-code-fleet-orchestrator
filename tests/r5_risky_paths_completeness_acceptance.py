#!/usr/bin/env python3
from __future__ import annotations

import fnmatch
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RISK_PATHS = ROOT / ".github" / "r5-risky-paths"
SHIP_GATE = ROOT / ".github" / "workflows" / "ship-gate.yml"

FAILURES: list[str] = []


def _check(label: str, condition: bool, detail: object = "") -> None:
    print(("PASS " if condition else "FAIL ") + label + ("" if condition else f" -> {detail}"))
    if not condition:
        FAILURES.append(label)


def _patterns() -> list[str]:
    patterns: list[str] = []
    for raw in RISK_PATHS.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if stripped and not stripped.startswith("#"):
            patterns.append(stripped)
    return patterns


def _matches(path: str, patterns: list[str]) -> str | None:
    for pattern in patterns:
        if fnmatch.fnmatchcase(path, pattern):
            return pattern
    return None


def _python_paths_from_ship_gate() -> list[str]:
    text = SHIP_GATE.read_text(encoding="utf-8")
    paths: list[str] = []
    for match in re.finditer(r"(?<![\w./-])python\s+(?!-m\b|-)([A-Za-z0-9_./-]+\.py)\b", text):
        path = match.group(1)
        if path not in paths:
            paths.append(path)
    return paths


def main() -> int:
    patterns = _patterns()
    for required in (
        "scripts/verify-*.py",
        "tests/*.py",
        "migrations/*.py",
        "scripts/orch",
        "scripts/install",
    ):
        _check(f"required risky glob present: {required}", required in patterns, patterns)

    invoked_paths = _python_paths_from_ship_gate()
    _check("ship-gate python invocations discovered", bool(invoked_paths), invoked_paths)
    for path in invoked_paths:
        _check(f"ship-gate invocation is r5 covered: {path}", _matches(path, patterns) is not None, patterns)

    for path in ("scripts/orch", "scripts/install", "migrations/v1_3_0_stage_a/run_acceptance.py"):
        _check(f"critical non-ship-gate path is r5 covered: {path}", _matches(path, patterns) is not None, patterns)

    weakened = [pattern for pattern in patterns if pattern != "tests/*.py"]
    _check("teeth: removing tests/*.py exposes test oracle", _matches("tests/human_review_gate_acceptance.py", weakened) is None, weakened)
    weakened = [pattern for pattern in patterns if pattern != "scripts/verify-*.py"]
    _check("teeth: removing scripts/verify-*.py exposes verifier oracle", _matches("scripts/verify-ai-native-coherence.py", weakened) is None, weakened)

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - R5 risky paths cover ship-gate verifiers, acceptance tests, migrations, and dashless scripts.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
