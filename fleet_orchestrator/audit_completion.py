"""Two-phase audit completion contract (task-05a27e83).

Phase 1: trusted supervisor/internal pin of class/repo/head/base/context/state/pr.
         Status IDs are never accepted at creation.
Phase 2: compare-once bind of a concrete GitHub status ID after querying exact
         repo/head/context/state and PR head/base. Completion then verifies the
         immutable bound ID plus a sealed receipt.

Ordinary create/evidence cannot select or overwrite the contract.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence

SHA1_RE = re.compile(r"^[0-9a-f]{40}$")
_DEFAULT_APPROVED_RECEIPT_ROOTS = (
    Path("/home/mira/recovery/r5-audit"),
    Path("/home/mira/recovery"),
)
APPROVED_RECEIPT_ROOTS = _DEFAULT_APPROVED_RECEIPT_ROOTS
RECEIPT_REFS_NAME = "refs.json"
REQUIRED_REFS_FIELDS = (
    "audit_repo",
    "audit_head",
    "audit_base",
    "audit_required_context",
    "audit_required_state",
    "audit_bound_status_id",
    "audit_pr_number",
)

StatusProvider = Callable[[str, str], List[Dict[str, Any]]]
PullProvider = Callable[[str, int], Dict[str, Any]]

_STATUS_PROVIDER: Optional[StatusProvider] = None
_PULL_PROVIDER: Optional[PullProvider] = None

FORBIDDEN_EVIDENCE_AUDIT_FIELDS = frozenset({
    "completion_class",
    "audit_repo",
    "audit_head",
    "audit_base",
    "audit_required_context",
    "audit_required_state",
    "audit_bound_status_id",
    "audit_pr_number",
    "required_audit_contexts",
    "audit_contexts",
    "pr_head_sha",
    "pr_base_sha",
})

ORDINARY_CREATE_AUDIT_FIELDS = frozenset({
    "completion_class",
    "audit_repo",
    "audit_head",
    "audit_base",
    "audit_required_context",
    "audit_required_state",
    "audit_bound_status_id",
    "audit_pr_number",
    "audit_status_id",
    "required_audit_status_ids",
    "required_audit_contexts",
    "audit_contexts",
})


class AuditContractError(ValueError):
    """Fail-closed audit contract violation."""


def set_audit_status_provider(provider: Optional[StatusProvider]) -> None:
    global _STATUS_PROVIDER
    _STATUS_PROVIDER = provider


def set_audit_pull_provider(provider: Optional[PullProvider]) -> None:
    global _PULL_PROVIDER
    _PULL_PROVIDER = provider


def set_approved_receipt_roots(roots: Optional[Sequence[Path]] = None) -> None:
    """Tests inject a throwaway root. Production keeps the recovery allowlist."""
    global APPROVED_RECEIPT_ROOTS
    if roots is None:
        APPROVED_RECEIPT_ROOTS = _DEFAULT_APPROVED_RECEIPT_ROOTS
        return
    APPROVED_RECEIPT_ROOTS = tuple(Path(root) for root in roots)


def assert_no_status_id_at_pin(payload: Optional[Dict[str, Any]]) -> None:
    if not isinstance(payload, dict):
        return
    present = sorted(
        key for key in ("audit_bound_status_id", "audit_status_id", "required_audit_status_ids")
        if payload.get(key) not in (None, "", [], {})
    )
    if present:
        raise AuditContractError(
            "status IDs cannot be pinned at creation; compare-once bind is required "
            f"({', '.join(present)})"
        )


def require_supervisor_actor(task: Dict[str, Any], actor: str, action: str) -> None:
    supervisor = str(task.get("project_supervisor") or "").strip()
    if not supervisor or str(actor or "").strip() != supervisor:
        raise AuditContractError(
            f"{action} requires the project supervisor as actor; ordinary API cannot {action}. "
            f"Next step: POST /api/task/<task-id>/{action} as the project supervisor"
        )


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


def _require_pr_number(value: Any) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError) as exc:
        raise AuditContractError("audit_pr_number must be a positive integer") from exc
    if number <= 0:
        raise AuditContractError("audit_pr_number must be a positive integer")
    return number


def reject_ordinary_create_audit_fields(payload: Optional[Dict[str, Any]]) -> None:
    if not isinstance(payload, dict):
        return
    present = sorted(
        k for k in ORDINARY_CREATE_AUDIT_FIELDS
        if k in payload and payload.get(k) not in (None, "", [], {})
    )
    if "completion_class" in present:
        klass = str(payload.get("completion_class") or "").strip().lower()
        if klass in {"", "standard"}:
            present = [k for k in present if k != "completion_class"]
    if present:
        raise AuditContractError(
            "ordinary POST /api/task/create cannot select audit contract fields "
            f"({', '.join(present)}); trusted supervisor/internal pin is required"
        )


def normalize_supervisor_pins(
    *,
    audit_repo: Optional[str],
    audit_head: Optional[str],
    audit_base: Optional[str],
    audit_required_context: Optional[str],
    audit_required_state: Optional[str],
    audit_pr_number: Any,
) -> Dict[str, Any]:
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
    pr_number = _require_pr_number(audit_pr_number)
    return {
        "completion_class": "audit",
        "audit_repo": repo,
        "audit_head": head,
        "audit_base": base,
        "audit_required_context": context,
        "audit_required_state": state,
        "audit_pr_number": pr_number,
        "audit_bound_status_id": None,
    }


def assert_no_audit_override_in_evidence(evidence: Optional[Dict[str, Any]]) -> None:
    if not isinstance(evidence, dict):
        return
    present = sorted(
        k for k in FORBIDDEN_EVIDENCE_AUDIT_FIELDS
        if k in evidence and evidence.get(k) not in (None, "", [], {})
    )
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
    raw = _require_nonempty("audit_receipt", claimed)
    if ".." in Path(raw).parts:
        raise AuditContractError("audit_receipt path must not contain '..'")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise AuditContractError("audit_receipt must be an absolute path under an approved recovery root")
    accumulated = Path("/")
    for part in candidate.parts[1:]:
        accumulated = accumulated / part
        if accumulated.exists() and accumulated.is_symlink():
            raise AuditContractError(f"audit_receipt path has symlink component: {accumulated}")
    resolved = candidate.resolve(strict=False)
    if not any(_is_under(root.resolve(), resolved) for root in APPROVED_RECEIPT_ROOTS):
        allowed = ", ".join(str(root) for root in APPROVED_RECEIPT_ROOTS)
        raise AuditContractError(
            f"audit_receipt must resolve under an approved recovery root ({allowed})"
        )
    if not resolved.exists() or not resolved.is_dir():
        raise AuditContractError("audit_receipt must be an existing sealed directory")
    return resolved


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.lstat().st_mode)


def parse_verdict_receipt(path: Path) -> Dict[str, Any]:
    """Parse structured verdict fields. Presence of prose is not provenance."""
    text = path.read_text(encoding="utf-8")
    try:
        loaded = json.loads(text)
    except json.JSONDecodeError:
        loaded = None
    parsed: Dict[str, Any] = {}
    if isinstance(loaded, dict):
        parsed = dict(loaded)
    else:
        for line in text.splitlines():
            raw = line.strip()
            if not raw or raw.startswith("#"):
                continue
            if ":" in raw:
                key, _, value = raw.partition(":")
            elif "=" in raw:
                key, _, value = raw.partition("=")
            else:
                continue
            key = key.strip()
            if key in REQUIRED_REFS_FIELDS:
                parsed[key] = value.strip().strip('"').strip("'")
    missing = [field for field in REQUIRED_REFS_FIELDS if field not in parsed]
    if missing:
        raise AuditContractError(
            "verdict receipt is not structured provenance (parse failed for "
            f"{', '.join(missing)}); substring-only text is not accepted"
        )
    return parsed


def verify_sealed_audit_receipt(
    claimed_path: str,
    *,
    expected_repo: str,
    expected_head: str,
    expected_base: str,
    expected_context: str,
    expected_state: str,
    expected_status_id: int,
    expected_pr_number: int,
) -> Dict[str, Any]:
    root = resolve_sealed_receipt_root(claimed_path)
    if _mode(root) != 0o555:
        raise AuditContractError(f"audit receipt root mode must be 0555, got {oct(_mode(root))}")

    sha_file = root / "SHA256SUMS"
    if not sha_file.is_file() or sha_file.is_symlink():
        raise AuditContractError("audit receipt must contain a non-symlink SHA256SUMS file")
    if _mode(sha_file) != 0o444:
        raise AuditContractError(f"SHA256SUMS mode must be 0444, got {oct(_mode(sha_file))}")

    files: List[Path] = []
    on_disk: List[str] = []
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
            rel = child.relative_to(root).as_posix()
            if rel != "SHA256SUMS":
                on_disk.append(rel)

    entries: Dict[str, str] = {}
    for line in sha_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) != 2:
            raise AuditContractError(f"malformed SHA256SUMS line: {line!r}")
        digest, rel = parts[0], parts[1]
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

    listed = set(entries)
    disk = set(on_disk)
    extra = sorted(disk - listed)
    missing_files = sorted(listed - disk)
    if extra:
        raise AuditContractError(
            "sealed receipt contains files not listed in SHA256SUMS "
            f"(manifest itself is the only permitted unlisted file): {', '.join(extra)}"
        )
    if missing_files:
        raise AuditContractError(
            f"SHA256SUMS lists files missing from the receipt: {', '.join(missing_files)}"
        )
    if RECEIPT_REFS_NAME not in entries:
        raise AuditContractError(
            f"sealed receipt must include {RECEIPT_REFS_NAME} in SHA256SUMS"
        )
    verdict_name = next(
        (name for name in ("verdict-receipt.txt", "verdict_receipt.txt", "verdict.json") if name in entries),
        None,
    )
    if verdict_name is None:
        raise AuditContractError(
            "sealed receipt must include a parseable verdict file in SHA256SUMS "
            "(verdict-receipt.txt, verdict_receipt.txt, or verdict.json)"
        )

    refs_path = root / RECEIPT_REFS_NAME
    try:
        refs = json.loads(refs_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise AuditContractError(f"{RECEIPT_REFS_NAME} is not valid JSON: {exc}") from exc
    if not isinstance(refs, dict):
        raise AuditContractError(f"{RECEIPT_REFS_NAME} must be a JSON object")

    missing = [k for k in REQUIRED_REFS_FIELDS if k not in refs]
    if missing:
        raise AuditContractError(f"{RECEIPT_REFS_NAME} missing required fields: {', '.join(missing)}")

    def _exact(field: str, expected: Any) -> None:
        actual = refs.get(field)
        if field in {"audit_head", "audit_base", "audit_required_state"}:
            actual_norm = str(actual or "").strip().lower()
            expected_norm = str(expected or "").strip().lower()
        elif field in {"audit_bound_status_id", "audit_pr_number"}:
            try:
                actual_norm = int(actual)
                expected_norm = int(expected)
            except (TypeError, ValueError) as exc:
                raise AuditContractError(
                    f"{RECEIPT_REFS_NAME}.{field} must be an integer matching the trusted pin"
                ) from exc
        else:
            actual_norm = str(actual or "").strip()
            expected_norm = str(expected or "").strip()
        if actual_norm != expected_norm:
            raise AuditContractError(
                f"{RECEIPT_REFS_NAME}.{field} mismatch: expected {expected_norm!r}, got {actual_norm!r}"
            )

    _exact("audit_repo", expected_repo)
    _exact("audit_head", expected_head)
    _exact("audit_base", expected_base)
    _exact("audit_required_context", expected_context)
    _exact("audit_required_state", expected_state)
    _exact("audit_bound_status_id", expected_status_id)
    _exact("audit_pr_number", expected_pr_number)

    verdict_fields = parse_verdict_receipt(root / verdict_name)
    for field in REQUIRED_REFS_FIELDS:
        expected = {
            "audit_repo": expected_repo,
            "audit_head": expected_head,
            "audit_base": expected_base,
            "audit_required_context": expected_context,
            "audit_required_state": expected_state,
            "audit_bound_status_id": expected_status_id,
            "audit_pr_number": expected_pr_number,
        }[field]
        actual = verdict_fields.get(field)
        if field in {"audit_head", "audit_base", "audit_required_state"}:
            actual_n = str(actual or "").strip().lower()
            expected_n = str(expected or "").strip().lower()
        elif field in {"audit_bound_status_id", "audit_pr_number"}:
            try:
                actual_n = int(actual)
                expected_n = int(expected)
            except (TypeError, ValueError) as exc:
                raise AuditContractError(
                    f"verdict {field} must be an integer matching the trusted pin"
                ) from exc
        else:
            actual_n = str(actual or "").strip()
            expected_n = str(expected or "").strip()
        if actual_n != expected_n:
            raise AuditContractError(
                f"verdict {field} mismatch: expected {expected_n!r}, got {actual_n!r}"
            )

    return {
        "receipt_root": str(root),
        "entries": len(entries),
        "sha256sums": str(sha_file),
        "refs": RECEIPT_REFS_NAME,
        "verdict": verdict_name,
    }


def _default_status_provider(repo: str, sha: str) -> List[Dict[str, Any]]:
    from .evidence_verification import _gh_api

    path = f"repos/{repo}/commits/{sha}/statuses?per_page=100"
    try:
        payload = _gh_api(path)
    except RuntimeError as exc:
        raise AuditContractError(f"github status query failed (fail-closed): {exc}") from exc
    if not isinstance(payload, list):
        raise AuditContractError("github status query must return a list")
    return payload


def _default_pull_provider(repo: str, pr_number: int) -> Dict[str, Any]:
    from .evidence_verification import _gh_api

    path = f"repos/{repo}/pulls/{int(pr_number)}"
    try:
        payload = _gh_api(path)
    except RuntimeError as exc:
        raise AuditContractError(f"github pull query failed (fail-closed): {exc}") from exc
    if not isinstance(payload, dict):
        raise AuditContractError("github pull query must return an object")
    head = payload.get("head") if isinstance(payload.get("head"), dict) else {}
    base = payload.get("base") if isinstance(payload.get("base"), dict) else {}
    return {
        "number": int(payload.get("number") or pr_number),
        "head_sha": str(head.get("sha") or ""),
        "base_sha": str(base.get("sha") or ""),
        "html_url": str(payload.get("html_url") or ""),
    }


def load_commit_statuses(repo: str, sha: str) -> List[Dict[str, Any]]:
    provider = _STATUS_PROVIDER if _STATUS_PROVIDER is not None else _default_status_provider
    return provider(repo, sha)


def load_pull_provenance(repo: str, pr_number: int) -> Dict[str, Any]:
    provider = _PULL_PROVIDER if _PULL_PROVIDER is not None else _default_pull_provider
    payload = provider(repo, int(pr_number))
    if not isinstance(payload, dict):
        raise AuditContractError("pull provider must return a dict")
    head = _require_sha("pr_head_sha", str(payload.get("head_sha") or ""))
    base = _require_sha("pr_base_sha", str(payload.get("base_sha") or ""))
    return {
        "number": int(payload.get("number") or pr_number),
        "head_sha": head,
        "base_sha": base,
        "html_url": str(payload.get("html_url") or ""),
    }


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
    audit_pr_number: Optional[int]
    audit_bound_status_id: Optional[int]


def pins_from_task(task: Dict[str, Any]) -> AuditPins:
    klass = str(task.get("completion_class") or "standard").strip().lower()
    bound = task.get("audit_bound_status_id")
    try:
        bound_id = int(bound) if bound is not None and str(bound).strip() != "" else None
    except (TypeError, ValueError):
        bound_id = None
    pr_raw = task.get("audit_pr_number")
    try:
        pr_number = int(pr_raw) if pr_raw is not None and str(pr_raw).strip() != "" else None
    except (TypeError, ValueError):
        pr_number = None
    return AuditPins(
        completion_class=klass or "standard",
        audit_repo=str(task.get("audit_repo") or "").strip(),
        audit_head=str(task.get("audit_head") or "").strip().lower(),
        audit_base=str(task.get("audit_base") or "").strip().lower(),
        audit_required_context=str(task.get("audit_required_context") or "").strip(),
        audit_required_state=str(task.get("audit_required_state") or "").strip().lower(),
        audit_pr_number=pr_number,
        audit_bound_status_id=bound_id,
    )


def is_audit_task(task: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(task, dict):
        return False
    return pins_from_task(task).completion_class == "audit"


def compare_once_bind_status(task: Dict[str, Any], *, status_id: int) -> Dict[str, Any]:
    pins = pins_from_task(task)
    if pins.completion_class != "audit":
        raise AuditContractError("compare-once bind requires completion_class=audit")
    if pins.audit_pr_number is None:
        raise AuditContractError("compare-once bind requires trusted audit_pr_number pin")
    if pins.audit_bound_status_id is not None:
        if int(pins.audit_bound_status_id) == int(status_id):
            return {"already_bound": True, "audit_bound_status_id": int(status_id)}
        raise AuditContractError(
            f"audit_bound_status_id already set to {pins.audit_bound_status_id}; compare-once refuses overwrite"
        )
    head = _require_sha("audit_head", pins.audit_head)
    base = _require_sha("audit_base", pins.audit_base)
    pull = load_pull_provenance(pins.audit_repo, int(pins.audit_pr_number))
    if pull["head_sha"] != head or pull["base_sha"] != base:
        raise AuditContractError(
            f"server-side PR provenance mismatch for {pins.audit_repo}#{pins.audit_pr_number}: "
            f"expected head={head} base={base}, observed head={pull['head_sha']} base={pull['base_sha']}"
        )
    statuses = load_commit_statuses(pins.audit_repo, head)
    row = find_status_by_id(statuses, int(status_id))
    if row is None:
        raise AuditContractError(f"status id {status_id} not found on {pins.audit_repo}@{head}")
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
    return {
        "already_bound": False,
        "audit_bound_status_id": int(status_id),
        "context": context,
        "state": state,
        "repo": pins.audit_repo,
        "head": head,
        "base": base,
        "pr_number": int(pins.audit_pr_number),
    }


def verify_audit_completion(
    task: Dict[str, Any],
    evidence: Dict[str, Any],
    *,
    producer: str = "",
) -> Dict[str, Any]:
    from .evidence_verification import VERIFIED, _unverified

    assert_no_audit_override_in_evidence(evidence)
    pins = pins_from_task(task)
    if pins.completion_class != "audit":
        raise AuditContractError("verify_audit_completion called for non-audit task")
    if pins.audit_bound_status_id is None:
        return _unverified(
            "audit completion requires a prior compare-once bind of audit_bound_status_id",
            repo=pins.audit_repo,
            commit_sha=pins.audit_head,
            required_checks=[pins.audit_required_context],
            producer=producer,
            reject_completion=True,
        )
    if pins.audit_pr_number is None:
        return _unverified(
            "audit completion requires trusted audit_pr_number pin on OrchTask",
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
            expected_pr_number=int(pins.audit_pr_number),
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
        "audit_pr_number": int(pins.audit_pr_number),
        "audit_receipt": sealed["receipt_root"],
        "reason": (
            "trusted OrchTask completion_class=audit with compare-once bound status id "
            "and sealed structured refs.json receipt"
        ),
        "checks": [{
            "name": pins.audit_required_context,
            "kind": "github-commit-status",
            "ok": True,
            "detail": (
                f"id={pins.audit_bound_status_id} state={pins.audit_required_state} "
                f"on {pins.audit_repo}@{pins.audit_head} pr=#{pins.audit_pr_number}"
            ),
        }],
    }
