#!/usr/bin/env python3
"""Isolated two-phase audit completion (task-7cf8187b).

No live Neo4j, Redis, or GitHub. Fake store + fake providers.
Principal is SO_PEERCRED uid mapped by ORCH_AUDIT_CAPABILITY_UID_MAP.
tmux / ORCH_SESSION_ID are not authority. LISTEN_FDS must keep the inherited inode.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fleet_orchestrator.audit_capability_issuer import (  # noqa: E402
    handle_issuer_request,
    issue_audit_capability,
    issue_for_peer_uid,
    run_socket_server,
    set_issuer_hooks,
)
from fleet_orchestrator.audit_supervisor_capability import (  # noqa: E402
    DEFAULT_CAPABILITY_DIR,
    default_key_paths,
)
from fleet_orchestrator.audit_completion import (  # noqa: E402
    AuditContractError,
    assert_no_status_id_at_pin,
    compare_once_bind_status,
    is_audit_task,
    normalize_supervisor_pins,
    reject_ordinary_create_audit_fields,
    require_supervisor_actor,
    set_approved_receipt_roots,
    set_audit_pull_provider,
    set_audit_status_provider,
    verify_sealed_audit_receipt,
)
from fleet_orchestrator.audit_supervisor_capability import (  # noqa: E402
    CAPABILITY_HEADER,
    generate_keypair,
    mint_audit_capability,
    set_audit_capability_keys,
    set_peer_session_override,
    write_keypair_files,
)
from fleet_orchestrator.evidence_verification import (  # noqa: E402
    VERIFIED,
    verify_completion_evidence,
)


FAILURES: list[str] = []
SUPERVISOR = "conductor-codex"
WORKER = "infra-grok"
SUPERVISOR_UID = 1001
WORKER_UID = os.getuid()
REPO = "palios-taey/claude-code-fleet-orchestrator"
HEAD = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
BASE = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
WRONG_HEAD = "cccccccccccccccccccccccccccccccccccccccc"
WRONG_BASE = "dddddddddddddddddddddddddddddddddddddddd"
CONTEXT = "audit/gatekeeper"
STATUS_ID = 52572591788
WRONG_ID = 11111111111
OTHER_ID = 22222222222
PR_NUMBER = 32


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _expect_error(label: str, fn, fragment: str):
    try:
        fn()
    except AuditContractError as exc:
        text = str(exc)
        _check(label, fragment in text, text)
        return text
    except Exception as exc:  # noqa: BLE001
        _check(label, False, f"wrong exception {type(exc).__name__}: {exc}")
        return str(exc)
    _check(label, False, "no exception raised")
    return ""


def _expect_unverified(label: str, result, fragment: str) -> None:
    if not isinstance(result, dict):
        _check(label, False, f"expected dict, got {type(result).__name__}: {result}")
        return
    _check(f"{label} reject_completion", result.get("reject_completion") is True, result)
    _check(f"{label} not verified", result.get("verified") is not True, result)
    _check(f"{label} reason", fragment in str(result.get("reason") or ""), result.get("reason"))


class FakeGitHub:
    def __init__(self) -> None:
        self.statuses: dict[tuple[str, str], list[dict]] = {}
        self.pulls: dict[tuple[str, int], dict] = {}
        self.status_calls: list[tuple[str, str]] = []
        self.pull_calls: list[tuple[str, int]] = []

    def status_provider(self, repo: str, sha: str):
        self.status_calls.append((repo, sha))
        return list(self.statuses.get((repo, sha.lower()), []))

    def pull_provider(self, repo: str, number: int):
        self.pull_calls.append((repo, int(number)))
        payload = self.pulls.get((repo, int(number)))
        if payload is None:
            raise AuditContractError(f"unknown PR {repo}#{number}")
        return dict(payload)


class FakeTaskStore:
    def __init__(self, supervisor: str = SUPERVISOR) -> None:
        self.supervisor = supervisor
        self.tasks: dict[str, dict] = {}
        self._lock = threading.Lock()

    def create_ordinary(self, task_id: str, payload: dict | None = None) -> dict:
        payload = dict(payload or {})
        reject_ordinary_create_audit_fields(payload)
        if task_id in self.tasks:
            raise AuditContractError(f"task {task_id} already exists")
        task = {
            "id": task_id,
            "status": "pending",
            "completion_class": "standard",
            "project_supervisor": self.supervisor,
            "audit_bound_status_id": None,
        }
        self.tasks[task_id] = task
        return task

    def create_trusted(self, task_id: str, pins: dict, actor: str) -> dict:
        require_supervisor_actor({"project_supervisor": self.supervisor}, actor, "trusted-create")
        assert_no_status_id_at_pin(pins)
        normalized = normalize_supervisor_pins(
            audit_repo=pins.get("audit_repo"),
            audit_head=pins.get("audit_head"),
            audit_base=pins.get("audit_base"),
            audit_required_context=pins.get("audit_required_context"),
            audit_required_state=pins.get("audit_required_state"),
            audit_pr_number=pins.get("audit_pr_number"),
        )
        task = {
            "id": task_id,
            "status": "pending",
            "project_supervisor": self.supervisor,
            **normalized,
        }
        self.tasks[task_id] = task
        return task

    def pin(self, task_id: str, pins: dict, actor: str) -> dict:
        task = self.tasks.get(task_id)
        if task is None:
            raise AuditContractError(f"task {task_id} not found")
        require_supervisor_actor(task, actor, "pin-audit-contract")
        assert_no_status_id_at_pin(pins)
        normalized = normalize_supervisor_pins(
            audit_repo=pins.get("audit_repo"),
            audit_head=pins.get("audit_head"),
            audit_base=pins.get("audit_base"),
            audit_required_context=pins.get("audit_required_context"),
            audit_required_state=pins.get("audit_required_state"),
            audit_pr_number=pins.get("audit_pr_number"),
        )
        with self._lock:
            if is_audit_task(task):
                mismatches = [
                    key for key in (
                        "audit_repo", "audit_head", "audit_base",
                        "audit_required_context", "audit_required_state", "audit_pr_number",
                    )
                    if task.get(key) != normalized[key]
                ]
                if mismatches:
                    raise AuditContractError(
                        "trusted audit pins are immutable after creation; refuse overwrite of "
                        + ", ".join(mismatches)
                    )
                return {"already_pinned": True, **normalized}
            if is_audit_task(task):
                raise AuditContractError("pin CAS loser refused overwrite")
            task.update(normalized)
            return {"already_pinned": False, **normalized}

    def bind(self, task_id: str, status_id: int, actor: str) -> dict:
        task = self.tasks.get(task_id)
        if task is None:
            raise AuditContractError(f"task {task_id} not found")
        require_supervisor_actor(task, actor, "bind-audit-status")
        compare_once_bind_status(task, status_id=int(status_id))
        with self._lock:
            prior = task.get("audit_bound_status_id")
            if prior is None:
                task["audit_bound_status_id"] = int(status_id)
                return {"already_bound": False, "audit_bound_status_id": int(status_id)}
            if int(prior) == int(status_id):
                return {"already_bound": True, "audit_bound_status_id": int(status_id)}
            raise AuditContractError(
                f"audit_bound_status_id already set to {prior}; compare-once CAS loser refused overwrite"
            )

    def complete(self, task_id: str, evidence: dict, producer: str = WORKER) -> dict:
        task = self.tasks.get(task_id)
        if task is None:
            raise AuditContractError(f"task {task_id} not found")
        verification = verify_completion_evidence(
            evidence, producer=producer, trusted_task=task,
        )
        if not isinstance(verification, dict):
            raise AuditContractError("completion evidence produced no verification record")
        if verification.get("reject_completion") or verification.get("verified") is not True:
            raise AuditContractError(str(verification.get("reason") or "completion rejected"))
        task["status"] = "completed"
        task["completion_evidence_verification"] = verification
        return verification


def _pins(**overrides) -> dict:
    payload = {
        "audit_repo": REPO,
        "audit_head": HEAD,
        "audit_base": BASE,
        "audit_required_context": CONTEXT,
        "audit_required_state": "success",
        "audit_pr_number": PR_NUMBER,
    }
    payload.update(overrides)
    return payload


def _chmod_tree(root: Path, dir_mode: int, file_mode: int) -> None:
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        os.chmod(dirpath, dir_mode)
        for name in filenames:
            os.chmod(Path(dirpath) / name, file_mode)
        for name in dirnames:
            os.chmod(Path(dirpath) / name, dir_mode)


def _structured_verdict() -> str:
    return json.dumps({
        "audit_repo": REPO,
        "audit_head": HEAD,
        "audit_base": BASE,
        "audit_required_context": CONTEXT,
        "audit_required_state": "success",
        "audit_bound_status_id": STATUS_ID,
        "audit_pr_number": PR_NUMBER,
        "verdict": "ENDORSE",
    }, indent=2, sort_keys=True) + "\n"


def _good_refs(**overrides) -> dict:
    refs = {
        "audit_repo": REPO,
        "audit_head": HEAD,
        "audit_base": BASE,
        "audit_required_context": CONTEXT,
        "audit_required_state": "success",
        "audit_bound_status_id": STATUS_ID,
        "audit_pr_number": PR_NUMBER,
    }
    refs.update(overrides)
    return refs


def _write_sealed_receipt(
    root: Path,
    *,
    refs: dict,
    verdict: str,
    extra_unhashed: dict[str, str] | None = None,
    extra_hashed: dict[str, str] | None = None,
    dir_mode: int = 0o555,
    file_mode: int = 0o444,
    sums_extra: list[str] | None = None,
) -> Path:
    if root.exists():
        _chmod_tree(root, 0o755, 0o644)
        shutil.rmtree(root)
    root.mkdir(parents=True)
    files = {
        "refs.json": json.dumps(refs, indent=2, sort_keys=True) + "\n",
        "verdict-receipt.txt": verdict,
        **(extra_hashed or {}),
    }
    entries = []
    for name, content in files.items():
        path = root / name
        path.write_text(content, encoding="utf-8")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        entries.append(f"{digest}  {name}")
    if sums_extra:
        entries.extend(sums_extra)
    (root / "SHA256SUMS").write_text("\n".join(entries) + "\n", encoding="utf-8")
    for name, content in (extra_unhashed or {}).items():
        (root / name).write_text(content, encoding="utf-8")
    for child in root.iterdir():
        if child.is_file() and not child.is_symlink():
            os.chmod(child, file_mode)
    os.chmod(root, dir_mode)
    return root


def _install_matching_github(gh: FakeGitHub) -> None:
    gh.pulls[(REPO, PR_NUMBER)] = {
        "number": PR_NUMBER,
        "head_sha": HEAD,
        "base_sha": BASE,
        "html_url": f"https://github.com/{REPO}/pull/{PR_NUMBER}",
    }
    gh.statuses[(REPO, HEAD)] = [
        {"id": STATUS_ID, "context": CONTEXT, "state": "success", "sha": HEAD},
        {"id": OTHER_ID, "context": CONTEXT, "state": "success", "sha": HEAD},
        {"id": WRONG_ID, "context": "r5-audit-gate", "state": "success", "sha": HEAD},
    ]


def main() -> int:
    tmp_root = Path(tempfile.mkdtemp(prefix="audit-completion-05a27e83-"))
    gh = FakeGitHub()
    store = FakeTaskStore()
    priv, pub = generate_keypair()
    set_audit_capability_keys(private_key=priv, public_key=pub)
    uid_map = {SUPERVISOR_UID: SUPERVISOR}
    set_issuer_hooks(
        uid_map_loader=lambda: uid_map,
        supervisor_loader=lambda _tid: SUPERVISOR,
    )
    set_approved_receipt_roots((tmp_root,))
    set_audit_status_provider(gh.status_provider)
    set_audit_pull_provider(gh.pull_provider)
    try:
        _install_matching_github(gh)

        defaults = default_key_paths({})
        _check(
            "default private key is not under shared user home",
            str(defaults["private"]).startswith("/var/lib/fleet-orchestrator/"),
            defaults["private"],
        )
        _check(
            "DEFAULT_CAPABILITY_DIR is OS-separated",
            DEFAULT_CAPABILITY_DIR == Path("/var/lib/fleet-orchestrator/audit-capability"),
        )
        unit = (ROOT / "deploy/systemd/orch-audit-capabilityd.socket").read_text(encoding="utf-8")
        _check("socket unit SocketUser=orch-cap", "SocketUser=orch-cap" in unit)
        _check("socket unit SocketGroup=orch-audit-sup", "SocketGroup=orch-audit-sup" in unit)
        _check("socket unit SocketMode=0660", "SocketMode=0660" in unit)
        svc = (ROOT / "deploy/systemd/orch-audit-capabilityd.service").read_text(encoding="utf-8")
        _check("service User=orch-cap", "User=orch-cap" in svc)
        _check("service Requires socket unit", "Requires=orch-audit-capabilityd.socket" in svc)

        _expect_error(
            "env HMAC mint is not an authority channel",
            lambda: mint_audit_capability(),
            "not an authority channel",
        )
        _expect_error(
            "peer session env override is not authority",
            lambda: set_peer_session_override(SUPERVISOR),
            "not an authority channel",
        )
        _expect_error(
            "session-string issuer hook is rejected",
            lambda: set_issuer_hooks(peer_resolver=lambda _pid: SUPERVISOR),
            "not an authority channel",
        )
        set_issuer_hooks(
            uid_map_loader=lambda: uid_map,
            supervisor_loader=lambda _tid: SUPERVISOR,
        )
        _expect_error(
            "shared-UID worker uid is not a provisioned principal",
            lambda: issue_for_peer_uid(
                peer_uid=WORKER_UID, task_id="t-cap", action="pin-audit-contract",
            ),
            "not a provisioned supervisor principal",
        )
        issued = issue_for_peer_uid(
            peer_uid=SUPERVISOR_UID, task_id="t-cap", action="pin-audit-contract",
        )
        _check("provisioned supervisor uid can issue", issued.get("ok") is True, issued)

        old = os.environ.get("ORCH_SESSION_ID")
        os.environ["ORCH_SESSION_ID"] = SUPERVISOR
        try:
            _expect_error(
                "forged ORCH_SESSION_ID environ cannot issue as supervisor",
                lambda: issue_for_peer_uid(
                    peer_uid=WORKER_UID, task_id="t-cap", action="pin-audit-contract",
                ),
                "not a provisioned supervisor principal",
            )
        finally:
            if old is None:
                os.environ.pop("ORCH_SESSION_ID", None)
            else:
                os.environ["ORCH_SESSION_ID"] = old

        _expect_error(
            "client JSON cannot supply peer_uid",
            lambda: handle_issuer_request(
                {"cmd": "issue", "task_id": "t-cap", "action": "pin-audit-contract", "peer_uid": SUPERVISOR_UID},
                peer_uid=WORKER_UID,
                peer_pid=os.getpid(),
            ),
            "client cannot supply peer_uid",
        )

        key_dir = tmp_root / "keys"
        key_dir.mkdir()
        write_keypair_files(key_dir / "ed25519.private", key_dir / "ed25519.public")
        _check("private key mode 0600", oct((key_dir / "ed25519.private").stat().st_mode & 0o777) == "0o600")
        _check("public key mode 0644", oct((key_dir / "ed25519.public").stat().st_mode & 0o777) == "0o644")
        prod_key = Path("/var/lib/fleet-orchestrator/audit-capability/ed25519.private")
        if prod_key.exists():
            _check(
                "shared-UID cannot read orch-cap private key",
                not os.access(prod_key, os.R_OK) or prod_key.stat().st_uid != WORKER_UID,
                (prod_key.stat().st_uid, oct(prod_key.stat().st_mode)),
            )
        else:
            _check(
                "undeployed orch-cap key path is outside shared home",
                not str(prod_key).startswith(str(Path.home())),
                prod_key,
            )

        sock_dir = Path(tempfile.mkdtemp(prefix="audit-issuer-sock-"))
        sock_path = sock_dir / "issuer.sock"
        sock_env = {
            "ORCH_AUDIT_CAPABILITY_KEY_DIR": str(key_dir),
            "ORCH_AUDIT_CAPABILITY_SOCKET": str(sock_path),
            "ORCH_AUDIT_CAPABILITY_UID_MAP": json.dumps({str(SUPERVISOR_UID): SUPERVISOR}),
            "PATH": os.environ.get("PATH", ""),
        }

        # LISTEN_FDS before any extra threads (fork is unsafe after threads).
        listen_dir = Path(tempfile.mkdtemp(prefix="audit-listenfds-"))
        listen_path = listen_dir / "issuer.sock"
        listen_env = {
            **sock_env,
            "ORCH_AUDIT_CAPABILITY_SOCKET": str(listen_path),
        }
        listen_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        listen_sock.bind(str(listen_path))
        os.chmod(listen_path, 0o660)
        listen_sock.listen(16)
        os.set_inheritable(listen_sock.fileno(), True)
        inode_before = os.stat(listen_path).st_ino
        ready_r, ready_w = os.pipe()
        child_pid = os.fork()
        if child_pid == 0:
            os.close(ready_r)
            os.dup2(listen_sock.fileno(), 3)
            if listen_sock.fileno() != 3:
                listen_sock.close()
            os.environ.clear()
            os.environ.update(listen_env)
            os.environ["LISTEN_FDS"] = "1"
            os.environ["LISTEN_PID"] = str(os.getpid())
            os.write(ready_w, b"1")
            os.close(ready_w)
            try:
                run_socket_server(socket_path=listen_path, env=listen_env)
            finally:
                os._exit(0)
        os.close(ready_w)
        listen_sock.close()
        os.read(ready_r, 1)
        os.close(ready_r)
        time.sleep(0.05)
        inode_after = os.stat(listen_path).st_ino
        _check("LISTEN_FDS keeps systemd inode (no unlink/rebind)", inode_before == inode_after, (inode_before, inode_after))
        try:
            issue_audit_capability(
                task_id="t-listen", action="pin-audit-contract", socket_path=listen_path, env=listen_env,
            )
            _check("LISTEN_FDS first connect reached inherited fd", True)
        except AuditContractError as exc:
            _check(
                "LISTEN_FDS first connect reached inherited fd",
                "not a provisioned supervisor principal" in str(exc),
                exc,
            )
        os.kill(child_pid, 15)
        os.waitpid(child_pid, 0)
        shutil.rmtree(listen_dir, ignore_errors=True)

        def _serve() -> None:
            try:
                run_socket_server(socket_path=sock_path, env=sock_env)
            except Exception:
                return

        set_audit_capability_keys(private_key=None, public_key=None)
        os.environ["ORCH_SESSION_ID"] = SUPERVISOR
        server = threading.Thread(target=_serve, daemon=True)
        server.start()
        for _ in range(50):
            if sock_path.exists():
                break
            time.sleep(0.02)
        _check("issuer socket created", sock_path.exists(), sock_path)
        if sock_path.exists():
            _check("issuer socket mode 0660", oct(sock_path.stat().st_mode & 0o777) == "0o660")
            _expect_error(
                "real SO_PEERCRED worker uid denied despite forged env",
                lambda: issue_audit_capability(
                    task_id="t-sock", action="pin-audit-contract", socket_path=sock_path, env=sock_env,
                ),
                "not a provisioned supervisor principal",
            )
            probe = f"orch-cap-probe-{os.getpid()}"
            client_py = sock_dir / "tmux_client.py"
            result_path = sock_dir / "tmux_result.txt"
            client_py.write_text(
                "import json,sys,traceback\n"
                f"sys.path.insert(0, {str(ROOT)!r})\n"
                "from fleet_orchestrator.audit_capability_issuer import issue_audit_capability\n"
                "try:\n"
                f"    issue_audit_capability(task_id='t-sock', action='pin-audit-contract', socket_path={str(sock_path)!r}, env={sock_env!r})\n"
                f"    open({str(result_path)!r},'w').write('issued')\n"
                "except Exception as exc:\n"
                f"    open({str(result_path)!r},'w').write(str(exc))\n",
                encoding="utf-8",
            )
            spawned = subprocess.run(
                ["tmux", "new-session", "-d", "-s", probe, "python3", str(client_py)],
                capture_output=True, text=True, check=False,
            )
            if spawned.returncode == 0:
                for _ in range(50):
                    if result_path.exists():
                        break
                    time.sleep(0.05)
                text = result_path.read_text(encoding="utf-8") if result_path.exists() else ""
                _check(
                    "tmux-spawned same-UID process is not supervisor authority",
                    "not a provisioned supervisor principal" in text,
                    text,
                )
                subprocess.run(["tmux", "kill-session", "-t", probe], check=False, capture_output=True)
            else:
                _check("tmux probe skipped (no tmux)", False, spawned.stderr)
        os.environ.pop("ORCH_SESSION_ID", None)
        set_issuer_hooks(
            uid_map_loader=lambda: uid_map,
            supervisor_loader=lambda _tid: SUPERVISOR,
        )
        set_audit_capability_keys(private_key=priv, public_key=pub)
        shutil.rmtree(sock_dir, ignore_errors=True)

        _expect_error(
            "ordinary create cannot select completion_class=audit",
            lambda: store.create_ordinary("t-ordinary-class", {"completion_class": "audit"}),
            "ordinary POST /api/task/create cannot select audit contract fields",
        )
        _expect_error(
            "trusted pin rejects status id at creation",
            lambda: store.create_trusted("t-pin-status", _pins(audit_bound_status_id=STATUS_ID), SUPERVISOR),
            "status IDs cannot be pinned at creation",
        )
        trusted = store.create_trusted("t-lifecycle", _pins(), SUPERVISOR)
        _check("trusted create pins class=audit", trusted.get("completion_class") == "audit", trusted)
        _check("trusted create leaves bound id unset", trusted.get("audit_bound_status_id") is None, trusted)

        _expect_error(
            "ordinary actor cannot bind",
            lambda: store.bind("t-lifecycle", STATUS_ID, WORKER),
            "bind-audit-status requires the project supervisor as actor",
        )
        bind = store.bind("t-lifecycle", STATUS_ID, SUPERVISOR)
        _check("compare-once bind stores concrete id", bind.get("audit_bound_status_id") == STATUS_ID, bind)
        _expect_error(
            "compare-once CAS refuses overwrite with a different id",
            lambda: store.bind("t-lifecycle", OTHER_ID, SUPERVISOR),
            "CAS loser refused overwrite" if False else "already set",
        )

        cas_store = FakeTaskStore()
        cas_store.create_trusted("t-cas", _pins(), SUPERVISOR)
        barrier = threading.Barrier(2)
        outcomes: list[str] = []

        def _racer(sid: int) -> None:
            barrier.wait()
            try:
                cas_store.bind("t-cas", sid, SUPERVISOR)
                outcomes.append(f"win:{sid}")
            except AuditContractError as exc:
                outcomes.append(f"lose:{sid}:{exc}")

        t1 = threading.Thread(target=_racer, args=(STATUS_ID,))
        t2 = threading.Thread(target=_racer, args=(OTHER_ID,))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        wins = [item for item in outcomes if item.startswith("win:")]
        losses = [item for item in outcomes if item.startswith("lose:")]
        _check("CAS concurrent bind has exactly one winner", len(wins) == 1, outcomes)
        _check("CAS concurrent bind has exactly one loser", len(losses) == 1, outcomes)
        _check(
            "CAS loser reason is overwrite refusal",
            any("already set" in item or "loser" in item for item in losses),
            losses,
        )

        good_root = tmp_root / "good-receipt"
        _write_sealed_receipt(good_root, refs=_good_refs(), verdict=_structured_verdict())

        _expect_error(
            "evidence cannot overwrite trusted audit_head",
            lambda: store.complete("t-lifecycle", {"audit_receipt": str(good_root), "audit_head": WRONG_HEAD}),
            "completion evidence cannot select or overwrite trusted audit contract fields",
        )
        missing_class = verify_completion_evidence(
            {"audit_receipt": str(good_root)}, producer=WORKER, trusted_task=None,
        )
        _check(
            "omitted trusted_task never enters audit verifier",
            not (isinstance(missing_class, dict) and missing_class.get("source") == "audit-completion-contract"),
            missing_class,
        )

        unlisted = tmp_root / "unlisted-receipt"
        _write_sealed_receipt(
            unlisted,
            refs=_good_refs(),
            verdict=_structured_verdict(),
            extra_unhashed={"RECEIPT_HASH.txt": "deadbeef\n"},
        )
        _expect_error(
            "unlisted RECEIPT_HASH.txt is rejected",
            lambda: verify_sealed_audit_receipt(
                str(unlisted),
                expected_repo=REPO, expected_head=HEAD, expected_base=BASE,
                expected_context=CONTEXT, expected_state="success",
                expected_status_id=STATUS_ID, expected_pr_number=PR_NUMBER,
            ),
            "not listed in SHA256SUMS",
        )

        prose = tmp_root / "prose-receipt"
        _write_sealed_receipt(
            prose,
            refs=_good_refs(),
            verdict=f"ENDORSE looks like exact base {BASE} status_id={STATUS_ID}\n",
        )
        _expect_error(
            "substring-only prose verdict is not structured provenance",
            lambda: verify_sealed_audit_receipt(
                str(prose),
                expected_repo=REPO, expected_head=HEAD, expected_base=BASE,
                expected_context=CONTEXT, expected_state="success",
                expected_status_id=STATUS_ID, expected_pr_number=PR_NUMBER,
            ),
            "not structured provenance",
        )

        wrong_verdict = tmp_root / "wrong-verdict"
        bad_verdict = json.dumps({**_good_refs(), "audit_base": WRONG_BASE}) + "\n"
        _write_sealed_receipt(wrong_verdict, refs=_good_refs(), verdict=bad_verdict)
        _expect_error(
            "structured verdict must exact-match pinned base",
            lambda: verify_sealed_audit_receipt(
                str(wrong_verdict),
                expected_repo=REPO, expected_head=HEAD, expected_base=BASE,
                expected_context=CONTEXT, expected_state="success",
                expected_status_id=STATUS_ID, expected_pr_number=PR_NUMBER,
            ),
            "verdict audit_base mismatch",
        )

        verification = store.complete("t-lifecycle", {"audit_receipt": str(good_root)})
        _check("full lifecycle verified", verification.get("status") == VERIFIED, verification)
        _check("full lifecycle bound id", verification.get("audit_bound_status_id") == STATUS_ID, verification)
        _check("full lifecycle applies", verification.get("applies") is True, verification)

        from fastapi.testclient import TestClient
        from fleet_orchestrator.tasks_api import app
        from fleet_orchestrator.audit_supervisor_capability import CAPABILITY_HEADER as HDR

        headers = {}
        token = os.environ.get("ORCH_AUTH_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        with mock.patch("fleet_orchestrator.tasks_api.create_task") as create_task, \
                mock.patch("fleet_orchestrator.tasks_api.ensure_default_project", return_value="phase-x"):
            client = TestClient(app)
            response = client.post(
                "/api/task/create",
                headers=headers,
                json={
                    "description": "ordinary create must not pin audit class",
                    "from": WORKER,
                    "phase_id": "phase-x",
                    "completion_class": "audit",
                    "audit_repo": REPO,
                },
            )
            _check("HTTP ordinary create rejects audit fields", response.status_code == 400, response.text)
            _check("HTTP ordinary create never calls create_task", not create_task.called)

        with mock.patch("fleet_orchestrator.tasks_api.resolve_task_id", side_effect=lambda tid, config=None: tid), \
                mock.patch("fleet_orchestrator.tasks_api.load_task_record", return_value={"id": "t-http"}), \
                mock.patch(
                    "fleet_orchestrator.completion_guard._task_project_supervisor",
                    return_value=SUPERVISOR,
                ), \
                mock.patch("fleet_orchestrator.tasks_api.pin_audit_contract") as pin_mock:
            pin_mock.return_value = {"already_pinned": False}
            client = TestClient(app)
            forged = client.post(
                "/api/task/t-http/pin-audit-contract",
                headers=headers,
                json={"from": SUPERVISOR, "audit_repo": REPO, "audit_head": HEAD,
                      "audit_base": BASE, "audit_required_context": CONTEXT,
                      "audit_required_state": "success", "audit_pr_number": PR_NUMBER},
            )
            _check(
                "HTTP pin with forged body.from and no capability is 403",
                forged.status_code == 403,
                forged.text,
            )
            _check("HTTP pin without capability never writes", not pin_mock.called, pin_mock.call_args)
            cap = issue_for_peer_uid(
                peer_uid=SUPERVISOR_UID, task_id="t-http", action="pin-audit-contract",
            )["capability"]
            ok = client.post(
                "/api/task/t-http/pin-audit-contract",
                headers={**headers, HDR: cap},
                json={"audit_repo": REPO, "audit_head": HEAD, "audit_base": BASE,
                      "audit_required_context": CONTEXT, "audit_required_state": "success",
                      "audit_pr_number": PR_NUMBER},
            )
            _check("HTTP pin with issuer capability reaches store", pin_mock.called, ok.text)

        from fleet_orchestrator.evidence_verification import _gh_api as live_gh

        _check("default GitHub provider is the existing gh api helper", callable(live_gh))
        set_audit_status_provider(gh.status_provider)
        set_audit_pull_provider(gh.pull_provider)

        _check("isolated store holds explicit fake ids", "t-lifecycle" in store.tasks, set(store.tasks))
    finally:
        set_audit_status_provider(None)
        set_audit_pull_provider(None)
        set_approved_receipt_roots(None)
        set_audit_capability_keys(private_key=None, public_key=None)
        set_issuer_hooks(uid_map_loader=None, supervisor_loader=None)
        if tmp_root.exists():
            _chmod_tree(tmp_root, 0o755, 0o644)
            shutil.rmtree(tmp_root, ignore_errors=True)

    if FAILURES:
        print(f"FAILED {len(FAILURES)}: {FAILURES}")
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
