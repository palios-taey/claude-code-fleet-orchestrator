"""Verification-status lookup over the accountability ledger — ANTIFAB P2/P3 shared primitive.

Answers the one question `update_task_status` must ask before granting "done": for THIS task at THIS
sha, is there an independent verified PASS — a mechanical `gate_run` PASS and/or a gatekeeper
`audit_verdict` PASS — recorded in the ledger by something OTHER than the builder's say-so?

A broken ledger chain means the record itself is untrustworthy, so it forces verified=False (you
cannot ride a tampered ledger to a "done"). This module only READS the ledger.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Iterator

from lib.accountability_ledger import LEDGER_PATH, verify_chain

_PASS = "PASS"


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
        if ev.get("task") != task or ev.get("sha") != sha:
            continue
        etype = ev.get("type")
        if etype == "gate_run":
            gate_run = ev.get("result")
        elif etype == "audit_verdict" and str(ev.get("reviewer", "")).startswith("gatekeeper"):
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
