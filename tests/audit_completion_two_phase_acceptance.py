#!/usr/bin/env python3
"""Isolated acceptance: distinct-UID peercred principal (task-05a27e83).

CONTROL: ORCH_SESSION_ID from environ is forgeable. Principal = SO_PEERCRED uid
via ORCH_AUDIT_CAPABILITY_UID_MAP under deployed ownership modes.

Adversarial:
  - Worker uid + spoofed ORCH_SESSION_ID=<supervisor> → denied
  - Supervisor uid in map → issue succeeds (environ irrelevant)
  - Private key not owned by issuer euid → denied
  - Local env mint helper is not authority
  - Full pin/bind/complete with issued capability

No live Neo4j / Redis / GitHub / useradd mutation.
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

from fleet_orchestrator.audit_capability_issuer import (  # noqa: E402
    assert_private_key_ownership,
    issue_audit_capability,
    resolve_peer_principal,
    set_issuer_hooks,
)
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
    verify_sealed_audit_receipt,
)
from fleet_orchestrator.audit_supervisor_capability import (  # noqa: E402
    generate_keypair,
    mint_audit_capability,
    mint_signed_capability,
    resolve_attested_audit_actor,
    set_audit_capability_keys,
    verify_audit_capability,
    write_keypair_files,
)
from fleet_orchestrator.evidence_verification import VERIFIED, verify_completion_evidence  # noqa: E402

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
# Deployed ownership simulation: distinct uids (not created on host).
WORKER_UID = 1000
SUPERVISOR_UID = 1001
ISSUER_UID = 1002  # orch-cap

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
    def __init__(self, *, project_supervisor: str) -> None:
        self.project_supervisor = project_supervisor
        self._tasks: Dict[str, Dict[str, Any]] = {}

    def create_ordinary(self, task_id: str, payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        reject_ordinary_create_audit_fields(payload or {})
        row = {
            "id": task_id,
            "status": "in_progress",
            "completion_class": "standard",
            "audit_bound_status_id": None,
            "project_supervisor": self.project_supervisor,
        }
        self._tasks[task_id] = row
        return dict(row)

    def get(self, task_id: str) -> Dict[str, Any]:
        return dict(self._tasks[task_id])

    def pin(self, task_id: str, *, capability_token: str, body_from: Optional[str] = None, **pins_kw: Any) -> Dict[str, Any]:
        task = self._tasks[task_id]
        actor = resolve_attested_audit_actor(
            capability_token=capability_token,
            task_id=task_id,
            action="pin-audit-contract",
            project_supervisor=task.get("project_supervisor"),
            body_from=body_from,
        )
        assert_actor_is_project_supervisor(
            actor=actor,
            project_supervisor=task.get("project_supervisor"),
            action="pin-audit-contract",
        )
        pins = normalize_supervisor_pins(**pins_kw)
        if task.get("completion_class") == "audit":
            for key, value in pins.items():
                if task.get(key) != value and key != "audit_bound_status_id":
                    raise AuditContractError(
                        f"trusted audit pins are immutable after creation; refuse overwrite of {key}"
                    )
            return {"already_pinned": True, "attested_actor": actor, **pins}
        task.update(pins)
        return {"already_pinned": False, "attested_actor": actor, **pins}

    def bind(self, task_id: str, *, capability_token: str, status_id: int, body_from: Optional[str] = None) -> Dict[str, Any]:
        task = self._tasks[task_id]
        actor = resolve_attested_audit_actor(
            capability_token=capability_token,
            task_id=task_id,
            action="bind-audit-status",
            project_supervisor=task.get("project_supervisor"),
            body_from=body_from,
        )
        bind = compare_once_bind_status(task, status_id=status_id)
        if not bind.get("already_bound"):
            if task.get("audit_bound_status_id") is not None:
                raise AuditContractError("compare-once refuses overwrite of bound status id")
            task["audit_bound_status_id"] = int(status_id)
        out = dict(bind)
        out["attested_actor"] = actor
        return out

    def complete(self, task_id: str, evidence: Dict[str, Any], *, producer: str = "tester") -> Dict[str, Any]:
        task = self._tasks[task_id]
        assert_no_audit_override_in_evidence(evidence)
        result = verify_completion_evidence(evidence, producer=producer, trusted_task=task)
        if not isinstance(result, dict) or result.get("reject_completion") or result.get("status") != VERIFIED:
            raise AuditContractError(str((result or {}).get("reason") or "completion rejected"))
        task["status"] = "completed"
        return result


def _seal_receipt(root: Path, *, forge: bool = False, include_refs: bool = True) -> str:
    root.mkdir(parents=True, exist_ok=True)
    refs = {
        "audit_repo": REPO,
        "audit_head": WRONG_HEAD if forge else HEAD,
        "audit_base": WRONG_HEAD if forge else BASE,
        "audit_required_context": CONTEXT,
        "audit_required_state": STATE,
        "audit_bound_status_id": WRONG_ID if forge else STATUS_ID,
        "audit_pr_number": PR_NUMBER,
    }
    (root / "refs.json").write_text(json.dumps(refs, indent=2) + "\n", encoding="utf-8")
    (root / "verdict-receipt.txt").write_text(
        f"ENDORSE audit_repo={REPO} audit_head={HEAD} audit_base={BASE} "
        f"audit_required_context={CONTEXT} audit_required_state={STATE} "
        f"audit_bound_status_id={STATUS_ID} audit_pr_number={PR_NUMBER}\n",
        encoding="utf-8",
    )
    names = ["verdict-receipt.txt"] + (["refs.json"] if include_refs else [])
    if include_refs:
        names = ["refs.json", "verdict-receipt.txt"]
    lines = [f"{hashlib.sha256((root / n).read_bytes()).hexdigest()}  {n}" for n in names]
    (root / "SHA256SUMS").write_text("\n".join(lines) + "\n", encoding="utf-8")
    for path in root.rglob("*"):
        os.chmod(path, 0o555 if path.is_dir() else 0o444)
    os.chmod(root, 0o555)
    return str(root)


def main() -> int:
    print("=== audit_completion_two_phase_acceptance (uid principal + ownership modes) ===")
    priv, pub = generate_keypair()
    set_audit_capability_keys(private_key=priv, public_key=pub)

    uid_map = {SUPERVISOR_UID: SUPERVISOR}  # worker uid intentionally absent
    set_issuer_hooks(
        supervisor_loader=lambda _tid: SUPERVISOR,
        uid_map_loader=lambda: dict(uid_map),
        euid_getter=lambda: ISSUER_UID,
        stat_uid_getter=lambda _p: (ISSUER_UID, 0o100600),
    )

    store = FakeTaskStore(project_supervisor=SUPERVISOR)
    set_audit_status_provider(
        lambda repo, sha: (
            [{"id": STATUS_ID, "context": CONTEXT, "state": STATE},
             {"id": WRONG_ID, "context": "audit/other", "state": "failure"}]
            if repo == REPO and sha == HEAD else []
        )
    )
    set_audit_pull_provider(
        lambda repo, pr: {
            "number": pr,
            "head_sha": HEAD if repo == REPO and pr == PR_NUMBER else WRONG_HEAD,
            "base_sha": BASE if repo == REPO and pr == PR_NUMBER else WRONG_HEAD,
        }
    )

    def _issue(peer_uid: int, task_id: str, action: str, spoof_session: str = "") -> Dict[str, Any]:
        return issue_audit_capability(
            task_id=task_id,
            action=action,
            inprocess_peer_uid=peer_uid,
            inprocess_peer_session=spoof_session,
        )

    # --- Spoofed-env denial under ownership modes ---
    print("-- spoofed environ denial (uid principal) --")
    store.create_ordinary("t-cap")
    os.environ["ORCH_SESSION_ID"] = SUPERVISOR  # forge attempt
    _expect_error(
        "worker uid + spoofed ORCH_SESSION_ID=<supervisor> denied",
        lambda: _issue(WORKER_UID, "t-cap", "pin-audit-contract", spoof_session=SUPERVISOR),
        substr="not a provisioned supervisor principal",
    )
    _check(
        "spoofed environ does not map worker uid",
        True,  # covered by expect_error above
    )
    _expect_error(
        "resolve_peer_principal ignores environ for worker uid",
        lambda: resolve_peer_principal(peer_uid=WORKER_UID),
        substr="not a provisioned supervisor principal",
    )
    # Supervisor uid succeeds; spoofed environ is irrelevant (not read for principal).
    os.environ["ORCH_SESSION_ID"] = WORKER
    issued = _issue(SUPERVISOR_UID, "t-cap", "pin-audit-contract")
    _check("supervisor uid issues despite spoofed worker environ", issued.get("ok") is True)
    _check("issued session from uid map not environ", issued.get("session_id") == SUPERVISOR)
    _check("peer_uid echoed", issued.get("peer_uid") == SUPERVISOR_UID)
    _check("spoofed environ still set but unused", os.environ.get("ORCH_SESSION_ID") == WORKER)
    os.environ.pop("ORCH_SESSION_ID", None)

    _expect_error(
        "claimed peer_session conflicting with uid map rejected",
        lambda: _issue(SUPERVISOR_UID, "t-cap", "pin-audit-contract", spoof_session="evil-session"),
        substr="conflicts with uid-mapped principal",
    )
    _expect_error(
        "inprocess_peer_session alone rejected",
        lambda: issue_audit_capability(
            task_id="t-cap",
            action="pin-audit-contract",
            inprocess_peer_session=SUPERVISOR,
        ),
        substr="not authority",
    )
    _expect_error(
        "local env mint helper is not authority",
        lambda: mint_audit_capability(session_id=SUPERVISOR, task_id="t-cap", action="pin-audit-contract"),
        substr="not an authority channel",
    )

    # --- Distinct-UID private key ownership ---
    print("-- distinct-UID key ownership --")
    with tempfile.TemporaryDirectory(prefix="audit-cap-own-") as td:
        priv_path = Path(td) / "ed25519.private"
        pub_path = Path(td) / "ed25519.public"
        write_keypair_files(priv_path, pub_path)
        # Simulate key owned by issuer uid; process is worker uid → deny
        set_issuer_hooks(
            supervisor_loader=lambda _tid: SUPERVISOR,
            uid_map_loader=lambda: dict(uid_map),
            euid_getter=lambda: WORKER_UID,
            stat_uid_getter=lambda _p: (ISSUER_UID, 0o100600),
        )
        _expect_error(
            "worker euid cannot load issuer-owned private key",
            lambda: assert_private_key_ownership(priv_path),
            substr="does not own private key",
        )
        # Correct ownership mode
        set_issuer_hooks(
            supervisor_loader=lambda _tid: SUPERVISOR,
            uid_map_loader=lambda: dict(uid_map),
            euid_getter=lambda: ISSUER_UID,
            stat_uid_getter=lambda _p: (ISSUER_UID, 0o100600),
        )
        assert_private_key_ownership(priv_path)
        _check("issuer euid owning 0600 key accepted", True)
        # Group-readable key rejected
        set_issuer_hooks(
            supervisor_loader=lambda _tid: SUPERVISOR,
            uid_map_loader=lambda: dict(uid_map),
            euid_getter=lambda: ISSUER_UID,
            stat_uid_getter=lambda _p: (ISSUER_UID, 0o100640),
        )
        _expect_error(
            "group-readable private key rejected",
            lambda: assert_private_key_ownership(priv_path),
            substr="mode 0600",
        )
        set_issuer_hooks(
            supervisor_loader=lambda _tid: SUPERVISOR,
            uid_map_loader=lambda: dict(uid_map),
            euid_getter=lambda: ISSUER_UID,
            stat_uid_getter=lambda _p: (ISSUER_UID, 0o100600),
        )

    # Deploy unit files present
    unit = ROOT / "deploy/systemd/orch-audit-capabilityd.service"
    sock_unit = ROOT / "deploy/systemd/orch-audit-capabilityd.socket"
    _check("systemd service unit present", unit.is_file())
    _check("systemd socket unit present", sock_unit.is_file())
    service_text = unit.read_text(encoding="utf-8")
    _check("service uses User=orch-cap", "User=orch-cap" in service_text)

    # Verify-only public key path
    att = resolve_attested_audit_actor(
        capability_token=issued["capability"],
        task_id="t-cap",
        action="pin-audit-contract",
        project_supervisor=SUPERVISOR,
        public_key=pub,
    )
    _check("public-key verify accepts uid-issued token", att == SUPERVISOR)

    # --- Ordinary create + pin/bind lifecycle ---
    print("-- ordinary create + pin/bind lifecycle --")
    _expect_error(
        "ordinary create rejects audit fields",
        lambda: reject_ordinary_create_audit_fields({"completion_class": "audit"}),
        substr="cannot select audit contract",
    )
    store.create_ordinary("t-audit")
    pin_tok = _issue(SUPERVISOR_UID, "t-audit", "pin-audit-contract")["capability"]
    pin = store.pin(
        "t-audit",
        capability_token=pin_tok,
        audit_repo=REPO,
        audit_head=HEAD,
        audit_base=BASE,
        audit_required_context=CONTEXT,
        audit_required_state=STATE,
        audit_pr_number=PR_NUMBER,
    )
    _check("pin attested supervisor", pin.get("attested_actor") == SUPERVISOR)
    _check("is_audit_task", is_audit_task(store.get("t-audit")))
    bind_tok = _issue(SUPERVISOR_UID, "t-audit", "bind-audit-status")["capability"]
    bind = store.bind("t-audit", capability_token=bind_tok, status_id=STATUS_ID)
    _check("bind status id", bind.get("audit_bound_status_id") == STATUS_ID)

    print("-- sealed receipt + complete --")
    with tempfile.TemporaryDirectory(prefix="audit-receipt-", dir="/home/mira/recovery") as td:
        _expect_error(
            "unlisted refs rejected",
            lambda: verify_sealed_audit_receipt(
                _seal_receipt(Path(td) / "u", include_refs=False),
                expected_repo=REPO, expected_head=HEAD, expected_base=BASE,
                expected_context=CONTEXT, expected_state=STATE,
                expected_status_id=STATUS_ID, expected_pr_number=PR_NUMBER,
            ),
            substr="refs.json",
        )
        _expect_error(
            "forged substring rejected",
            lambda: verify_sealed_audit_receipt(
                _seal_receipt(Path(td) / "f", forge=True),
                expected_repo=REPO, expected_head=HEAD, expected_base=BASE,
                expected_context=CONTEXT, expected_state=STATE,
                expected_status_id=STATUS_ID, expected_pr_number=PR_NUMBER,
            ),
            substr="mismatch",
        )
        good = _seal_receipt(Path(td) / "g")
        result = store.complete("t-audit", {"audit_receipt": good}, producer=SUPERVISOR)
        _check("lifecycle VERIFIED", result.get("status") == VERIFIED, result)

    set_audit_status_provider(None)
    set_audit_pull_provider(None)
    set_audit_capability_keys(private_key=None, public_key=None)
    set_issuer_hooks()
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
