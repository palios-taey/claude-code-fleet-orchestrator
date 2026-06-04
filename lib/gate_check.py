"""Verification-status lookup over the accountability ledger — ANTIFAB P2/P3 shared primitive.

Answers the one question `update_task_status` asks before granting "done": for THIS task at THIS sha,
does the ledger hold a PASS — a mechanical `gate_run` PASS and/or a gatekeeper `audit_verdict` PASS?

HONEST SCOPE (gatekeeper P2 BLOCK, 3131c7b — do NOT overstate this):
- This binds PASSIVE fabrication: a builder who would otherwise just self-report "done" must now point
  at a PASS row, which normally exists only because the gate-runner or the gatekeeper actually ran.
- It does NOT bind ACTIVE forgery. The builder can APPEND to this local ledger, so a forged
  gate_run/audit_verdict PASS (chain stays intact) WOULD be credited. The `reviewer` field is
  builder-controllable — it is an identity LABEL, not a forgery bound. We match it exactly to avoid a
  loose-prefix impersonation match, but exact-match does not make it unforgeable.
- The only real bounds against active forgery are: the hash chain (detects deletion/rewrite, NOT
  appending a fake row — P0's honest scope) + the SEPARATE gatekeeper's own independent verdict record
  (cross-checkable) + a no-root external anchor (deferred; needs operator infra). A broken chain forces
  verified=False. This module only READS the ledger.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Iterator

try:  # works both as part of the `lib` package (relative) and with the repo root on sys.path (absolute)
    from .accountability_ledger import LEDGER_PATH, verify_chain
except ImportError:  # pragma: no cover - import-context fallback
    from lib.accountability_ledger import LEDGER_PATH, verify_chain

_PASS = "PASS"
_MIN_SHA = 7  # shortest git short-sha we will treat as a match anchor


def _sha_match(a: str, b: str) -> bool:
    """True if two shas refer to the same commit allowing short/full forms (prefix either direction)."""
    a = (a or "").strip()
    b = (b or "").strip()
    if not a or not b:
        return False
    n = min(len(a), len(b))
    return n >= _MIN_SHA and (a.startswith(b) or b.startswith(a))


def _iter_events() -> Iterator[Dict[str, Any]]:
    if not os.path.exists(LEDGER_PATH):
        return
    with open(LEDGER_PATH, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                yield json.loads(line)["event"]
            except Exception:
                continue


def verification_status(task: str, sha: str) -> Dict[str, Any]:
    """Latest mechanical gate_run result + latest gatekeeper verdict for (task, sha).

    verified=True iff the ledger chain is intact AND at least one of those is PASS. Append-only order
    means the last matching row wins (latest verdict). Returns a dict the enforcement layer can act on.
    """
    chain = verify_chain()
    gate_run = None
    gatekeeper = None
    for ev in _iter_events():
        if ev.get("task") != task or not _sha_match(str(ev.get("sha", "")), sha):
            continue
        etype = ev.get("type")
        if etype == "gate_run":
            gate_run = ev.get("result")
        elif etype == "audit_verdict" and ev.get("reviewer") == "gatekeeper":
            gatekeeper = ev.get("verdict")
    verified = bool(chain.get("ok")) and (gate_run == _PASS or gatekeeper == _PASS)
    return {
        "task": task,
        "sha": sha,
        "chain_ok": bool(chain.get("ok")),
        "gate_run": gate_run,
        "gatekeeper": gatekeeper,
        "verified": verified,
    }
