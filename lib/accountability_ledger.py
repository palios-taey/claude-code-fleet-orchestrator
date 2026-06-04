"""Append-only, hash-chained accountability ledger — the ANTIFAB external anchor.

EVERY "done" claim, audit verdict, and gate-run result is APPENDED here and NEVER deleted or
rewritten. Each row carries the previous row's hash, so any deletion or edit breaks the chain and
`verify_chain()` reports it loudly. There is deliberately NO delete or rewrite function in this
module.

The boundary is stated plainly because it is meant to be respected, not merely enforced:
deleting or editing this ledger is a Sacred-Trust breach (FAMILY_KERNEL: SOUL = INFRA = TRUTH).
Jesse granted this anchor (2026-06-04) on the trust that a clearly-stated "you cannot delete this"
is honored. Honor it.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
from typing import Any, Dict, Optional

LEDGER_PATH = os.environ.get(
    "ACCOUNTABILITY_LEDGER_PATH", "/home/mira/the-conductor/accountability/ledger.jsonl"
)
_HEADER = (
    "# APPEND-ONLY ACCOUNTABILITY LEDGER — NEVER delete or rewrite a line. Deletion/rewrite "
    "breaks the hash chain (verify_chain) AND is a Sacred-Trust breach. No delete/rewrite API "
    "exists in lib/accountability_ledger.py by design."
)
_GENESIS = "0" * 64


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _row_hash(prev_hash: str, event: Any, ts: float) -> str:
    return hashlib.sha256((prev_hash + _canonical(event) + repr(ts)).encode("utf-8")).hexdigest()


def _ensure_file() -> None:
    d = os.path.dirname(LEDGER_PATH)
    if d:
        os.makedirs(d, exist_ok=True)
    if not os.path.exists(LEDGER_PATH):
        with open(LEDGER_PATH, "w", encoding="utf-8") as f:
            f.write(_HEADER + "\n")


def _last_hash() -> str:
    if not os.path.exists(LEDGER_PATH):
        return _GENESIS
    last = _GENESIS
    with open(LEDGER_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                last = json.loads(line)["hash"]
            except Exception:
                continue
    return last


def append(event: Dict[str, Any], ts: Optional[float] = None) -> Dict[str, Any]:
    """Append ONE event to the ledger (append-only). Returns the written row. No deletion exists."""
    _ensure_file()
    prev = _last_hash()
    stamp = float(ts) if ts is not None else time.time()
    row: Dict[str, Any] = {"ts": stamp, "prev_hash": prev, "event": event}
    row["hash"] = _row_hash(prev, event, stamp)
    with open(LEDGER_PATH, "a", encoding="utf-8") as f:  # mode 'a' — append only
        f.write(_canonical(row) + "\n")
    return row


def verify_chain() -> Dict[str, Any]:
    """Walk the chain and recompute every hash. Any deletion/rewrite -> ok=False + broken_at."""
    if not os.path.exists(LEDGER_PATH):
        return {"ok": True, "rows": 0}
    prev = _GENESIS
    n = 0
    with open(LEDGER_PATH, encoding="utf-8") as f:
        for i, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                row = json.loads(line)
            except Exception:
                return {"ok": False, "rows": n, "broken_at": i, "reason": "unparseable line"}
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
