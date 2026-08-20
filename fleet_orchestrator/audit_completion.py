"""Two-phase audit completion contract (task-05a27e83 / task-8e2f7378).

Phase 1 — trusted creation pins immutable class/repo/head/base/context/state.
Phase 2 — compare-once supervisor bind of concrete GitHub status ID after live
query proves exact repo/head/context/state (+ PR head/base when provided).
Completion verifies the immutable bound ID plus a sealed self-contained receipt.

Ordinary create/evidence payloads cannot select or overwrite the contract.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
APPROVED_RECEIPT_ROOTS = (
    Path("/home/mira/recovery/r5-audit"),
    Path("/home/mira/recovery"),
)

StatusProvider = Callable[[str, str], List[Dict[str, Any]]]
# provider(repo, sha) -> list of status objects (GitHub commit status shape)

_STATUS_PROVIDER: Optional[StatusProvider] = None


class AuditContractError(ValueError):
    """Fail-closed audit contract violation."""


def set_audit_status_provider(provider: Optional[StatusProvider]) -> None:
    """Test hook: inject a fake GitHub status provider (no live network)."""
    global _STATUS_PROVIDER
    _STATUS_PROVIDER = provider


def _require_sha(name: str, value: str) -> str:
    text = str(value or "").strip().lower()
    if not SHA1_RE.fullmatch(text):
        raise AuditContractError(f"{name} must be an exact 40-hex commit SHA")
    return text


def _require_nonempty(name: str, value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        raise AuditContractError(f"{name} is required for audit completion contract")
    return text


def normalize_creation_pins(
    *,
    completion_class: str,
    audit_repo: Optional[str],
    audit_head: Optional[str],
    audit_base: Optional[str],
    audit_required_context: Optional[str],
    audit_required_state: Optional[str],
) -> Dict[str, Any]:
    """Validate pins allowed at trusted creation time (no status ID)."""
    klass = str(completion_class or "standard").strip().lower() or "standard"
    if klass != "audit":
        return {
            "completion_class": "standard",
            "audit_repo": None,
            "audit_head": None,
            "audit_base": None,
            "audit_required_context": None,
            "audit_required_state": None,
            "audit_bound_status_id": None,
        }
    repo = _require_nonempty("audit_repo", audit_repo)
    if "/" not in repo or repo.count("/") != 1:
        raise AuditContractError("audit_repo must be OWNER/REPO")
    head = _require_sha("audit_head", audit_head or "")
    base = _require_sha("audit_base", audit_base or "")
    if head == base:
        raise AuditContractError("audit_head and audit_base must differ")
    context = _require_nonempty("audit_required_context", audit_required_context)
    state = _require_nonempty("audit_required_state", audit_required_state).lower()
    if state not in {"success", "failure", "error", "pending"}:
        raise AuditContractError("audit_required_state must be a GitHub status state")
    return {
        "completion_class": "audit",
        "audit_repo": repo,
        "audit_head": head,
        "audit_base": base,
        "audit_required_context": context,
        "audit_required_state": state,
        "audit_bound_status_id": None,
    }


def assert_no_audit_override_in_evidence(evidence: Optional[Dict[str, Any]]) -> None:
    """Ordinary evidence cannot select/overwrite the trusted audit contract."""
    if not isinstance(evidence, dict):
        return
    forbidden = {
        "completion_class",
        "audit_repo",
        "audit_head",
        "audit_base",
        "audit_required_context",
        "audit_required_state",
        "audit_bound_status_id",
        "required_audit_contexts",
        "audit_contexts",
    }
    present = sorted(k for k in forbidden if k in evidence and evidence.get(k) not in (None, "", [], {}))
    if present:
        raise AuditContractError(
            "completion evidence cannot select or overwrite trusted audit contract fields: "
            + ", ".join(present)
        )


def _is_under(root: Path, path: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def resolve_sealed_receipt_root(claimed: str) -> Path:
    """Resolve receipt path under approved recovery roots; reject symlinks/traversal."""
    raw = _require_nonempty("audit_receipt", claimed)
    if ".." in Path(raw).parts:
        raise AuditContractError("audit_receipt path must not contain '..'")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise AuditContractError("audit_receipt must be an absolute path under an approved recovery root")
    # Reject any symlink component on the way to the root.
    accumulated = Path("/")
    for part in candidate.parts[1:]:
        accumulated = accumulated / part
        if accumulated.exists() and accumulated.is_symlink():
            raise AuditContractError(f"audit_receipt path has symlink component: {accumulated}")
    resolved = candidate.resolve(strict=False)
    if not any(_is_under(root.resolve(), resolved) for root in APPROVED_RECEIPT_ROOTS):
        raise AuditContractError(
            "audit_receipt must resolve under /home/mira/recovery/r5-audit (or /home/mira/recovery)"
        )
    if not resolved.exists() or not resolved.is_dir():
        raise AuditContractError("audit_receipt must be an existing sealed directory")
    return resolved


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def verify_sealed_audit_receipt(
    claimed_path: str,
    *,
    expected_repo: str,
    expected_head: str,
    expected_base: str,
    expected_context: str,
    expected_state: str,
    expected_status_id: int,
) -> Dict[str, Any]:
    """Fail-closed sealed receipt: modes, no symlinks, self-contained SHA256SUMS, provenance bind."""
    root = resolve_sealed_receipt_root(claimed_path)
    if _mode(root) != 0o555:
        raise AuditContractError(f"audit receipt root mode must be 0555, got {oct(_mode(root))}")

    sha_file = root / "SHA256SUMS"
    if not sha_file.is_file() or sha_file.is_symlink():
        raise AuditContractError("audit receipt must contain a non-symlink SHA256SUMS file")
    if _mode(sha_file) != 0o444:
        raise AuditContractError(f"SHA256SUMS mode must be 0444, got {oct(_mode(sha_file))}")

    # Walk tree: every file 0444, every dir 0555, no symlinks anywhere.
    files: List[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dpath = Path(dirpath)
        if dpath.is_symlink():
            raise AuditContractError(f"symlink directory not allowed: {dpath}")
        if _mode(dpath) != 0o555:
            raise AuditContractError(f"directory mode must be 0555: {dpath} ({oct(_mode(dpath))})")
        for name in dirnames:
            child = dpath / name
            if child.is_symlink():
                raise AuditContractError(f"symlink directory not allowed: {child}")
        for name in filenames:
            child = dpath / name
            if child.is_symlink():
                raise AuditContractError(f"symlink file not allowed: {child}")
            if _mode(child) != 0o444:
                raise AuditContractError(f"file mode must be 0444: {child} ({oct(_mode(child))})")
            files.append(child)

    # Parse SHA256SUMS: relative, non-traversing, self-contained.
    entries: Dict[str, str] = {}
    for line in sha_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) < 2:
            raise AuditContractError(f"malformed SHA256SUMS line: {line!r}")
        digest, rel = parts[0], parts[-1]
        if rel.startswith("*"):
            rel = rel[1:]
        if not re.fullmatch(r"[0-9a-f]{64}", digest.lower()):
            raise AuditContractError(f"invalid digest in SHA256SUMS: {digest}")
        if rel.startswith("/") or rel.startswith("\\") or ".." in Path(rel).parts:
            raise AuditContractError(f"SHA256SUMS entry escapes receipt root: {rel}")
        target = (root / rel).resolve(strict=False)
        if not _is_under(root.resolve(), target):
            raise AuditContractError(f"SHA256SUMS entry resolves outside receipt: {rel}")
        if not target.is_file() or target.is_symlink():
            raise AuditContractError(f"SHA256SUMS entry missing or symlink: {rel}")
        actual = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual != digest.lower():
            raise AuditContractError(f"SHA256SUMS mismatch for {rel}")
        entries[rel] = digest.lower()

    if "verdict-receipt.txt" not in entries and "verdict_receipt.txt" not in entries:
        # allow either name
        verdict_names = [p.name for p in files if "verdict" in p.name.lower() and p.suffix == ".txt"]
        if not verdict_names:
            raise AuditContractError("sealed receipt must include a verdict receipt text file in SHA256SUMS")

    # Provenance bind: receipt must name exact repo/head/base/context/state/status id.
    # Prefer structured refs.json if present; else scan verdict text.
    blob = ""
    for name in ("refs.json", "verdict-receipt.txt", "verdict_receipt.txt", "RECEIPT.md"):
        p = root / name
        if p.is_file():
            blob += "\n" + p.read_text(encoding="utf-8", errors="replace")
    if not blob:
        blob = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in files if p.suffix in {".txt", ".md", ".json"})

    def _must_contain(label: str, value: str) -> None:
        if value not in blob:
            raise AuditContractError(f"sealed receipt does not bind {label}={value}")

    _must_contain("audit_repo", expected_repo)
    _must_contain("audit_head", expected_head)
    _must_contain("audit_base", expected_base)
    _must_contain("audit_required_context", expected_context)
    # status id + state
    if str(expected_status_id) not in blob:
        raise AuditContractError(f"sealed receipt does not bind audit_bound_status_id={expected_status_id}")
    if expected_state not in blob.lower() and expected_state not in blob:
        # soft: allow SUCCESS uppercase
        if expected_state.upper() not in blob and expected_state.capitalize() not in blob:
            raise AuditContractError(f"sealed receipt does not bind audit_required_state={expected_state}")

    return {
        "receipt_root": str(root),
        "entries": len(entries),
        "sha256sums": str(sha_file),
    }


def _default_status_provider(repo: str, sha: str) -> List[Dict[str, Any]]:
    from .evidence_verification import _gh_api

    payload = _gh_api(f"repos/{repo}/commits/{sha}/statuses?per_page=100")
    if not isinstance(payload, list):
        raise AuditContractError("GitHub statuses response was not a list")
    return payload


def load_commit_statuses(repo: str, sha: str) -> List[Dict[str, Any]]:
    provider = _STATUS_PROVIDER or _default_status_provider
    return provider(repo, sha)


def find_status_by_id(statuses: List[Dict[str, Any]], status_id: int) -> Optional[Dict[str, Any]]:
    for row in statuses:
        try:
            if int(row.get("id")) == int(status_id):
                return row
        except (TypeError, ValueError):
            continue
    return None


@dataclass(frozen=True)
class AuditPins:
    completion_class: str
    audit_repo: str
    audit_head: str
    audit_base: str
    audit_required_context: str
    audit_required_state: str
    audit_bound_status_id: Optional[int]


def pins_from_task(task: Dict[str, Any]) -> AuditPins:
    klass = str(task.get("completion_class") or "standard").strip().lower()
    bound = task.get("audit_bound_status_id")
    bound_id: Optional[int]
    try:
        bound_id = int(bound) if bound is not None and str(bound).strip() != "" else None
    except (TypeError, ValueError):
        bound_id = None
    return AuditPins(
        completion_class=klass or "standard",
        audit_repo=str(task.get("audit_repo") or "").strip(),
        audit_head=str(task.get("audit_head") or "").strip().lower(),
        audit_base=str(task.get("audit_base") or "").strip().lower(),
        audit_required_context=str(task.get("audit_required_context") or "").strip(),
        audit_required_state=str(task.get("audit_required_state") or "").strip().lower(),
        audit_bound_status_id=bound_id,
    )


def is_audit_task(task: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(task, dict):
        return False
    return pins_from_task(task).completion_class == "audit"


def compare_once_bind_status(
    task: Dict[str, Any],
    *,
    status_id: int,
    pr_head_sha: str,
    pr_base_sha: str,
) -> Dict[str, Any]:
    """Supervisor/internal compare-once bind after querying exact status + PR provenance."""
    pins = pins_from_task(task)
    if pins.completion_class != "audit":
        raise AuditContractError("compare-once bind requires completion_class=audit")
    if pins.audit_bound_status_id is not None:
        if int(pins.audit_bound_status_id) == int(status_id):
            return {"already_bound": True, "audit_bound_status_id": int(status_id)}
        raise AuditContractError(
            f"audit_bound_status_id already set to {pins.audit_bound_status_id}; compare-once refuses overwrite"
        )
    head = _require_sha("audit_head", pins.audit_head)
    base = _require_sha("audit_base", pins.audit_base)
    pr_head = _require_sha("pr_head_sha", pr_head_sha)
    pr_base = _require_sha("pr_base_sha", pr_base_sha)
    if pr_head != head or pr_base != base:
        raise AuditContractError(
            f"PR provenance mismatch: expected head={head} base={base}, got head={pr_head} base={pr_base}"
        )
    statuses = load_commit_statuses(pins.audit_repo, head)
    row = find_status_by_id(statuses, int(status_id))
    if row is None:
        raise AuditContractError(
            f"status id {status_id} not found on {pins.audit_repo}@{head}"
        )
    context = str(row.get("context") or "").strip()
    state = str(row.get("state") or "").strip().lower()
    if context != pins.audit_required_context:
        raise AuditContractError(
            f"status context mismatch: required {pins.audit_required_context!r}, got {context!r}"
        )
    if state != pins.audit_required_state:
        raise AuditContractError(
            f"status state mismatch: required {pins.audit_required_state!r}, got {state!r}"
        )
    # GitHub status payloads do not always embed sha; provider must have been queried for exact head.
    return {
        "already_bound": False,
        "audit_bound_status_id": int(status_id),
        "context": context,
        "state": state,
        "repo": pins.audit_repo,
        "head": head,
        "base": base,
    }


def verify_audit_completion(
    task: Dict[str, Any],
    evidence: Dict[str, Any],
    *,
    producer: str = "",
) -> Dict[str, Any]:
    """Completion-time verification for audit-class tasks."""
    from .evidence_verification import VERIFIED, _unverified

    assert_no_audit_override_in_evidence(evidence)
    pins = pins_from_task(task)
    if pins.completion_class != "audit":
        raise AuditContractError("verify_audit_completion called for non-audit task")
    if pins.audit_bound_status_id is None:
        return _unverified(
            "audit completion requires a prior compare-once bind of audit_bound_status_id "
            "(POST /api/tasks/{id}/bind-audit-status); creation must not invent status IDs",
            repo=pins.audit_repo,
            commit_sha=pins.audit_head,
            required_checks=[pins.audit_required_context],
            producer=producer,
            reject_completion=True,
        )
    receipt = str(evidence.get("audit_receipt") or "").strip()
    if not receipt:
        return _unverified(
            "audit completion evidence must include audit_receipt path only (pins are trusted on OrchTask)",
            repo=pins.audit_repo,
            commit_sha=pins.audit_head,
            required_checks=[pins.audit_required_context],
            producer=producer,
            reject_completion=True,
        )
    try:
        # Re-query status to ensure bound ID still matches contract.
        statuses = load_commit_statuses(pins.audit_repo, pins.audit_head)
        row = find_status_by_id(statuses, int(pins.audit_bound_status_id))
        if row is None:
            raise AuditContractError(
                f"bound status id {pins.audit_bound_status_id} missing on {pins.audit_repo}@{pins.audit_head}"
            )
        if str(row.get("context") or "").strip() != pins.audit_required_context:
            raise AuditContractError("bound status context no longer matches trusted pin")
        if str(row.get("state") or "").strip().lower() != pins.audit_required_state:
            raise AuditContractError("bound status state no longer matches trusted pin")
        sealed = verify_sealed_audit_receipt(
            receipt,
            expected_repo=pins.audit_repo,
            expected_head=pins.audit_head,
            expected_base=pins.audit_base,
            expected_context=pins.audit_required_context,
            expected_state=pins.audit_required_state,
            expected_status_id=int(pins.audit_bound_status_id),
        )
    except AuditContractError as exc:
        return _unverified(
            str(exc),
            repo=pins.audit_repo,
            commit_sha=pins.audit_head,
            required_checks=[pins.audit_required_context],
            producer=producer,
            reject_completion=True,
        )
    return {
        "status": VERIFIED,
        "verified": True,
        "applies": True,
        "source": "audit-completion-contract",
        "repo": pins.audit_repo,
        "commit_sha": pins.audit_head,
        "required_checks": [pins.audit_required_context],
        "producer": producer,
        "verifier": "audit-completion-contract",
        "audit_bound_status_id": int(pins.audit_bound_status_id),
        "audit_base": pins.audit_base,
        "audit_receipt": sealed["receipt_root"],
        "reason": (
            "trusted OrchTask completion_class=audit with compare-once bound status id, "
            "exact head/base pins, and sealed self-contained receipt (no merge required)"
        ),
        "checks": [{
            "name": pins.audit_required_context,
            "kind": "github-commit-status",
            "ok": True,
            "detail": (
                f"id={pins.audit_bound_status_id} state={pins.audit_required_state} "
                f"on {pins.audit_repo}@{pins.audit_head}"
            ),
        }],
    }
