#!/usr/bin/env python3
"""Acceptance: merge audit rows reuse the accountability hash chain."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fleet_orchestrator import accountability_ledger as ledger  # noqa: E402


FAILURES: list[str] = []
REPO = "palios-taey/claude-code-fleet-orchestrator"
SHA = "7504180530d140d24921bc8715574e117c69f2d0"


def _check(label: str, condition: bool, detail: object = "") -> None:
    print(("PASS " if condition else "FAIL ") + label + ("" if condition else f" -> {detail}"))
    if not condition:
        FAILURES.append(label)


def _read_rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.startswith("#"):
            continue
        rows.append(json.loads(raw))
    return rows


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="merge-ledger-") as tmp:
        root = Path(tmp)
        default_ledger = root / "ledger.jsonl"
        ci_ledger = root / "ci-audit.jsonl"

        ledger.append({"type": "audit_verdict", "verdict": "pass"}, ts=1.0, path=str(default_ledger))
        _check("default ledger verifies through parameterized path",
               ledger.verify_chain(path=str(default_ledger)) == {"ok": True, "rows": 1})
        _check("CI ledger starts empty and separate",
               ledger.verify_chain(path=str(ci_ledger)) == {"ok": True, "rows": 0})

        first = ledger.record_merge(
            REPO,
            SHA,
            [
                {"gate": "r5-audit-gate", "verdict": "success"},
                {"gate": "ship-gate-acceptance", "verdict": "success"},
            ],
            {"r5-audit-gate": 1.25, "ship-gate-acceptance": 2.0},
            "conductor",
            ts=2.0,
            path=str(ci_ledger),
        )
        event = first["event"]
        _check("record_merge writes type=merge", event.get("type") == "merge", event)
        _check("record_merge stores repo and sha", event.get("repo") == REPO and event.get("sha") == SHA, event)
        _check("record_merge carries per-gate durations",
               event.get("gate_results") == [
                   {"gate": "r5-audit-gate", "verdict": "success", "duration_s": 1.25},
                   {"gate": "ship-gate-acceptance", "verdict": "success", "duration_s": 2.0},
               ],
               event)
        _check("record_merge stores summed total_duration_s", event.get("total_duration_s") == 3.25, event)
        _check("CI ledger verifies after direct record_merge",
               ledger.verify_chain(path=str(ci_ledger)) == {"ok": True, "rows": 1})
        _check("default ledger row count remains unchanged",
               ledger.verify_chain(path=str(default_ledger)) == {"ok": True, "rows": 1})

        env = os.environ.copy()
        env["PYTHONPATH"] = str(ROOT)
        cli = subprocess.run(
            [
                sys.executable,
                "-m",
                "fleet_orchestrator.accountability_ledger",
                "record-merge",
                "--path",
                str(ci_ledger),
                "--repo",
                REPO,
                "--sha",
                SHA,
                "--merged-by",
                "conductor",
                "--gate-result",
                "r5-audit-gate=success=1.5",
                "--gate-result",
                "ship-gate-acceptance=success=2.5",
                "--ts",
                "3.0",
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        _check("record-merge CLI exits 0", cli.returncode == 0, cli.stderr or cli.stdout)
        _check("CI ledger verifies after CLI append",
               ledger.verify_chain(path=str(ci_ledger)) == {"ok": True, "rows": 2})
        rows = _read_rows(ci_ledger)
        _check("CLI row is appended to same hash chain",
               len(rows) == 2 and rows[1]["prev_hash"] == rows[0]["hash"], rows)

        verify = subprocess.run(
            [
                sys.executable,
                "-m",
                "fleet_orchestrator.accountability_ledger",
                "verify-chain",
                "--path",
                str(ci_ledger),
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        _check("verify-chain CLI exits 0", verify.returncode == 0, verify.stderr or verify.stdout)
        _check("verify-chain CLI reports two rows",
               json.loads(verify.stdout)["rows"] == 2, verify.stdout)

        try:
            ledger.record_merge(
                REPO,
                SHA,
                [{"gate": "r5-audit-gate", "verdict": "success"}],
                {},
                "conductor",
                path=str(ci_ledger),
            )
        except ValueError as exc:
            _check("missing durations fail loud", "durations are required" in str(exc), exc)
        else:
            _check("missing durations fail loud", False, "record_merge accepted absent durations")

        no_gate = subprocess.run(
            [
                sys.executable,
                "-m",
                "fleet_orchestrator.accountability_ledger",
                "record-merge",
                "--path",
                str(ci_ledger),
                "--repo",
                REPO,
                "--sha",
                SHA,
                "--merged-by",
                "conductor",
            ],
            cwd=ROOT,
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        _check("CLI refuses absent gate results",
               no_gate.returncode != 0 and "at least one" in no_gate.stderr,
               no_gate.stderr or no_gate.stdout)

        tampered = ci_ledger.read_text(encoding="utf-8").replace('"success"', '"failure"', 1)
        ci_ledger.write_text(tampered, encoding="utf-8")
        _check("verify_chain detects rewritten CI row",
               ledger.verify_chain(path=str(ci_ledger)).get("ok") is False,
               ledger.verify_chain(path=str(ci_ledger)))

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - merge audit ledger reuses the existing hash chain and refuses synthesized rows.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
