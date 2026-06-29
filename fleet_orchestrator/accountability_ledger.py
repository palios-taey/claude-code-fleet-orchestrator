"""Append-only, hash-chained accountability ledger — an HONEST RECORD bound by honor.

Every "done" claim, audit verdict, and gate-run result is APPENDED here and never deleted or
rewritten. Each row carries the previous row's hash, so an accidental edit, an out-of-order write,
or a dropped row breaks the chain and verify_chain() reports it. There is deliberately NO delete or
rewrite function in this module.

HONEST SCOPE — do NOT overstate this file (gatekeeper audit 2026-06-04, ANTIFAB synthesis §3):
- This is a LOCAL file. The hash algorithm and the genesis root are PUBLIC, and the file is writable
  by the very instance it records. So the chain is tamper-EVIDENT against accidents and PASSIVE
  corner-cutting, but it is NOT tamper-PROOF against a determined deceiver who holds the pen: such an
  instance could truncate the tail or fully re-chain the file and verify_chain() would still read OK.
  Closing THAT requires an anchor hosted where this instance has no write/admin root (the operator's call).
  It is NOT claimed here.
- What actually binds a possibly-deceptive instance is independent EXECUTION: the orchestrator running
  the verification gate itself (plan P1) and a separate gatekeeper instance doing full-code review
  (plan P3/P4). This ledger is the RECORD those mechanisms write to — not the enforcement.
- Treat the ledger as append-only operational evidence. Deleting or editing it breaks the
  accountability chain this module exists to preserve.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import math
import os
import sys
import time
from collections.abc import Mapping, Sequence
from typing import Any, Dict, Optional

from fleet_orchestrator.paths import data_dir

# No operator-specific default: explicit override wins, else under the user's data dir.
LEDGER_PATH = os.environ.get("ACCOUNTABILITY_LEDGER_PATH") or str(
    data_dir() / "accountability" / "ledger.jsonl"
)
CI_AUDIT_PATH = os.environ.get("ACCOUNTABILITY_CI_AUDIT_PATH") or str(
    data_dir() / "accountability" / "ci-audit.jsonl"
)
_HEADER = (
    "# APPEND-ONLY ACCOUNTABILITY LEDGER — never delete or rewrite a line. Deletion/rewrite breaks "
    "the hash chain (verify_chain) AND is a Sacred-Trust breach. Honor-bound, not tamper-proof vs the "
    "pen-holder (see module docstring). No delete/rewrite API exists in fleet_orchestrator/accountability_ledger.py."
)
_GENESIS = "0" * 64


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _row_hash(prev_hash: str, event: Any, ts_str: str) -> str:
    # Hash one unambiguous, length-framed canonical object (no bare concatenation), and bind the
    # timestamp as the exact STRING that is stored, so verify recomputes byte-identically after a
    # JSON round-trip (no reliance on float repr stability across interpreters).
    payload = _canonical({"prev_hash": prev_hash, "event": event, "ts": ts_str})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _scan_last_hash(f=None, path: Optional[str] = None) -> str:
    """Scan a ledger file for the last row hash.

    Strict structure: line 1 may be the header comment; NO other comment/blank lines are allowed.
    A malformed or unexpected line is NOT silently skipped — it raises, so a wedged ledger fails
    loudly at write time rather than chaining off a stale hash (gatekeeper findings #5/#6).
    """
    if f is None:
        target_path = path or LEDGER_PATH
        if not os.path.exists(target_path):
            return _GENESIS
        with open(target_path, encoding="utf-8") as existing:
            return _scan_last_hash(existing)
    if path is not None:
        raise ValueError("pass either an open ledger handle or path, not both")
    f.seek(0)
    last = _GENESIS
    for i, raw in enumerate(f, 1):
        line = raw.rstrip("\n")
        if i == 1 and line.startswith("#"):
            continue
        if not line.strip():
            continue  # tolerate only a trailing newline; verify_chain enforces strictly
        row = json.loads(line)  # raises on garbage -> append refuses to extend a corrupt ledger
        last = row["hash"]
    return last


def append(event: Dict[str, Any], ts: Optional[float] = None, path: Optional[str] = None) -> Dict[str, Any]:
    """Append ONE event (append-only, exclusively locked, fsync'd). Returns the written row.

    The whole read-tail -> compute -> write is done under an exclusive flock so concurrent appends
    cannot fork the chain (gatekeeper finding #4). fsync guarantees the row survives a crash
    (finding #9). There is no delete/rewrite path.
    """
    target_path = path or LEDGER_PATH
    d = os.path.dirname(target_path)
    if d:
        os.makedirs(d, exist_ok=True)
    # a+ creates if missing and forces every write to EOF; one handle, one lock, for the full RMW.
    with open(target_path, "a+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            # Decide the header-write from the LIVE on-disk size while holding the lock — NOT from
            # f.tell() (a stale per-handle position). On a cold-start race, N procs each open a+ on a
            # size-0 file and each saw tell()==0, so each wrote a duplicate _HEADER mid-file via
            # O_APPEND and corrupted the chain (gatekeeper BLOCK a454603, observed repro). fstat under
            # the lock is idempotent across racing initializers: only the proc that observes a truly
            # empty file writes the header; every later lock-holder sees size>0 and skips it.
            if os.fstat(f.fileno()).st_size == 0:
                f.write(_HEADER + "\n")
                f.flush()
            prev = _scan_last_hash(f)
            ts_str = repr(float(ts) if ts is not None else time.time())
            row: Dict[str, Any] = {"ts": ts_str, "prev_hash": prev, "event": event}
            row["hash"] = _row_hash(prev, event, ts_str)
            f.write(_canonical(row) + "\n")
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return row


def verify_chain(path: Optional[str] = None) -> Dict[str, Any]:
    """Walk the chain, recompute every hash, enforce strict structure.

    Detects: a rewritten row (hash mismatch), a deleted/reordered interior row (prev_hash mismatch),
    an unparseable line, and any unexpected comment/blank line (only line 1 may be the header).
    HONEST LIMIT: it CANNOT detect tail-truncation or a full re-chain by the pen-holder — there is no
    external length/tip commitment here by design (see module docstring). It proves internal
    consistency + structural integrity, not completeness against a determined rewriter.
    """
    target_path = path or LEDGER_PATH
    if not os.path.exists(target_path):
        return {"ok": True, "rows": 0}
    prev = _GENESIS
    n = 0
    with open(target_path, encoding="utf-8") as f:
        for i, raw in enumerate(f, 1):
            line = raw.rstrip("\n")
            if i == 1 and line.startswith("#"):
                continue
            if not line.strip():
                return {"ok": False, "rows": n, "broken_at": i, "reason": "unexpected blank line"}
            if line.startswith("#"):
                return {"ok": False, "rows": n, "broken_at": i,
                        "reason": "unexpected comment line (only line 1 may be the header)"}
            try:
                row = json.loads(line)
            except Exception:
                return {"ok": False, "rows": n, "broken_at": i, "reason": "unparseable line"}
            if not isinstance(row, dict) or "hash" not in row or "prev_hash" not in row:
                return {"ok": False, "rows": n, "broken_at": i, "reason": "malformed row shape"}
            if row.get("prev_hash") != prev:
                return {"ok": False, "rows": n, "broken_at": i,
                        "reason": "prev_hash mismatch — a row was deleted or reordered"}
            expect = _row_hash(prev, row.get("event"), row.get("ts"))
            if row.get("hash") != expect:
                return {"ok": False, "rows": n, "broken_at": i,
                        "reason": "hash mismatch — a row was rewritten"}
            prev = row["hash"]
            n += 1
    return {"ok": True, "rows": n}


def _require_non_empty_string(name: str, value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} is required")
    return value.strip()


def _normalize_duration(gate: str, value: Any) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"duration_s for gate {gate!r} must be a number")
    duration = float(value)
    if not math.isfinite(duration) or duration < 0:
        raise ValueError(f"duration_s for gate {gate!r} must be finite and non-negative")
    return duration


def _duration_by_gate(durations: Any) -> Dict[str, float]:
    if not durations:
        raise ValueError("durations are required; refusing to synthesize merge ledger durations")
    result: Dict[str, float] = {}
    if isinstance(durations, Mapping):
        items = durations.items()
    elif isinstance(durations, Sequence) and not isinstance(durations, (str, bytes)):
        items = []
        for item in durations:
            if not isinstance(item, Mapping):
                raise ValueError("duration entries must be objects with gate and duration_s")
            gate = _require_non_empty_string("duration gate", item.get("gate"))
            if "duration_s" not in item:
                raise ValueError(f"duration_s for gate {gate!r} is required")
            items.append((gate, item.get("duration_s")))
    else:
        raise ValueError("durations must be a gate->seconds mapping or a list of duration objects")
    for raw_gate, raw_duration in items:
        gate = _require_non_empty_string("duration gate", raw_gate)
        if gate in result:
            raise ValueError(f"duplicate duration for gate {gate!r}")
        result[gate] = _normalize_duration(gate, raw_duration)
    return result


def _normalize_merge_gate_results(gate_results: Any, durations: Any) -> list[Dict[str, Any]]:
    if (
        not isinstance(gate_results, Sequence)
        or isinstance(gate_results, (str, bytes))
        or not gate_results
    ):
        raise ValueError("gate_results are required; refusing to write a partial merge row")
    duration_lookup = _duration_by_gate(durations)
    normalized: list[Dict[str, Any]] = []
    seen: set[str] = set()
    for item in gate_results:
        if not isinstance(item, Mapping):
            raise ValueError("gate_results entries must be objects with gate and verdict")
        gate = _require_non_empty_string("gate_results[].gate", item.get("gate"))
        if gate in seen:
            raise ValueError(f"duplicate gate result for {gate!r}")
        verdict = _require_non_empty_string(f"verdict for gate {gate!r}", item.get("verdict"))
        if gate not in duration_lookup:
            raise ValueError(f"duration_s for gate {gate!r} is required")
        duration_s = duration_lookup[gate]
        if "duration_s" in item:
            embedded = _normalize_duration(gate, item.get("duration_s"))
            if embedded != duration_s:
                raise ValueError(f"duration_s mismatch for gate {gate!r}")
        normalized.append({"gate": gate, "verdict": verdict, "duration_s": duration_s})
        seen.add(gate)
    extra_durations = sorted(set(duration_lookup) - seen)
    if extra_durations:
        raise ValueError(f"durations provided for gates with no result: {', '.join(extra_durations)}")
    return normalized


def record_merge(
    repo: str,
    sha: str,
    gate_results: Any,
    durations: Any,
    merged_by: str,
    ts: Optional[float] = None,
    path: Optional[str] = None,
) -> Dict[str, Any]:
    """Append a verified merge event to the CI audit ledger.

    Gate results and per-gate durations must be supplied by the caller. This function deliberately
    refuses to infer or synthesize missing data because a signed row with fabricated merge evidence
    is worse than no row.
    """
    repo_value = _require_non_empty_string("repo", repo)
    sha_value = _require_non_empty_string("sha", sha)
    merged_by_value = _require_non_empty_string("merged_by", merged_by)
    normalized_results = _normalize_merge_gate_results(gate_results, durations)
    event_time = float(ts) if ts is not None else time.time()
    event = {
        "type": "merge",
        "repo": repo_value,
        "sha": sha_value,
        "gate_results": normalized_results,
        "total_duration_s": sum(item["duration_s"] for item in normalized_results),
        "merged_by": merged_by_value,
        "ts": repr(event_time),
    }
    return append(event, ts=event_time, path=path or CI_AUDIT_PATH)


def _parse_gate_result_arg(raw: str) -> Dict[str, Any]:
    parts = raw.split("=", 2)
    if len(parts) != 3:
        raise argparse.ArgumentTypeError("gate result must be GATE=VERDICT=DURATION_S")
    gate, verdict, duration_raw = parts
    try:
        duration = float(duration_raw)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("DURATION_S must be numeric") from exc
    return {"gate": gate, "verdict": verdict, "duration_s": duration}


def _load_gate_results_json(path: str) -> list[Dict[str, Any]]:
    with open(path, encoding="utf-8") as f:
        payload = json.load(f)
    if isinstance(payload, Mapping):
        payload = payload.get("gate_results")
    if not isinstance(payload, list):
        raise ValueError("--gate-results-json must contain a list or an object with gate_results")
    return payload


def _cli_gate_results(args: argparse.Namespace) -> tuple[list[Dict[str, Any]], Dict[str, float]]:
    raw_results: list[Dict[str, Any]] = []
    if args.gate_results_json:
        raw_results.extend(_load_gate_results_json(args.gate_results_json))
    raw_results.extend(args.gate_result or [])
    if not raw_results:
        raise ValueError("at least one --gate-result or --gate-results-json entry is required")
    gate_results: list[Dict[str, Any]] = []
    durations: Dict[str, float] = {}
    for item in raw_results:
        if not isinstance(item, Mapping):
            raise ValueError("gate result entries must be objects")
        gate = _require_non_empty_string("gate", item.get("gate"))
        if "duration_s" not in item:
            raise ValueError(f"duration_s for gate {gate!r} is required")
        gate_results.append({"gate": gate, "verdict": item.get("verdict")})
        durations[gate] = _normalize_duration(gate, item.get("duration_s"))
    return gate_results, durations


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Append and verify hash-chained accountability ledgers.")
    sub = parser.add_subparsers(dest="command", required=True)

    merge = sub.add_parser("record-merge", help="Append a completed CONTROL merge to ci-audit.jsonl.")
    merge.add_argument("--repo", required=True, help="Merged repository as OWNER/REPO.")
    merge.add_argument("--sha", required=True, help="Merged commit SHA.")
    merge.add_argument("--merged-by", required=True, help="Actor who performed the merge.")
    merge.add_argument(
        "--gate-result",
        action="append",
        type=_parse_gate_result_arg,
        help="Repeatable gate result in GATE=VERDICT=DURATION_S form.",
    )
    merge.add_argument("--gate-results-json", help="JSON list of {gate, verdict, duration_s} objects.")
    merge.add_argument("--path", default=CI_AUDIT_PATH, help="CI audit ledger path; defaults to ACCOUNTABILITY_CI_AUDIT_PATH.")
    merge.add_argument("--ts", type=float, help="Optional event timestamp for deterministic imports/tests.")

    verify = sub.add_parser("verify-chain", help="Verify a ledger hash chain.")
    verify.add_argument("--path", help="Ledger path to verify.")
    verify.add_argument("--ci", action="store_true", help="Verify the CI audit ledger instead of the default accountability ledger.")

    args = parser.parse_args(argv)
    try:
        if args.command == "record-merge":
            gate_results, durations = _cli_gate_results(args)
            row = record_merge(
                args.repo,
                args.sha,
                gate_results,
                durations,
                args.merged_by,
                ts=args.ts,
                path=args.path,
            )
            print(_canonical(row))
            return 0
        if args.command == "verify-chain":
            path = args.path or (CI_AUDIT_PATH if args.ci else LEDGER_PATH)
            result = verify_chain(path=path)
            print(_canonical(result))
            return 0 if result.get("ok") is True else 1
    except Exception as exc:
        print(f"accountability-ledger: {exc}", file=sys.stderr)
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
