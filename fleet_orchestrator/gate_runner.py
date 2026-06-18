"""Clean-room verification gate runner — ANTIFAB P1.

The orchestrator runs the gate ITSELF: fresh detached checkout of the EXACT sha -> gate.clean (setup)
-> gate.boot (start the component) -> gate.assert (a live read-after-write). The verdict is derived
ONLY from what this runner observed by executing those commands — never from a builder-supplied value.
A `gate_run` row is appended to the accountability ledger. The builder is never asked "did it pass?"

Trust boundary: the builder/operator declares the gate command strings (WHAT to check, e.g.
`curl -fsS :PORT/x | grep y`); the runner executes them through the local shell and records the
result. Honest limit: the runner trusts the gate DEFINITION (a builder could declare a vacuous
`assert: true`) — gate-definition quality is the gatekeeper's (P4) judgment review, not something
this mechanical runner can decide. This is the accepted local-tool risk documented in SECURITY.md;
if gate definitions ever come from untrusted input, shell execution must be replaced by structured
commands and/or sandboxing. What this closes is "marked done without anything being run": here,
something independent of the builder ran it and recorded the proof.
"""
from __future__ import annotations

import hashlib
import os
import shlex
import shutil
import subprocess
import tempfile
from typing import Any, Dict, Optional

from fleet_orchestrator.accountability_ledger import append as ledger_append
from fleet_orchestrator.paths import repo_root

# No operator-specific default: explicit override wins, else the install root (the repo
# this orchestrator is running from), so gates target a real repo on any user's machine.
DEFAULT_REPO = os.environ.get("ORCH_GATE_REPO") or str(repo_root())
_EMPTY = (None, "", "-")


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def _run(cmd: Optional[str], cwd: str, timeout: int) -> tuple[int, str]:
    """Run a shell command, returning (exit_code, combined_output). A blank/'-' command is a no-op."""
    if cmd is None or cmd.strip() in _EMPTY:
        return 0, ""
    try:
        p = subprocess.run(cmd, shell=True, cwd=cwd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, f"TIMEOUT after {timeout}s: {cmd}"


def run_gate(
    task: str,
    sha: str,
    *,
    clean: Optional[str] = None,
    boot: Optional[str] = None,
    assert_cmd: Optional[str] = None,
    repo: str = DEFAULT_REPO,
    timeout: int = 600,
) -> Dict[str, Any]:
    """Clean-room run of one task's gate at one sha. `assert_cmd` is the production oracle: PASS iff it
    exits 0. Returns the verdict dict (also appended to the ledger). No gate.assert -> INDETERMINATE
    (the judgment gate / gatekeeper is then required), never PASS."""
    if assert_cmd is None or assert_cmd.strip() in _EMPTY:
        v = {"type": "gate_run", "task": task, "sha": sha, "result": "INDETERMINATE",
             "reason": "no gate.assert defined — judgment gate (gatekeeper) required"}
        ledger_append(v)
        return v

    work = tempfile.mkdtemp(prefix=f"gate-{task}-")
    wt = os.path.join(work, "tree")
    artifacts: Dict[str, str] = {}

    def _verdict(result: str, stage: str, rc: int, out: str) -> Dict[str, Any]:
        artifacts[stage] = _sha256(out)
        v = {"type": "gate_run", "task": task, "sha": sha, "result": result, "stage": stage,
             "exit": rc, "artifacts": dict(artifacts), "detail": out[-600:]}
        ledger_append(v)
        return v

    try:
        rc, out = _run(
            f"git -C {shlex.quote(repo)} worktree add --detach {shlex.quote(wt)} -- {shlex.quote(sha)}",
            repo, 120)
        if rc != 0:
            return _verdict("FAIL", "checkout", rc, out)
        artifacts["checkout"] = _sha256(out)

        rc, out = _run(clean, wt, timeout)
        if rc != 0:
            return _verdict("FAIL", "clean", rc, out)
        artifacts["clean"] = _sha256(out)

        rc, out = _run(boot, wt, timeout)
        if rc != 0:
            return _verdict("FAIL", "boot", rc, out)
        artifacts["boot"] = _sha256(out)

        # the production oracle — a live read-after-write; PASS iff it exits 0
        rc, out = _run(assert_cmd, wt, timeout)
        return _verdict("PASS" if rc == 0 else "FAIL", "assert", rc, out)
    finally:
        _run(f"git -C {shlex.quote(repo)} worktree remove --force {shlex.quote(wt)}", repo, 60)
        shutil.rmtree(work, ignore_errors=True)
