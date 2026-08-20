#!/usr/bin/env python3
"""Isolated fake-store/provider acceptance for two-phase audit completion (task-05a27e83).

No live Neo4j / Redis / GitHub mutation. Exercises:
  Phase 1 — trusted creation pins class/repo/head/base/context/state (NO status ID)
  Phase 2 — compare-once bind of concrete status ID after exact status+PR provenance
  Completion — verifies immutable bound ID + sealed receipt
  Adversarial — evidence cannot select/overwrite; wrong ID/class/symlink/mode rejected
"""
from __future__ import annotations

import hashlib
import os
import stat
import sys
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fleet_orchestrator.audit_completion import (  # noqa: E402
    AuditContractError,
    assert_no_audit_override_in_evidence,
    compare_once_bind_status,
    is_audit_task,
    normalize_creation_pins,
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
    except Exception as exc:  # noqa: BLE001 — assert exact contract path, not broad silence
        _check(label, False, f"wrong exception type {type(exc).__name__}: {exc}")
        return
    _check(label, False, "expected AuditContractError")


class FakeTaskStore:
    """In-memory OrchTask store mirroring trusted create → bind → complete contract."""

    def __init__(self) -> None:
        self._tasks: Dict[str, Dict[str, Any]] = {}

    def create(
        self,
        task_id: str,
        *,
        completion_class: str = "standard",
        audit_repo: Optional[str] = None,
        audit_head: Optional[str] = None,
        audit_base: Optional[str] = None,
        audit_required_context: Optional[str] = None,
        audit_required_state: Optional[str] = None,
        audit_bound_status_id: Any = None,
    ) -> Dict[str, Any]:
        if audit_bound_status_id not in (None, "", [], {}):
            raise AuditContractError(
                "audit status IDs cannot be set at task creation; use compare-once bind"
            )
        pins = normalize_creation_pins(
            completion_class=completion_class,
            audit_repo=audit_repo,
            audit_head=audit_head,
            audit_base=audit_base,
            audit_required_context=audit_required_context,
            audit_required_state=audit_required_state,
        )
        if task_id in self._tasks:
            # Immutable: refuse pin overwrite on re-create (ON CREATE semantics).
            existing = self._tasks[task_id]
            for key, value in pins.items():
                if existing.get(key) != value and key != "audit_bound_status_id":
                    raise AuditContractError(
                        f"trusted audit pins are immutable after creation; refuse overwrite of {key}"
                    )
            return dict(existing)
        row = {
            "id": task_id,
            "status": "in_progress",
            **pins,
        }
        self._tasks[task_id] = row
        return dict(row)

    def get(self, task_id: str) -> Dict[str, Any]:
        return dict(self._tasks[task_id])

    def bind(
        self,
        task_id: str,
        *,
        status_id: int,
        pr_head_sha: str,
        pr_base_sha: str,
    ) -> Dict[str, Any]:
        task = self._tasks[task_id]
        bind = compare_once_bind_status(
            task,
            status_id=status_id,
            pr_head_sha=pr_head_sha,
            pr_base_sha=pr_base_sha,
        )
        if not bind.get("already_bound"):
            # Compare-once write
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
) -> str:
    root.mkdir(parents=True, exist_ok=True)
    refs = {
        "audit_repo": repo,
        "audit_head": head,
        "audit_base": base,
        "audit_required_context": context,
        "audit_required_state": state,
        "audit_bound_status_id": status_id,
    }
    import json

    (root / "refs.json").write_text(json.dumps(refs, indent=2) + "\n", encoding="utf-8")
    verdict = (
        f"ENDORSE\naudit_repo={repo}\naudit_head={head}\naudit_base={base}\n"
        f"audit_required_context={context}\naudit_required_state={state}\n"
        f"audit_bound_status_id={status_id}\n"
    )
    (root / "verdict-receipt.txt").write_text(verdict, encoding="utf-8")
    lines = []
    for name in ("refs.json", "verdict-receipt.txt"):
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


def _provider_factory(rows: List[Dict[str, Any]]):
    def _provider(repo: str, sha: str) -> List[Dict[str, Any]]:
        if repo != REPO or sha != HEAD:
            return []
        return list(rows)

    return _provider


def main() -> int:
    print("=== audit_completion_two_phase_acceptance (fake store/provider) ===")
    store = FakeTaskStore()
    good_statuses = [
        {"id": STATUS_ID, "context": CONTEXT, "state": STATE},
        {"id": WRONG_ID, "context": "audit/other", "state": "failure"},
    ]
    set_audit_status_provider(_provider_factory(good_statuses))

    # --- Phase 1: trusted creation pins (no status ID) ---
    print("-- phase 1 creation pins --")
    _expect_error(
        "reject status id at creation",
        lambda: store.create(
            "t-bad-id",
            completion_class="audit",
            audit_repo=REPO,
            audit_head=HEAD,
            audit_base=BASE,
            audit_required_context=CONTEXT,
            audit_required_state=STATE,
            audit_bound_status_id=STATUS_ID,
        ),
        substr="cannot be set at task creation",
    )
    _expect_error(
        "reject incomplete audit pins",
        lambda: store.create("t-incomplete", completion_class="audit", audit_repo=REPO),
        substr="audit_head",
    )
    task = store.create(
        "t-audit",
        completion_class="audit",
        audit_repo=REPO,
        audit_head=HEAD,
        audit_base=BASE,
        audit_required_context=CONTEXT,
        audit_required_state=STATE,
    )
    _check("creation pins class=audit", task["completion_class"] == "audit")
    _check("creation leaves bound status id unset", task["audit_bound_status_id"] is None)
    _check("creation pins exact head", task["audit_head"] == HEAD)
    _check("creation pins exact base", task["audit_base"] == BASE)
    _check("is_audit_task true", is_audit_task(task))
    _check("missing class is not audit", not is_audit_task({"audit_head": HEAD}))

    _expect_error(
        "refuse pin overwrite on re-create",
        lambda: store.create(
            "t-audit",
            completion_class="audit",
            audit_repo=REPO,
            audit_head=WRONG_HEAD,
            audit_base=BASE,
            audit_required_context=CONTEXT,
            audit_required_state=STATE,
        ),
        substr="immutable",
    )

    # --- Evidence cannot select/overwrite ---
    print("-- evidence cannot select/overwrite --")
    _expect_error(
        "evidence cannot set completion_class",
        lambda: assert_no_audit_override_in_evidence({"completion_class": "audit", "audit_receipt": "/x"}),
        substr="cannot select or overwrite",
    )
    _expect_error(
        "evidence cannot set audit_bound_status_id",
        lambda: assert_no_audit_override_in_evidence({"audit_bound_status_id": STATUS_ID}),
        substr="cannot select or overwrite",
    )
    _expect_error(
        "evidence cannot set audit_head",
        lambda: assert_no_audit_override_in_evidence({"audit_head": HEAD}),
        substr="cannot select or overwrite",
    )

    # Ordinary verifier path: audit_receipt without trusted audit class rejects
    v = verify_completion_evidence(
        {"audit_receipt": "/home/mira/recovery/r5-audit/example"},
        producer="attacker",
        trusted_task={"completion_class": "standard"},
    )
    _check(
        "standard task cannot self-select audit_receipt",
        isinstance(v, dict)
        and v.get("reject_completion") is True
        and "completion_class=audit" in str(v.get("reason") or ""),
        v,
    )

    # --- Phase 2: compare-once bind ---
    print("-- phase 2 compare-once bind --")
    _expect_error(
        "bind rejects PR head mismatch",
        lambda: store.bind("t-audit", status_id=STATUS_ID, pr_head_sha=WRONG_HEAD, pr_base_sha=BASE),
        substr="PR provenance mismatch",
    )
    _expect_error(
        "bind rejects PR base mismatch",
        lambda: store.bind("t-audit", status_id=STATUS_ID, pr_head_sha=HEAD, pr_base_sha=WRONG_HEAD),
        substr="PR provenance mismatch",
    )
    _expect_error(
        "bind rejects wrong status id",
        lambda: store.bind("t-audit", status_id=WRONG_ID, pr_head_sha=HEAD, pr_base_sha=BASE),
        substr="context mismatch",
    )
    _expect_error(
        "bind rejects missing status id",
        lambda: store.bind("t-audit", status_id=99999999999, pr_head_sha=HEAD, pr_base_sha=BASE),
        substr="not found",
    )
    bind = store.bind("t-audit", status_id=STATUS_ID, pr_head_sha=HEAD, pr_base_sha=BASE)
    _check("bind writes concrete status id", bind["audit_bound_status_id"] == STATUS_ID)
    _check("task now has bound id", store.get("t-audit")["audit_bound_status_id"] == STATUS_ID)
    again = store.bind("t-audit", status_id=STATUS_ID, pr_head_sha=HEAD, pr_base_sha=BASE)
    _check("bind same id is idempotent", again.get("already_bound") is True)
    _expect_error(
        "bind different id refuses overwrite",
        lambda: store.bind("t-audit", status_id=WRONG_ID, pr_head_sha=HEAD, pr_base_sha=BASE),
        substr="refuses overwrite",
    )

    # Complete without bind (fresh task)
    unbound = store.create(
        "t-unbound",
        completion_class="audit",
        audit_repo=REPO,
        audit_head=HEAD,
        audit_base=BASE,
        audit_required_context=CONTEXT,
        audit_required_state=STATE,
    )
    _check("unbound task has no status id", unbound["audit_bound_status_id"] is None)
    _expect_error(
        "complete without prior bind rejected",
        lambda: store.complete("t-unbound", {"audit_receipt": "/home/mira/recovery/r5-audit/x"}),
        substr="compare-once bind",
    )

    # --- Sealed receipt adversarial ---
    print("-- sealed receipt adversarial --")
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
        )
        # Make link tree look sealed by pointing at real; resolve must refuse symlink component.
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
            ),
            substr="symlink",
        )

        # Wrong mode
        bad_mode = Path(td) / "badmode"
        sealed = _seal_receipt(
            bad_mode,
            repo=REPO,
            head=HEAD,
            base=BASE,
            context=CONTEXT,
            state=STATE,
            status_id=STATUS_ID,
        )
        os.chmod(bad_mode / "verdict-receipt.txt", 0o644)
        _expect_error(
            "wrong file mode rejected",
            lambda: verify_sealed_audit_receipt(
                sealed,
                expected_repo=REPO,
                expected_head=HEAD,
                expected_base=BASE,
                expected_context=CONTEXT,
                expected_state=STATE,
                expected_status_id=STATUS_ID,
            ),
            substr="0444",
        )
        os.chmod(bad_mode / "verdict-receipt.txt", 0o444)

        # Path traversal / escape outside recovery
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
            ),
            substr="must resolve under",
        )

        # Good sealed receipt under /home/mira/recovery
        good_root = Path(td) / "good"
        good_path = _seal_receipt(
            good_root,
            repo=REPO,
            head=HEAD,
            base=BASE,
            context=CONTEXT,
            state=STATE,
            status_id=STATUS_ID,
        )
        sealed_ok = verify_sealed_audit_receipt(
            good_path,
            expected_repo=REPO,
            expected_head=HEAD,
            expected_base=BASE,
            expected_context=CONTEXT,
            expected_state=STATE,
            expected_status_id=STATUS_ID,
        )
        _check("good sealed receipt verifies", sealed_ok["receipt_root"] == str(good_root.resolve()) or sealed_ok["receipt_root"] == good_path)

        # Wrong status id in receipt vs bound
        _expect_error(
            "receipt missing wrong status id bind",
            lambda: verify_sealed_audit_receipt(
                good_path,
                expected_repo=REPO,
                expected_head=HEAD,
                expected_base=BASE,
                expected_context=CONTEXT,
                expected_state=STATE,
                expected_status_id=WRONG_ID,
            ),
            substr="audit_bound_status_id",
        )

        # --- Full successful lifecycle ---
        print("-- full successful lifecycle --")
        result = store.complete("t-audit", {"audit_receipt": good_path}, producer="conductor-grok")
        _check("lifecycle VERIFIED", result.get("status") == VERIFIED, result)
        _check("lifecycle applies", result.get("applies") is True)
        _check("lifecycle bound id echoed", result.get("audit_bound_status_id") == STATUS_ID)
        _check("lifecycle source is contract", result.get("source") == "audit-completion-contract")
        _check("task status completed", store.get("t-audit")["status"] == "completed")

        # Direct verify_audit_completion path (update_task_status caller surface)
        direct = verify_audit_completion(
            store.get("t-audit"),
            {"audit_receipt": good_path},
            producer="conductor-grok",
        )
        _check("direct verify_audit_completion VERIFIED", direct.get("status") == VERIFIED, direct)

        # verify_completion_evidence with trusted_task (update_task_status surface)
        via = verify_completion_evidence(
            {"audit_receipt": good_path},
            producer="conductor-grok",
            trusted_task=store.get("t-audit"),
        )
        _check("verify_completion_evidence trusted path VERIFIED", via and via.get("status") == VERIFIED, via)

    set_audit_status_provider(None)
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
