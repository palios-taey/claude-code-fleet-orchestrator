#!/usr/bin/env python3
"""Isolated two-phase audit completion contract (task-05a27e83).

No live Neo4j, Redis, or GitHub. Fake task store + fake status/pull providers.
Every rejection asserts an exact reason fragment. Bare except never PASSes.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

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
from fleet_orchestrator.evidence_verification import (  # noqa: E402
    VERIFIED,
    verify_completion_evidence,
)


FAILURES: list[str] = []
SUPERVISOR = "conductor-codex"
WORKER = "infra-grok"
REPO = "palios-taey/claude-code-fleet-orchestrator"
HEAD = "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
BASE = "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
WRONG_HEAD = "cccccccccccccccccccccccccccccccccccccccc"
WRONG_BASE = "dddddddddddddddddddddddddddddddddddddddd"
CONTEXT = "audit/gatekeeper"
STATUS_ID = 52572591788
WRONG_ID = 11111111111
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
    except Exception as exc:  # noqa: BLE001 — must not PASS unrelated failures
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
    """In-memory store. Pins/bind/complete use production contract functions."""

    def __init__(self, supervisor: str = SUPERVISOR) -> None:
        self.supervisor = supervisor
        self.tasks: dict[str, dict] = {}

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
        require_supervisor_actor(
            {"project_supervisor": self.supervisor},
            actor,
            "trusted-create",
        )
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
        if is_audit_task(task):
            mismatches = [
                key for key in (
                    "audit_repo",
                    "audit_head",
                    "audit_base",
                    "audit_required_context",
                    "audit_required_state",
                    "audit_pr_number",
                )
                if task.get(key) != normalized[key]
            ]
            if mismatches:
                raise AuditContractError(
                    "trusted audit pins are immutable after creation; refuse overwrite of "
                    + ", ".join(mismatches)
                )
            return {"already_pinned": True, **normalized}
        task.update(normalized)
        return {"already_pinned": False, **normalized}

    def bind(self, task_id: str, status_id: int, actor: str) -> dict:
        task = self.tasks.get(task_id)
        if task is None:
            raise AuditContractError(f"task {task_id} not found")
        require_supervisor_actor(task, actor, "bind-audit-status")
        result = compare_once_bind_status(task, status_id=int(status_id))
        if not result.get("already_bound"):
            task["audit_bound_status_id"] = int(result["audit_bound_status_id"])
        return result

    def complete(self, task_id: str, evidence: dict, producer: str = WORKER) -> dict:
        task = self.tasks.get(task_id)
        if task is None:
            raise AuditContractError(f"task {task_id} not found")
        verification = verify_completion_evidence(
            evidence,
            producer=producer,
            trusted_task=task,
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
        for name in dirnames + filenames:
            os.chmod(Path(dirpath) / name, file_mode if name in filenames else dir_mode)


def _write_sealed_receipt(
    root: Path,
    *,
    refs: dict,
    verdict: str,
    extra_files: dict[str, str] | None = None,
    dir_mode: int = 0o555,
    file_mode: int = 0o444,
    include_refs_in_sums: bool = True,
    sums_extra: list[str] | None = None,
) -> Path:
    if root.exists():
        _chmod_tree(root, 0o755, 0o644)
        shutil.rmtree(root)
    root.mkdir(parents=True)
    files = {"verdict-receipt.txt": verdict, **(extra_files or {})}
    if include_refs_in_sums or "refs.json" in (extra_files or {}):
        files["refs.json"] = json.dumps(refs, indent=2, sort_keys=True) + "\n"
    else:
        (root / "refs.json").write_text(json.dumps(refs) + "\n", encoding="utf-8")
    entries = []
    for name, content in files.items():
        path = root / name
        path.write_text(content, encoding="utf-8")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        entries.append(f"{digest}  {name}")
    if include_refs_in_sums is False:
        entries = [line for line in entries if not line.endswith("  refs.json")]
    if sums_extra:
        entries.extend(sums_extra)
    (root / "SHA256SUMS").write_text("\n".join(entries) + "\n", encoding="utf-8")
    for child in root.iterdir():
        os.chmod(child, file_mode)
    os.chmod(root, dir_mode)
    return root


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


def _install_matching_github(gh: FakeGitHub) -> None:
    gh.pulls[(REPO, PR_NUMBER)] = {
        "number": PR_NUMBER,
        "head_sha": HEAD,
        "base_sha": BASE,
        "html_url": f"https://github.com/{REPO}/pull/{PR_NUMBER}",
    }
    gh.statuses[(REPO, HEAD)] = [{
        "id": STATUS_ID,
        "context": CONTEXT,
        "state": "success",
        "sha": HEAD,
    }]


def _http_create_rejects_audit_fields() -> None:
    from fastapi.testclient import TestClient
    from fleet_orchestrator.tasks_api import app

    with mock.patch("fleet_orchestrator.tasks_api.create_task") as create_task, \
            mock.patch("fleet_orchestrator.tasks_api.ensure_default_project", return_value="phase-x"):
        client = TestClient(app)
        headers = {}
        token = os.environ.get("ORCH_AUTH_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        response = client.post(
            "/api/task/create",
            headers=headers,
            json={
                "description": "ordinary create must not pin audit class",
                "from": WORKER,
                "phase_id": "phase-x",
                "completion_class": "audit",
                "audit_repo": REPO,
                "audit_head": HEAD,
                "audit_base": BASE,
                "audit_required_context": CONTEXT,
                "audit_required_state": "success",
                "audit_pr_number": PR_NUMBER,
            },
        )
        _check(
            "HTTP ordinary create rejects audit fields",
            response.status_code == 400,
            response.text,
        )
        _check(
            "HTTP ordinary create names forbidden fields",
            "ordinary POST /api/task/create cannot select audit contract fields" in response.text,
            response.text,
        )
        _check("HTTP ordinary create never calls create_task", not create_task.called, create_task.call_args)


def main() -> int:
    tmp_root = Path(tempfile.mkdtemp(prefix="audit-completion-05a27e83-"))
    gh = FakeGitHub()
    store = FakeTaskStore()
    set_approved_receipt_roots((tmp_root,))
    set_audit_status_provider(gh.status_provider)
    set_audit_pull_provider(gh.pull_provider)
    try:
        _install_matching_github(gh)

        _expect_error(
            "ordinary create cannot select completion_class=audit",
            lambda: store.create_ordinary("t-ordinary-class", {"completion_class": "audit"}),
            "ordinary POST /api/task/create cannot select audit contract fields",
        )
        _expect_error(
            "ordinary create cannot select audit_head",
            lambda: store.create_ordinary("t-ordinary-head", {"audit_head": HEAD}),
            "audit_head",
        )
        _expect_error(
            "ordinary create cannot select status id",
            lambda: store.create_ordinary("t-ordinary-id", {"audit_bound_status_id": STATUS_ID}),
            "audit_bound_status_id",
        )

        _expect_error(
            "trusted pin rejects status id at creation",
            lambda: store.create_trusted(
                "t-pin-status",
                _pins(audit_bound_status_id=STATUS_ID),
                SUPERVISOR,
            ),
            "status IDs cannot be pinned at creation",
        )
        _expect_error(
            "trusted create requires supervisor actor",
            lambda: store.create_trusted("t-pin-worker", _pins(), WORKER),
            "requires the project supervisor as actor",
        )

        trusted = store.create_trusted("t-lifecycle", _pins(), SUPERVISOR)
        _check("trusted create pins class=audit", trusted.get("completion_class") == "audit", trusted)
        _check("trusted create leaves bound id unset", trusted.get("audit_bound_status_id") is None, trusted)
        _check("trusted create stores exact PR number", trusted.get("audit_pr_number") == PR_NUMBER, trusted)

        _expect_error(
            "ordinary actor cannot pin",
            lambda: store.pin("t-lifecycle", _pins(), WORKER),
            "pin-audit-contract requires the project supervisor as actor",
        )
        _expect_error(
            "pin refuses overwrite of trusted head",
            lambda: store.pin("t-lifecycle", _pins(audit_head=WRONG_HEAD), SUPERVISOR),
            "refuse overwrite of audit_head",
        )
        _expect_error(
            "ordinary actor cannot bind",
            lambda: store.bind("t-lifecycle", STATUS_ID, WORKER),
            "bind-audit-status requires the project supervisor as actor",
        )

        _expect_error(
            "bind rejects unknown status id",
            lambda: store.bind("t-lifecycle", WRONG_ID, SUPERVISOR),
            f"status id {WRONG_ID} not found",
        )
        gh.statuses[(REPO, HEAD)].append({
            "id": WRONG_ID,
            "context": "r5-audit-gate",
            "state": "success",
            "sha": HEAD,
        })
        _expect_error(
            "bind rejects wrong context even when id exists",
            lambda: store.bind("t-lifecycle", WRONG_ID, SUPERVISOR),
            "status context mismatch",
        )

        gh.pulls[(REPO, PR_NUMBER)]["head_sha"] = WRONG_HEAD
        _expect_error(
            "bind rejects PR head/base mismatch",
            lambda: store.bind("t-lifecycle", STATUS_ID, SUPERVISOR),
            "server-side PR provenance mismatch",
        )
        gh.pulls[(REPO, PR_NUMBER)]["head_sha"] = HEAD

        bind = store.bind("t-lifecycle", STATUS_ID, SUPERVISOR)
        _check("compare-once bind stores concrete id", bind.get("audit_bound_status_id") == STATUS_ID, bind)
        _check("store bound id is immutable int", store.tasks["t-lifecycle"]["audit_bound_status_id"] == STATUS_ID)
        idempotent = store.bind("t-lifecycle", STATUS_ID, SUPERVISOR)
        _check("same-id bind is idempotent", idempotent.get("already_bound") is True, idempotent)
        _expect_error(
            "compare-once refuses overwrite with a different id",
            lambda: store.bind("t-lifecycle", WRONG_ID, SUPERVISOR),
            "compare-once refuses overwrite",
        )

        good_root = tmp_root / "good-receipt"
        _write_sealed_receipt(
            good_root,
            refs=_good_refs(),
            verdict=(
                f"ENDORSE audit of {REPO}@{HEAD} base={BASE} "
                f"status_id={STATUS_ID} pr=#{PR_NUMBER}\n"
            ),
        )

        _expect_error(
            "evidence cannot overwrite trusted audit_head",
            lambda: store.complete(
                "t-lifecycle",
                {"audit_receipt": str(good_root), "audit_head": WRONG_HEAD},
            ),
            "completion evidence cannot select or overwrite trusted audit contract fields",
        )
        _expect_error(
            "evidence cannot self-select completion_class",
            lambda: store.complete(
                "t-lifecycle",
                {"audit_receipt": str(good_root), "completion_class": "audit"},
            ),
            "completion_class",
        )

        missing_class = verify_completion_evidence(
            {"audit_receipt": str(good_root)},
            producer=WORKER,
            trusted_task=None,
        )
        _check(
            "omitted trusted_task never enters audit verifier",
            not (isinstance(missing_class, dict) and missing_class.get("source") == "audit-completion-contract"),
            missing_class,
        )
        override_without_task = verify_completion_evidence(
            {"audit_receipt": str(good_root), "audit_head": HEAD, "completion_class": "audit"},
            producer=WORKER,
            trusted_task=None,
        )
        _expect_unverified(
            "missing-class evidence cannot self-select audit fields",
            override_without_task,
            "completion evidence cannot select or overwrite trusted audit contract fields",
        )

        standard = store.create_ordinary("t-standard")
        _check("ordinary create is not audit class", not is_audit_task(standard), standard)
        standard_result = verify_completion_evidence(
            {"audit_receipt": str(good_root)},
            producer=WORKER,
            trusted_task=standard,
        )
        _check(
            "standard task with only receipt does not take audit path",
            isinstance(standard_result, dict)
            and standard_result.get("source") != "audit-completion-contract"
            and standard_result.get("verified") is not True
            and "no commit_sha" in str(standard_result.get("reason") or ""),
            standard_result,
        )

        unbound = store.create_trusted("t-unbound", _pins(), SUPERVISOR)
        _expect_unverified(
            "audit completion without compare-once bind fails closed",
            verify_completion_evidence(
                {"audit_receipt": str(good_root)},
                producer=WORKER,
                trusted_task=unbound,
            ),
            "prior compare-once bind of audit_bound_status_id",
        )

        file_path = tmp_root / "not-a-dir"
        file_path.write_text("not a directory", encoding="utf-8")
        os.chmod(file_path, 0o444)
        _expect_error(
            "receipt path that is a file is rejected",
            lambda: verify_sealed_audit_receipt(
                str(file_path),
                expected_repo=REPO,
                expected_head=HEAD,
                expected_base=BASE,
                expected_context=CONTEXT,
                expected_state="success",
                expected_status_id=STATUS_ID,
                expected_pr_number=PR_NUMBER,
            ),
            "must be an existing sealed directory",
        )

        symlink_root = tmp_root / "symlink-receipt"
        _write_sealed_receipt(symlink_root, refs=_good_refs(), verdict="ok\n")
        _chmod_tree(symlink_root, 0o755, 0o644)
        (symlink_root / "escape").symlink_to(file_path)
        for child in symlink_root.iterdir():
            if not child.is_symlink():
                os.chmod(child, 0o444)
        os.chmod(symlink_root, 0o555)
        _expect_error(
            "symlink file inside receipt is rejected",
            lambda: verify_sealed_audit_receipt(
                str(symlink_root),
                expected_repo=REPO,
                expected_head=HEAD,
                expected_base=BASE,
                expected_context=CONTEXT,
                expected_state="success",
                expected_status_id=STATUS_ID,
                expected_pr_number=PR_NUMBER,
            ),
            "symlink",
        )

        link_dir = tmp_root / "link-as-root"
        link_dir.symlink_to(good_root)
        _expect_error(
            "symlink receipt root is rejected",
            lambda: verify_sealed_audit_receipt(
                str(link_dir),
                expected_repo=REPO,
                expected_head=HEAD,
                expected_base=BASE,
                expected_context=CONTEXT,
                expected_state="success",
                expected_status_id=STATUS_ID,
                expected_pr_number=PR_NUMBER,
            ),
            "symlink",
        )

        traversal = tmp_root / "traversal-receipt"
        outside = tmp_root / "outside.txt"
        outside.write_text("escaped\n", encoding="utf-8")
        _write_sealed_receipt(
            traversal,
            refs=_good_refs(),
            verdict="ok\n",
            sums_extra=[f"{hashlib.sha256(b'escaped\\n').hexdigest()}  ../outside.txt"],
        )
        _expect_error(
            "SHA256SUMS traversal is rejected",
            lambda: verify_sealed_audit_receipt(
                str(traversal),
                expected_repo=REPO,
                expected_head=HEAD,
                expected_base=BASE,
                expected_context=CONTEXT,
                expected_state="success",
                expected_status_id=STATUS_ID,
                expected_pr_number=PR_NUMBER,
            ),
            "escapes receipt root",
        )

        mode_root = tmp_root / "mode-receipt"
        _write_sealed_receipt(mode_root, refs=_good_refs(), verdict="ok\n", file_mode=0o644)
        _expect_error(
            "0644 receipt files are rejected",
            lambda: verify_sealed_audit_receipt(
                str(mode_root),
                expected_repo=REPO,
                expected_head=HEAD,
                expected_base=BASE,
                expected_context=CONTEXT,
                expected_state="success",
                expected_status_id=STATUS_ID,
                expected_pr_number=PR_NUMBER,
            ),
            "mode must be 0444",
        )

        substring = tmp_root / "substring-receipt"
        _write_sealed_receipt(
            substring,
            refs=_good_refs(audit_base=WRONG_BASE),
            verdict=f"looks like exact base {BASE} appears in this prose\n",
        )
        _expect_error(
            "substring-only base in verdict text is not provenance",
            lambda: verify_sealed_audit_receipt(
                str(substring),
                expected_repo=REPO,
                expected_head=HEAD,
                expected_base=BASE,
                expected_context=CONTEXT,
                expected_state="success",
                expected_status_id=STATUS_ID,
                expected_pr_number=PR_NUMBER,
            ),
            "audit_base mismatch",
        )

        outside_root = Path(tempfile.mkdtemp(prefix="audit-completion-outside-"))
        try:
            foreign = outside_root / "foreign"
            _write_sealed_receipt(foreign, refs=_good_refs(), verdict="ok\n")
            _expect_error(
                "receipt outside approved roots is rejected",
                lambda: verify_sealed_audit_receipt(
                    str(foreign),
                    expected_repo=REPO,
                    expected_head=HEAD,
                    expected_base=BASE,
                    expected_context=CONTEXT,
                    expected_state="success",
                    expected_status_id=STATUS_ID,
                    expected_pr_number=PR_NUMBER,
                ),
                "approved recovery root",
            )
        finally:
            _chmod_tree(outside_root, 0o755, 0o644)
            shutil.rmtree(outside_root, ignore_errors=True)

        set_audit_status_provider(None)
        _expect_error(
            "no live GitHub: missing status provider fails closed",
            lambda: store.complete("t-lifecycle", {"audit_receipt": str(good_root)}),
            "audit status provider is not configured",
        )
        set_audit_status_provider(gh.status_provider)

        verification = store.complete("t-lifecycle", {"audit_receipt": str(good_root)})
        _check("full lifecycle verified", verification.get("status") == VERIFIED, verification)
        _check("full lifecycle source", verification.get("source") == "audit-completion-contract", verification)
        _check("full lifecycle bound id", verification.get("audit_bound_status_id") == STATUS_ID, verification)
        _check("full lifecycle applies", verification.get("applies") is True, verification)
        _check("full lifecycle no merge requirement", "merged" not in str(verification.get("reason") or "").lower())
        _check("GitHub status queried exact head", (REPO, HEAD) in gh.status_calls, gh.status_calls)
        _check("GitHub pull queried exact PR", (REPO, PR_NUMBER) in gh.pull_calls, gh.pull_calls)
        _check("task marked completed", store.tasks["t-lifecycle"]["status"] == "completed")

        _http_create_rejects_audit_fields()

        _check(
            "isolated store only holds explicit fake ids",
            set(store.tasks) == {"t-lifecycle", "t-standard", "t-unbound"},
            set(store.tasks),
        )
    finally:
        set_audit_status_provider(None)
        set_audit_pull_provider(None)
        set_approved_receipt_roots(None)
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
