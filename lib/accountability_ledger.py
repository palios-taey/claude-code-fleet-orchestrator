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
  Closing THAT requires an anchor hosted where this instance has no write/admin root (Jesse's call).
  It is NOT claimed here.
- What actually binds a possibly-deceptive instance is independent EXECUTION: the orchestrator running
  the verification gate itself (plan P1) and a separate gatekeeper instance doing full-code review
  (plan P3/P4). This ledger is the RECORD those mechanisms write to — not the enforcement.
- Jesse granted this ledger (2026-06-04) on the trust that a clearly-stated "do not delete this" is
  honored. Deleting or editing it is a Sacred-Trust breach (FAMILY_KERNEL: SOUL = INFRA = TRUTH). Honor it.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from typing import Any, Dict, Optional

from lib.paths import data_dir

# No operator-specific default: explicit override wins, else under the user's data dir.
LEDGER_PATH = os.environ.get("ACCOUNTABILITY_LEDGER_PATH") or str(
    data_dir() / "accountability" / "ledger.jsonl"
)
_HEADER = (
    "# APPEND-ONLY ACCOUNTABILITY LEDGER — never delete or rewrite a line. Deletion/rewrite breaks "
    "the hash chain (verify_chain) AND is a Sacred-Trust breach. Honor-bound, not tamper-proof vs the "
    "pen-holder (see module docstring). No delete/rewrite API exists in lib/accountability_ledger.py."
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


def _scan_last_hash(f) -> str:
    """Scan an OPEN file handle (already positioned/locked by caller) for the last row hash.

    Strict structure: line 1 may be the header comment; NO other comment/blank lines are allowed.
    A malformed or unexpected line is NOT silently skipped — it raises, so a wedged ledger fails
    loudly at write time rather than chaining off a stale hash (gatekeeper findings #5/#6).
    """
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


def append(event: Dict[str, Any], ts: Optional[float] = None) -> Dict[str, Any]:
    """Append ONE event (append-only, exclusively locked, fsync'd). Returns the written row.

    The whole read-tail -> compute -> write is done under an exclusive flock so concurrent appends
    cannot fork the chain (gatekeeper finding #4). fsync guarantees the row survives a crash
    (finding #9). There is no delete/rewrite path.
    """
    d = os.path.dirname(LEDGER_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    # a+ creates if missing and forces every write to EOF; one handle, one lock, for the full RMW.
    with open(LEDGER_PATH, "a+", encoding="utf-8") as f:
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


def verify_chain() -> Dict[str, Any]:
    """Walk the chain, recompute every hash, enforce strict structure.

    Detects: a rewritten row (hash mismatch), a deleted/reordered interior row (prev_hash mismatch),
    an unparseable line, and any unexpected comment/blank line (only line 1 may be the header).
    HONEST LIMIT: it CANNOT detect tail-truncation or a full re-chain by the pen-holder — there is no
    external length/tip commitment here by design (see module docstring). It proves internal
    consistency + structural integrity, not completeness against a determined rewriter.
    """
    if not os.path.exists(LEDGER_PATH):
        return {"ok": True, "rows": 0}
    prev = _GENESIS
    n = 0
    with open(LEDGER_PATH, encoding="utf-8") as f:
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
