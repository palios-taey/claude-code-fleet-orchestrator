#!/usr/bin/env python3
"""Isolated fake-store/provider acceptance for two-phase audit completion (task-05a27e83).

No live Neo4j / Redis / GitHub mutation. Production-shaped adversarial probes:
  - ordinary creator cannot select audit pins on create
  - self-binder (non-supervisor) cannot bind status ID
  - forged substring receipt rejected (structured refs.json only)
  - unlisted semantic file rejected (refs.json must be in SHA256SUMS)
  - full supervisor pin → server-side PR query bind → sealed complete lifecycle
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fleet_orchestrator.audit_completion import (  # noqa: E402
    AuditContractError,
    assert_actor_is_project_supervisor,
    assert_no_audit_override_in_evidence,
    compare_once_bind_status,
    is_audit_task,
    normalize_supervisor_pins,
    reject_ordinary_create_audit_fields,
    set_audit_pull_provider,
    set_audit_status_provider,
    verify_audit_completion,
    verify_sealed_audit_receipt,
)
from fleet_orchestrator.evidence_verification import (  # noqa: E402
    VERIFIED,
    verify_completion_evidence,
)

REPO = "palios-taey/claude-code-fleet-orchestrator"
HEAD = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
BASE = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
WRONG_HEAD = "cccccccccccccccccccccccccccccccccccccccc"
CONTEXT = "audit/grok"
STATE = "success"
STATUS_ID = 52579906897
WRONG_ID = 11111111111
PR_NUMBER = 345
SUPERVISOR = "conductor-codex"
WORKER = "conductor-grok"

FAILURES: List[str] = []


def _check(label: str, cond: bool, detail: object = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {detail}"))
    if not cond:
        FAILURES.append(f"{label}: {detail}")


def _expect_error(label: str, fn, *, substr: str) -> None:
    try:
        fn()
    except AuditContractError as exc:
        msg = str(exc)
        _check(label, substr.lower() in msg.lower(), msg)
        return
    except Exception as exc:  # noqa: BLE001
        _check(label, False, f"wrong exception type {type(exc).__name__}: {exc}")
        return
    _check(label, False, "expected AuditContractError")


class FakeTaskStore:
    """In-memory OrchTask store mirroring supervisor pin → bind → complete."""

    def __init__(self, *, project_supervisor: str) -> None:
        self.project_supervisor = project_supervisor
        self._tasks: Dict[str, Dict[str, Any]] = {}

    def create_ordinary(self, task_id: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        reject_ordinary_create_audit_fields(payload or {})
        row = {
            "id": task_id,
            "status": "in_progress",
            "completion_class": "standard",
            "audit_repo": None,
            "audit_head": None,
            "audit_base": None,
            "audit_required_context": None,
            "audit_required_state": None,
            "audit_pr_number": None,
            "audit_bound_status_id": None,
            "project_supervisor": self.project_supervisor,
        }
        self._tasks[task_id] = row
        return dict(row)

    def get(self, task_id: str) -> Dict[str, Any]:
        return dict(self._tasks[task_id])

    def pin(
        self,
        task_id: str,
        *,
        actor: str,
        audit_repo: str,
        audit_head: str,
        audit_base: str,
        audit_required_context: str,
        audit_required_state: str,
        audit_pr_number: int,
    ) -> Dict[str, Any]:
        task = self._tasks[task_id]
        assert_actor_is_project_supervisor(
            actor=actor,
            project_supervisor=task.get("project_supervisor"),
            action="pin-audit-contract",
        )
        pins = normalize_supervisor_pins(
            audit_repo=audit_repo,
            audit_head=audit_head,
            audit_base=audit_base,
            audit_required_context=audit_required_context,
            audit_required_state=audit_required_state,
            audit_pr_number=audit_pr_number,
        )
        if task.get("completion_class") == "audit":
            for key, value in pins.items():
                if task.get(key) != value and key != "audit_bound_status_id":
                    raise AuditContractError(
                        f"trusted audit pins are immutable after creation; refuse overwrite of {key}"
                    )
            return {"already_pinned": True, **pins}
        task.update(pins)
        return {"already_pinned": False, **pins}

    def bind(self, task_id: str, *, actor: str, status_id: int) -> Dict[str, Any]:
        task = self._tasks[task_id]
        assert_actor_is_project_supervisor(
            actor=actor,
            project_supervisor=task.get("project_supervisor"),
            action="bind-audit-status",
        )
        bind = compare_once_bind_status(task, status_id=status_id)
        if not bind.get("already_bound"):
            if task.get("audit_bound_status_id") is not None:
                raise AuditContractError("compare-once refuses overwrite of bound status id")
            task["audit_bound_status_id"] = int(status_id)
        return bind

    def complete(self, task_id: str, evidence: Dict[str, Any], *, producer: str = "tester") -> Dict[str, Any]:
        task = self._tasks[task_id]
        assert_no_audit_override_in_evidence(evidence)
        result = verify_completion_evidence(
            evidence,
            producer=producer,
            trusted_task=task,
        )
        if not isinstance(result, dict):
            raise AuditContractError("verifier returned no result")
        if result.get("reject_completion") or result.get("status") != VERIFIED:
            raise AuditContractError(str(result.get("reason") or "completion rejected"))
        task["status"] = "completed"
        task["completion_evidence"] = dict(evidence)
        task["completion_evidence_verification"] = result
        return result


def _seal_receipt(
    root: Path,
    *,
    repo: str,
    head: str,
    base: str,
    context: str,
    state: str,
    status_id: int,
    pr_number: int,
    include_refs_in_sums: bool = True,
    forge_substring_only: bool = False,
) -> str:
    root.mkdir(parents=True, exist_ok=True)
    refs = {
        "audit_repo": repo,
        "audit_head": head,
        "audit_base": base,
        "audit_required_context": context,
        "audit_required_state": state,
        "audit_bound_status_id": status_id,
        "audit_pr_number": pr_number,
    }
    if forge_substring_only:
        # Free-text looks correct but refs.json has wrong structured values.
        refs["audit_head"] = WRONG_HEAD
        refs["audit_base"] = WRONG_HEAD
        refs["audit_bound_status_id"] = WRONG_ID
    (root / "refs.json").write_text(json.dumps(refs, indent=2) + "\n", encoding="utf-8")
    verdict = (
        f"ENDORSE\naudit_repo={repo}\naudit_head={head}\naudit_base={base}\n"
        f"audit_required_context={context}\naudit_required_state={state}\n"
        f"audit_bound_status_id={status_id}\naudit_pr_number={pr_number}\n"
    )
    (root / "verdict-receipt.txt").write_text(verdict, encoding="utf-8")
    names = ["verdict-receipt.txt"]
    if include_refs_in_sums:
        names.insert(0, "refs.json")
    lines = []
    for name in names:
        digest = hashlib.sha256((root / name).read_bytes()).hexdigest()
        lines.append(f"{digest}  {name}")
    (root / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for path in root.rglob("*"):
        if path.is_dir():
            os.chmod(path, 0o555)
        elif path.is_file():
            os.chmod(path, 0o444)
    os.chmod(root, 0o555)
    return str(root)


def main() -> int:
    print("=== audit_completion_two_phase_acceptance (supervisor authority + structured receipt) ===")
    store = FakeTaskStore(project_supervisor=SUPERVISOR)
    good_statuses = [
        {"id": STATUS_ID, "context": CONTEXT, "state": STATE},
        {"id": WRONG_ID, "context": "audit/other", "state": "failure"},
    ]
    set_audit_status_provider(
        lambda repo, sha: list(good_statuses) if repo == REPO and sha == HEAD else []
    )
    set_audit_pull_provider(
        lambda repo, pr: {
            "number": pr,
            "head_sha": HEAD if repo == REPO and pr == PR_NUMBER else WRONG_HEAD,
            "base_sha": BASE if repo == REPO and pr == PR_NUMBER else WRONG_HEAD,
        }
    )

    # --- Ordinary create cannot select audit pins ---
    print("-- ordinary create rejects audit fields --")
    _expect_error(
        "ordinary create rejects completion_class=audit",
        lambda: reject_ordinary_create_audit_fields({"completion_class": "audit", "audit_repo": REPO}),
        substr="cannot select audit contract",
    )
    _expect_error(
        "ordinary create rejects audit_head",
        lambda: reject_ordinary_create_audit_fields({"audit_head": HEAD}),
        substr="cannot select audit contract",
    )
    _expect_error(
        "ordinary create rejects status id",
        lambda: reject_ordinary_create_audit_fields({"audit_bound_status_id": STATUS_ID}),
        substr="cannot select audit contract",
    )
    ordinary = store.create_ordinary("t-audit", {"description": "x", "from": WORKER})
    _check("ordinary create is standard class", ordinary["completion_class"] == "standard")
    _check("ordinary create has no bound id", ordinary["audit_bound_status_id"] is None)

    # --- Supervisor pin (phase 1) ---
    print("-- supervisor pin authority --")
    _expect_error(
        "worker cannot pin audit contract",
        lambda: store.pin(
            "t-audit",
            actor=WORKER,
            audit_repo=REPO,
            audit_head=HEAD,
            audit_base=BASE,
            audit_required_context=CONTEXT,
            audit_required_state=STATE,
            audit_pr_number=PR_NUMBER,
        ),
        substr="not project supervisor",
    )
    _expect_error(
        "empty actor cannot pin",
        lambda: store.pin(
            "t-audit",
            actor="",
            audit_repo=REPO,
            audit_head=HEAD,
            audit_base=BASE,
            audit_required_context=CONTEXT,
            audit_required_state=STATE,
            audit_pr_number=PR_NUMBER,
        ),
        substr="authenticated supervisor",
    )
    pin = store.pin(
        "t-audit",
        actor=SUPERVISOR,
        audit_repo=REPO,
        audit_head=HEAD,
        audit_base=BASE,
        audit_required_context=CONTEXT,
        audit_required_state=STATE,
        audit_pr_number=PR_NUMBER,
    )
    _check("supervisor pin sets class=audit", pin["completion_class"] == "audit")
    _check("supervisor pin leaves status id unset", pin["audit_bound_status_id"] is None)
    _check("supervisor pin sets pr number", pin["audit_pr_number"] == PR_NUMBER)
    _check("is_audit_task true after pin", is_audit_task(store.get("t-audit")))
    _check("missing class is not audit", not is_audit_task({"audit_head": HEAD}))

    _expect_error(
        "refuse pin overwrite",
        lambda: store.pin(
            "t-audit",
            actor=SUPERVISOR,
            audit_repo=REPO,
            audit_head=WRONG_HEAD,
            audit_base=BASE,
            audit_required_context=CONTEXT,
            audit_required_state=STATE,
            audit_pr_number=PR_NUMBER,
        ),
        substr="immutable",
    )

    # --- Evidence cannot select/overwrite ---
    print("-- evidence cannot select/overwrite --")
    _expect_error(
        "evidence cannot set completion_class",
        lambda: assert_no_audit_override_in_evidence({"completion_class": "audit"}),
        substr="cannot select or overwrite",
    )
    _expect_error(
        "evidence cannot set audit_pr_number",
        lambda: assert_no_audit_override_in_evidence({"audit_pr_number": PR_NUMBER}),
        substr="cannot select or overwrite",
    )
    v = verify_completion_evidence(
        {"audit_receipt": "/home/mira/recovery/r5-audit/example"},
        producer="attacker",
        trusted_task={"completion_class": "standard"},
    )
    _check(
        "standard task cannot self-select audit_receipt",
        isinstance(v, dict) and v.get("reject_completion") is True,
        v,
    )

    # --- Bind: supervisor only; server-side PR; no caller PR SHAs ---
    print("-- compare-once bind (supervisor + server-side PR) --")
    _expect_error(
        "self-binder (worker) cannot bind",
        lambda: store.bind("t-audit", actor=WORKER, status_id=STATUS_ID),
        substr="not project supervisor",
    )
    _expect_error(
        "bind rejects wrong status id/context",
        lambda: store.bind("t-audit", actor=SUPERVISOR, status_id=WRONG_ID),
        substr="context mismatch",
    )
    # Wrong PR pin would fail server-side provenance — pin a decoy task
    store.create_ordinary("t-bad-pr")
    store.pin(
        "t-bad-pr",
        actor=SUPERVISOR,
        audit_repo=REPO,
        audit_head=HEAD,
        audit_base=BASE,
        audit_required_context=CONTEXT,
        audit_required_state=STATE,
        audit_pr_number=999,
    )
    _expect_error(
        "bind rejects server-side PR mismatch",
        lambda: store.bind("t-bad-pr", actor=SUPERVISOR, status_id=STATUS_ID),
        substr="PR provenance mismatch",
    )

    bind = store.bind("t-audit", actor=SUPERVISOR, status_id=STATUS_ID)
    _check("bind writes concrete status id", bind["audit_bound_status_id"] == STATUS_ID)
    _check("bind echoes server-side pr number", bind.get("pr_number") == PR_NUMBER)
    again = store.bind("t-audit", actor=SUPERVISOR, status_id=STATUS_ID)
    _check("bind same id is idempotent", again.get("already_bound") is True)
    _expect_error(
        "bind different id refuses overwrite",
        lambda: store.bind("t-audit", actor=SUPERVISOR, status_id=WRONG_ID),
        substr="refuses overwrite",
    )

    unbound = store.create_ordinary("t-unbound")
    store.pin(
        "t-unbound",
        actor=SUPERVISOR,
        audit_repo=REPO,
        audit_head=HEAD,
        audit_base=BASE,
        audit_required_context=CONTEXT,
        audit_required_state=STATE,
        audit_pr_number=PR_NUMBER,
    )
    _expect_error(
        "complete without prior bind rejected",
        lambda: store.complete("t-unbound", {"audit_receipt": "/home/mira/recovery/r5-audit/x"}),
        substr="compare-once bind",
    )

    # --- Sealed receipt adversarial ---
    print("-- sealed receipt structured provenance --")
    with tempfile.TemporaryDirectory(prefix="audit-receipt-", dir="/home/mira/recovery") as td:
        # Symlink escape
        real = Path(td) / "real"
        link = Path(td) / "link"
        real.mkdir()
        link.symlink_to(real)
        _seal_receipt(
            real,
            repo=REPO,
            head=HEAD,
            base=BASE,
            context=CONTEXT,
            state=STATE,
            status_id=STATUS_ID,
            pr_number=PR_NUMBER,
        )
        _expect_error(
            "symlink component rejected",
            lambda: verify_sealed_audit_receipt(
                str(link),
                expected_repo=REPO,
                expected_head=HEAD,
                expected_base=BASE,
                expected_context=CONTEXT,
                expected_state=STATE,
                expected_status_id=STATUS_ID,
                expected_pr_number=PR_NUMBER,
            ),
            substr="symlink",
        )

        # Unlisted semantic file: refs.json present on disk but NOT in SHA256SUMS
        unlisted = Path(td) / "unlisted"
        unlisted_path = _seal_receipt(
            unlisted,
            repo=REPO,
            head=HEAD,
            base=BASE,
            context=CONTEXT,
            state=STATE,
            status_id=STATUS_ID,
            pr_number=PR_NUMBER,
            include_refs_in_sums=False,
        )
        _expect_error(
            "unlisted refs.json rejected",
            lambda: verify_sealed_audit_receipt(
                unlisted_path,
                expected_repo=REPO,
                expected_head=HEAD,
                expected_base=BASE,
                expected_context=CONTEXT,
                expected_state=STATE,
                expected_status_id=STATUS_ID,
                expected_pr_number=PR_NUMBER,
            ),
            substr="refs.json",
        )

        # Forged substring receipt: verdict free-text matches pins, refs.json does not
        forged = Path(td) / "forged"
        forged_path = _seal_receipt(
            forged,
            repo=REPO,
            head=HEAD,
            base=BASE,
            context=CONTEXT,
            state=STATE,
            status_id=STATUS_ID,
            pr_number=PR_NUMBER,
            forge_substring_only=True,
        )
        _expect_error(
            "forged substring receipt rejected (structured mismatch)",
            lambda: verify_sealed_audit_receipt(
                forged_path,
                expected_repo=REPO,
                expected_head=HEAD,
                expected_base=BASE,
                expected_context=CONTEXT,
                expected_state=STATE,
                expected_status_id=STATUS_ID,
                expected_pr_number=PR_NUMBER,
            ),
            substr="mismatch",
        )

        _expect_error(
            "path outside recovery rejected",
            lambda: verify_sealed_audit_receipt(
                "/tmp/not-recovery",
                expected_repo=REPO,
                expected_head=HEAD,
                expected_base=BASE,
                expected_context=CONTEXT,
                expected_state=STATE,
                expected_status_id=STATUS_ID,
                expected_pr_number=PR_NUMBER,
            ),
            substr="must resolve under",
        )

        good_root = Path(td) / "good"
        good_path = _seal_receipt(
            good_root,
            repo=REPO,
            head=HEAD,
            base=BASE,
            context=CONTEXT,
            state=STATE,
            status_id=STATUS_ID,
            pr_number=PR_NUMBER,
        )
        sealed_ok = verify_sealed_audit_receipt(
            good_path,
            expected_repo=REPO,
            expected_head=HEAD,
            expected_base=BASE,
            expected_context=CONTEXT,
            expected_state=STATE,
            expected_status_id=STATUS_ID,
            expected_pr_number=PR_NUMBER,
        )
        _check("good structured receipt verifies", sealed_ok.get("refs") == "refs.json")

        print("-- full successful lifecycle --")
        result = store.complete("t-audit", {"audit_receipt": good_path}, producer=SUPERVISOR)
        _check("lifecycle VERIFIED", result.get("status") == VERIFIED, result)
        _check("lifecycle applies", result.get("applies") is True)
        _check("lifecycle bound id echoed", result.get("audit_bound_status_id") == STATUS_ID)
        _check("lifecycle pr echoed", result.get("audit_pr_number") == PR_NUMBER)
        _check("task status completed", store.get("t-audit")["status"] == "completed")

        via = verify_completion_evidence(
            {"audit_receipt": good_path},
            producer=SUPERVISOR,
            trusted_task=store.get("t-audit"),
        )
        _check("verify_completion_evidence trusted path VERIFIED", via and via.get("status") == VERIFIED, via)

        direct = verify_audit_completion(
            store.get("t-audit"),
            {"audit_receipt": good_path},
            producer=SUPERVISOR,
        )
        _check("direct verify_audit_completion VERIFIED", direct.get("status") == VERIFIED, direct)

    set_audit_status_provider(None)
    set_audit_pull_provider(None)
    print("=== summary ===")
    if FAILURES:
        print(f"FAILED {len(FAILURES)}")
        for item in FAILURES:
            print(f"  - {item}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
