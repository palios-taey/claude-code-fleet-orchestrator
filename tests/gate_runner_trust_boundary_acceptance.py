"""Acceptance: gate-runner shell execution has an explicit trusted-input boundary."""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def main() -> int:
    gate_runner = (ROOT / "fleet_orchestrator" / "gate_runner.py").read_text(encoding="utf-8")
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
    shippability = (ROOT / "docs" / "SHIPPABILITY.md").read_text(encoding="utf-8")
    shippability_flat = " ".join(shippability.split())

    _check("gate runner still uses shell execution", "shell=True" in gate_runner, "gate_runner.py")
    _check("gate runner docstring points to SECURITY.md", "SECURITY.md" in gate_runner, "gate_runner.py")
    _check("security doc names gate-runner command boundary", "Gate-runner command strings are trusted local input" in security, "SECURITY.md")
    _check("security doc names CLI provenance", "--clean" in security and "--boot" in security and "--assert" in security, "SECURITY.md")
    _check("security doc names run_gate provenance", "run_gate" in security and "assert_cmd" in security, "SECURITY.md")
    _check("security doc separates ORCH_SHIP_GATES from command strings",
           "ORCH_SHIP_GATES" in security and "does not supply shell command strings" in security,
           "SECURITY.md")
    _check("security doc says shell commands are not sandboxed",
           "does not sandbox those gate commands" in security and "must not be treated as safe for untrusted gate definitions" in security,
           "SECURITY.md")
    _check("security doc requires sandbox or structured commands for untrusted sources",
           "untrusted input" in security and "structured argv commands" in security and "sandbox" in security,
           "SECURITY.md")
    _check("shippability doc cross-links gate-runner trust boundary",
           "trusted operator-authored local input" in shippability_flat and "not sandboxed" in shippability_flat,
           "docs/SHIPPABILITY.md")

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - gate-runner shell trust boundary is documented and cannot-lie.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
