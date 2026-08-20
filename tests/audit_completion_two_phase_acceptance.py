#!/usr/bin/env python3
"""Isolated acceptance: principal-separated audit capability issuer (task-05a27e83).

Same-UID topology adversarial probes:
  - Worker peer session cannot issue supervisor capability (peercred simulation)
  - Worker holding only the public verify key cannot mint a valid token
  - Local env mint helper is not an authority channel
  - Real supervisor peer session issues; API public-key verify accepts; pin/bind succeed

No live Neo4j / Redis / GitHub mutation.
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
    issue_audit_capability,
    issue_for_peer_session,
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
    verify_audit_completion,
    verify_sealed_audit_receipt,
)
from fleet_orchestrator.audit_supervisor_capability import (  # noqa: E402
    generate_keypair,
    mint_audit_capability,
    mint_signed_capability,
    resolve_attested_audit_actor,
    set_audit_capability_keys,
    set_peer_session_override,
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

    def bind(
        self,
        task_id: str,
        *,
        capability_token: str,
        status_id: int,
        body_from: Optional[str] = None,
    ) -> Dict[str, Any]:
        task = self._tasks[task_id]
        actor = resolve_attested_audit_actor(
            capability_token=capability_token,
            task_id=task_id,
            action="bind-audit-status",
            project_supervisor=task.get("project_supervisor"),
            body_from=body_from,
        )
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
        "audit_head": head if not forge_substring_only else WRONG_HEAD,
        "audit_base": base if not forge_substring_only else WRONG_HEAD,
        "audit_required_context": context,
        "audit_required_state": state,
        "audit_bound_status_id": status_id if not forge_substring_only else WRONG_ID,
        "audit_pr_number": pr_number,
    }
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
    print("=== audit_completion_two_phase_acceptance (peercred issuer + verify-only pubkey) ===")
    priv, pub = generate_keypair()
    set_audit_capability_keys(private_key=priv, public_key=pub)
    # API-shaped verify path: only public key available (simulate worker/API without private).
    verify_only_pub = pub

    store = FakeTaskStore(project_supervisor=SUPERVISOR)
    set_issuer_hooks(supervisor_loader=lambda _tid: SUPERVISOR)

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

    def _issue_as(peer_session: str, task_id: str, action: str) -> Dict[str, Any]:
        return issue_audit_capability(
            task_id=task_id,
            action=action,
            inprocess_peer_session=peer_session,
        )

    # --- Principal separation / same-UID worker denial ---
    print("-- same-UID worker mint/issue denial --")
    store.create_ordinary("t-cap")
    _expect_error(
        "local env mint helper is not authority",
        lambda: mint_audit_capability(session_id=SUPERVISOR, task_id="t-cap", action="pin-audit-contract"),
        substr="not an authority channel",
    )
    _expect_error(
        "worker peer session cannot issue supervisor capability",
        lambda: _issue_as(WORKER, "t-cap", "pin-audit-contract"),
        substr="not project supervisor",
    )
    # Worker with only public key material: cannot produce a signature the verifier accepts.
    # Simulate by clearing private key and attempting issuer mint path without private.
    set_audit_capability_keys(private_key=None, public_key=verify_only_pub)
    _expect_error(
        "worker/API without private key cannot mint signed capability",
        lambda: mint_signed_capability(
            session_id=SUPERVISOR,
            task_id="t-cap",
            action="pin-audit-contract",
        ),
        substr="private key missing",
    )
    # Restore issuer private for supervisor path.
    set_audit_capability_keys(private_key=priv, public_key=pub)

    issued = _issue_as(SUPERVISOR, "t-cap", "pin-audit-contract")
    _check("supervisor peer session issues capability", issued.get("ok") is True)
    _check("issued session is supervisor", issued.get("session_id") == SUPERVISOR)

    # Real unix-socket channel: peer session from this process's ORCH_SESSION_ID environ.
    print("-- real unix-socket issuer channel --")
    import socket
    import threading
    import time
    from fleet_orchestrator.audit_capability_issuer import run_socket_server

    with tempfile.TemporaryDirectory(prefix="audit-cap-sock-") as sd:
        sock_path = Path(sd) / "issuer.sock"
        # Persist the in-memory keypair to files the server will load.
        write_keypair_files(Path(sd) / "ed25519.private", Path(sd) / "ed25519.public", private_key=priv)
        env = {
            "ORCH_AUDIT_CAPABILITY_PRIVATE_KEY_PATH": str(Path(sd) / "ed25519.private"),
            "ORCH_AUDIT_CAPABILITY_PUBLIC_KEY_PATH": str(Path(sd) / "ed25519.public"),
            "ORCH_AUDIT_CAPABILITY_SOCKET": str(sock_path),
        }
        # Clear in-memory keys so server uses file mode.
        set_audit_capability_keys(private_key=None, public_key=None)
        set_issuer_hooks(supervisor_loader=lambda _tid: SUPERVISOR)
        t = threading.Thread(
            target=lambda: run_socket_server(socket_path=sock_path, init_keys=False, env=env),
            daemon=True,
        )
        t.start()
        for _ in range(50):
            if sock_path.exists():
                break
            time.sleep(0.02)
        _check("issuer socket created", sock_path.exists())
        old_session = os.environ.get("ORCH_SESSION_ID")
        try:
            os.environ["ORCH_SESSION_ID"] = WORKER
            _expect_error(
                "socket channel: worker environ denied",
                lambda: issue_audit_capability(
                    task_id="t-cap",
                    action="pin-audit-contract",
                    socket_path=sock_path,
                    env=env,
                ),
                substr="not project supervisor",
            )
            os.environ["ORCH_SESSION_ID"] = SUPERVISOR
            sock_issued = issue_audit_capability(
                task_id="t-cap",
                action="pin-audit-contract",
                socket_path=sock_path,
                env=env,
            )
            _check("socket channel: supervisor environ issues", sock_issued.get("ok") is True)
            # Verify with public file only
            set_audit_capability_keys(private_key=None, public_key=None)
            os.environ["ORCH_AUDIT_CAPABILITY_PUBLIC_KEY_PATH"] = str(Path(sd) / "ed25519.public")
            att = verify_audit_capability(
                sock_issued["capability"],
                task_id="t-cap",
                action="pin-audit-contract",
                expected_supervisor=SUPERVISOR,
                env=env,
            )
            _check("socket-issued token verifies with public key", att == SUPERVISOR)
        finally:
            if old_session is None:
                os.environ.pop("ORCH_SESSION_ID", None)
            else:
                os.environ["ORCH_SESSION_ID"] = old_session
            os.environ.pop("ORCH_AUDIT_CAPABILITY_PUBLIC_KEY_PATH", None)
            set_audit_capability_keys(private_key=priv, public_key=pub)
            set_issuer_hooks(supervisor_loader=lambda _tid: SUPERVISOR)
    attested = resolve_attested_audit_actor(
        capability_token=issued["capability"],
        task_id="t-cap",
        action="pin-audit-contract",
        project_supervisor=SUPERVISOR,
        body_from=None,
        public_key=verify_only_pub,  # API verify-only key
    )
    _check("API public-key verify accepts issuer token", attested == SUPERVISOR)
    _expect_error(
        "forged body.from still not authority when capability missing",
        lambda: resolve_attested_audit_actor(
            capability_token="",
            task_id="t-cap",
            action="pin-audit-contract",
            project_supervisor=SUPERVISOR,
            body_from=SUPERVISOR,
            public_key=verify_only_pub,
        ),
        substr="missing or malformed",
    )

    # File mode keys: private 0600 / public readable; worker "secret-read" of public cannot mint.
    print("-- file-mode key separation --")
    with tempfile.TemporaryDirectory(prefix="audit-cap-keys-") as td:
        priv_path = Path(td) / "ed25519.private"
        pub_path = Path(td) / "ed25519.public"
        file_priv, file_pub = write_keypair_files(priv_path, pub_path)
        _check("private key mode 0600", oct(priv_path.stat().st_mode & 0o777) == "0o600")
        # Issuer mints with file private; API verifies with file public only.
        set_audit_capability_keys(private_key=file_priv, public_key=None)
        os.environ["ORCH_AUDIT_CAPABILITY_PUBLIC_KEY_PATH"] = str(pub_path)
        os.environ["ORCH_AUDIT_CAPABILITY_PRIVATE_KEY_PATH"] = str(Path(td) / "absent.private")
        tok = mint_signed_capability(
            session_id=SUPERVISOR,
            task_id="t-cap",
            action="bind-audit-status",
            private_key=file_priv,
        )
        session = verify_audit_capability(
            tok,
            task_id="t-cap",
            action="bind-audit-status",
            expected_supervisor=SUPERVISOR,
            public_key=file_pub,
        )
        _check("file-mode public verify works", session == SUPERVISOR)
        os.environ.pop("ORCH_AUDIT_CAPABILITY_PUBLIC_KEY_PATH", None)
        os.environ.pop("ORCH_AUDIT_CAPABILITY_PRIVATE_KEY_PATH", None)
        set_audit_capability_keys(private_key=priv, public_key=pub)

    # --- Ordinary create ---
    print("-- ordinary create rejects audit fields --")
    _expect_error(
        "ordinary create rejects completion_class=audit",
        lambda: reject_ordinary_create_audit_fields({"completion_class": "audit", "audit_repo": REPO}),
        substr="cannot select audit contract",
    )
    ordinary = store.create_ordinary("t-audit", {"description": "x", "from": WORKER})
    _check("ordinary create is standard class", ordinary["completion_class"] == "standard")

    # --- Pin / bind via issuer channel ---
    print("-- supervisor pin/bind via issuer channel --")
    pin_tok = _issue_as(SUPERVISOR, "t-audit", "pin-audit-contract")["capability"]
    _expect_error(
        "worker forged-from pin without capability denied",
        lambda: store.pin(
            "t-audit",
            capability_token="",
            body_from=SUPERVISOR,
            audit_repo=REPO,
            audit_head=HEAD,
            audit_base=BASE,
            audit_required_context=CONTEXT,
            audit_required_state=STATE,
            audit_pr_number=PR_NUMBER,
        ),
        substr="missing or malformed",
    )
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
    _check("supervisor pin sets class=audit", pin["completion_class"] == "audit")
    _check("pin attested actor is supervisor", pin.get("attested_actor") == SUPERVISOR)
    _check("is_audit_task true after pin", is_audit_task(store.get("t-audit")))

    _expect_error(
        "worker peer cannot issue bind capability",
        lambda: _issue_as(WORKER, "t-audit", "bind-audit-status"),
        substr="not project supervisor",
    )
    bind_tok = _issue_as(SUPERVISOR, "t-audit", "bind-audit-status")["capability"]
    bind = store.bind("t-audit", capability_token=bind_tok, status_id=STATUS_ID)
    _check("bind writes concrete status id", bind["audit_bound_status_id"] == STATUS_ID)
    _check("bind attested actor is supervisor", bind.get("attested_actor") == SUPERVISOR)

    # Decoy PR mismatch
    store.create_ordinary("t-bad-pr")
    bad_pin = _issue_as(SUPERVISOR, "t-bad-pr", "pin-audit-contract")["capability"]
    store.pin(
        "t-bad-pr",
        capability_token=bad_pin,
        audit_repo=REPO,
        audit_head=HEAD,
        audit_base=BASE,
        audit_required_context=CONTEXT,
        audit_required_state=STATE,
        audit_pr_number=999,
    )
    bad_bind = _issue_as(SUPERVISOR, "t-bad-pr", "bind-audit-status")["capability"]
    _expect_error(
        "bind rejects server-side PR mismatch",
        lambda: store.bind("t-bad-pr", capability_token=bad_bind, status_id=STATUS_ID),
        substr="PR provenance mismatch",
    )

    unbound = store.create_ordinary("t-unbound")
    store.pin(
        "t-unbound",
        capability_token=_issue_as(SUPERVISOR, "t-unbound", "pin-audit-contract")["capability"],
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

    # --- Receipt structured provenance ---
    print("-- sealed receipt structured provenance --")
    with tempfile.TemporaryDirectory(prefix="audit-receipt-", dir="/home/mira/recovery") as td:
        unlisted = Path(td) / "unlisted"
        unlisted_path = _seal_receipt(
            unlisted, repo=REPO, head=HEAD, base=BASE, context=CONTEXT, state=STATE,
            status_id=STATUS_ID, pr_number=PR_NUMBER, include_refs_in_sums=False,
        )
        _expect_error(
            "unlisted refs.json rejected",
            lambda: verify_sealed_audit_receipt(
                unlisted_path, expected_repo=REPO, expected_head=HEAD, expected_base=BASE,
                expected_context=CONTEXT, expected_state=STATE, expected_status_id=STATUS_ID,
                expected_pr_number=PR_NUMBER,
            ),
            substr="refs.json",
        )
        forged = Path(td) / "forged"
        forged_path = _seal_receipt(
            forged, repo=REPO, head=HEAD, base=BASE, context=CONTEXT, state=STATE,
            status_id=STATUS_ID, pr_number=PR_NUMBER, forge_substring_only=True,
        )
        _expect_error(
            "forged substring receipt rejected",
            lambda: verify_sealed_audit_receipt(
                forged_path, expected_repo=REPO, expected_head=HEAD, expected_base=BASE,
                expected_context=CONTEXT, expected_state=STATE, expected_status_id=STATUS_ID,
                expected_pr_number=PR_NUMBER,
            ),
            substr="mismatch",
        )
        good = Path(td) / "good"
        good_path = _seal_receipt(
            good, repo=REPO, head=HEAD, base=BASE, context=CONTEXT, state=STATE,
            status_id=STATUS_ID, pr_number=PR_NUMBER,
        )
        print("-- full successful lifecycle --")
        result = store.complete("t-audit", {"audit_receipt": good_path}, producer=SUPERVISOR)
        _check("lifecycle VERIFIED", result.get("status") == VERIFIED, result)
        _check("lifecycle bound id echoed", result.get("audit_bound_status_id") == STATUS_ID)

    set_audit_status_provider(None)
    set_audit_pull_provider(None)
    set_audit_capability_keys(private_key=None, public_key=None)
    set_issuer_hooks(peer_resolver=None, supervisor_loader=None)
    set_peer_session_override(None)
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
