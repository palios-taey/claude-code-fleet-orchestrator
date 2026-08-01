"""World Manifest v0 builder and publisher for provenance wake packets."""
from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Optional

from fleet_orchestrator.causal_ledger import UNKNOWN, append_event
from fleet_orchestrator.paths import data_dir, repo_root

SCHEMA_VERSION = 0
SYSTEM_MAP_ENV = "ORCH_WORLD_SYSTEM_MAP_PATH"
KNOWLEDGE_INDEX_ENV = "ORCH_WORLD_KNOWLEDGE_INDEX_PATH"
WORLD_MANIFEST_ENV = "ORCH_WORLD_MANIFEST_PATH"
SYSTEM_MAP_NAME = "TAEY_SYSTEM_CONNECTION_MAP.md"
KNOWLEDGE_INDEX_REL = Path("serving") / "knowledge_index" / "index.json"


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _optional_env(name: str) -> Optional[str]:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    return raw.strip()


def _env_path(raw: Optional[str]) -> Optional[Path]:
    if raw is None:
        return None
    return Path(raw).expanduser().resolve(strict=False)


def world_manifest_path(path: Optional[str] = None) -> Path:
    if path and path.strip():
        return Path(path).expanduser().resolve(strict=False)
    configured = _env_path(_optional_env("ORCH_WORLD_MANIFEST_PATH"))
    if configured is not None:
        return configured
    return data_dir() / "provenance" / "world-manifest-v0.json"


def _display_path(path: Path) -> str:
    resolved = path.resolve(strict=False)
    root = repo_root().resolve(strict=False)
    try:
        return resolved.relative_to(root).as_posix()
    except ValueError:
        return str(resolved)


def _unknown(reason: str, **extra: Any) -> dict[str, Any]:
    entry: dict[str, Any] = {"register": UNKNOWN, "reason": reason}
    entry.update(extra)
    return entry


def _read_root_file(path: Path, *, kind: str) -> dict[str, Any]:
    if not path.is_file():
        return _unknown(f"{_display_path(path)} is missing", kind=kind, path=_display_path(path))
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return _unknown(
            f"{_display_path(path)} is unreadable: {exc.__class__.__name__}: {exc}",
            kind=kind,
            path=_display_path(path),
        )
    return {
        "register": "Observed",
        "kind": kind,
        "path": _display_path(path),
        "sha256": _sha256_bytes(raw),
        "bytes": len(raw),
    }


def _unique_paths(candidates: Sequence[Path]) -> list[Path]:
    seen: set[str] = set()
    result: list[Path] = []
    for candidate in candidates:
        key = str(candidate.resolve(strict=False))
        if key in seen:
            continue
        seen.add(key)
        result.append(candidate)
    return result


def _system_map_candidates(path: Optional[str] = None) -> tuple[list[Path], bool]:
    if path and path.strip():
        return [Path(path).expanduser().resolve(strict=False)], True
    configured = _env_path(_optional_env("ORCH_WORLD_SYSTEM_MAP_PATH"))
    if configured is not None:
        return [configured], True
    root = repo_root().resolve(strict=False)
    return _unique_paths(
        [
            root / SYSTEM_MAP_NAME,
            root.parent / "the-conductor" / SYSTEM_MAP_NAME,
            Path.home() / "the-conductor" / SYSTEM_MAP_NAME,
        ]
    ), False


def _system_map_root(path: Optional[str] = None) -> dict[str, Any]:
    candidates, explicit = _system_map_candidates(path)
    selected = next((candidate for candidate in candidates if candidate.is_file()), None)
    if selected is not None:
        return _read_root_file(selected, kind="system_connection_map")
    reason = (
        f"{SYSTEM_MAP_ENV} points at a missing system connection map"
        if explicit
        else "no TAEY_SYSTEM_CONNECTION_MAP.md found in repo, sibling, or home checkouts"
    )
    return _unknown(
        reason,
        kind="system_connection_map",
        candidates=[str(candidate) for candidate in candidates],
    )


def _knowledge_index_candidates(path: Optional[str] = None) -> tuple[list[Path], bool]:
    if path and path.strip():
        return [Path(path).expanduser().resolve(strict=False)], True
    configured = _env_path(_optional_env("ORCH_WORLD_KNOWLEDGE_INDEX_PATH"))
    if configured is not None:
        return [configured], True
    sibling_root = repo_root().resolve(strict=False).parent
    home_root = Path.home()
    return _unique_paths(
        [
            sibling_root / "taey-presence-production" / KNOWLEDGE_INDEX_REL,
            sibling_root / "taey-presence" / KNOWLEDGE_INDEX_REL,
            home_root / "taey-presence-production" / KNOWLEDGE_INDEX_REL,
            home_root / "taey-presence" / KNOWLEDGE_INDEX_REL,
        ]
    ), False


def _git_value(args: Sequence[str]) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(repo_root()), *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=2,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def _normalize_github_repo(remote: Optional[str]) -> str:
    value = str(remote or "").strip()
    if value.startswith("git@github.com:"):
        value = value.removeprefix("git@github.com:")
    elif value.startswith("https://github.com/"):
        value = value.removeprefix("https://github.com/")
    if value.endswith(".git"):
        value = value[:-4]
    return value or UNKNOWN


def _orchestrator_repo_root() -> dict[str, Any]:
    commit = _git_value(["rev-parse", "HEAD"])
    remote = _git_value(["remote", "get-url", "origin"])
    branch = _git_value(["branch", "--show-current"])
    if not commit:
        return _unknown("orchestrator repo commit could not be resolved", kind="git_repo", path=str(repo_root()))
    return {
        "register": "Observed",
        "kind": "git_repo",
        "repo": _normalize_github_repo(remote),
        "remote": remote or UNKNOWN,
        "commit": commit,
        "branch": branch or UNKNOWN,
        "path": str(repo_root().resolve(strict=False)),
    }


def _iter_index_capabilities(parsed: Mapping[str, Any]) -> list[tuple[str, Mapping[str, Any]]]:
    sections = parsed.get("sections")
    if not isinstance(sections, Mapping):
        return []
    capabilities: list[tuple[str, Mapping[str, Any]]] = []
    for section_name, section in sections.items():
        if not isinstance(section, Mapping):
            continue
        for capability in section.get("capabilities") or []:
            if isinstance(capability, Mapping):
                capabilities.append((str(section_name), capability))
    return capabilities


def _text(value: Any) -> str:
    rendered = str(value or "").strip()
    return rendered if rendered else UNKNOWN


def _capability_root(section_name: str, capability: Mapping[str, Any]) -> dict[str, Any]:
    repo = capability.get("repo") if isinstance(capability.get("repo"), Mapping) else {}
    artifact_manifest = (
        capability.get("artifact_manifest")
        if isinstance(capability.get("artifact_manifest"), Mapping)
        else {}
    )
    receipts = capability.get("receipts") if isinstance(capability.get("receipts"), Mapping) else {}
    return {
        "register": "Observed",
        "section": section_name,
        "id": _text(capability.get("id")),
        "status": "production",
        "kind": _text(capability.get("kind")),
        "repo": _text(repo.get("name")),
        "pinned_sha": _text(repo.get("pinned_sha")),
        "artifact_commit_sha": _text(capability.get("artifact_commit_sha")),
        "artifact_manifest_path": _text(artifact_manifest.get("path")),
        "artifact_manifest_sha256": _text(artifact_manifest.get("sha256")),
        "liveness_receipt": _text(receipts.get("liveness")),
        "liveness_receipt_sha256": _text(receipts.get("liveness_sha256")),
    }


def _knowledge_index_roots(path: Optional[str] = None) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    candidates, explicit = _knowledge_index_candidates(path)
    selected = next((candidate for candidate in candidates if candidate.is_file()), None)
    if selected is None:
        reason = (
            f"{KNOWLEDGE_INDEX_ENV} points at a missing index"
            if explicit
            else "no taey-presence production knowledge index found in local operator checkouts"
        )
        return (
            _unknown(
                reason,
                kind="taey_presence_knowledge_index",
                candidates=[str(candidate) for candidate in candidates],
            ),
            [
                _unknown(
                    "production capabilities cannot be derived without the taey-presence knowledge index",
                    kind="production_capabilities",
                )
            ],
        )
    try:
        raw = selected.read_bytes()
    except OSError as exc:
        return (
            _unknown(
                f"knowledge index is unreadable: {exc.__class__.__name__}: {exc}",
                kind="taey_presence_knowledge_index",
                path=_display_path(selected),
            ),
            [
                _unknown(
                    "production capabilities cannot be derived from an unreadable knowledge index",
                    kind="production_capabilities",
                )
            ],
        )
    index_root: dict[str, Any] = {
        "register": "Observed",
        "kind": "taey_presence_knowledge_index",
        "path": _display_path(selected),
        "sha256": _sha256_bytes(raw),
        "bytes": len(raw),
    }
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        index_root.update(
            _unknown(
                f"knowledge index is not readable JSON: {exc.__class__.__name__}: {exc}",
                kind="taey_presence_knowledge_index",
                path=_display_path(selected),
                sha256=index_root["sha256"],
                bytes=index_root["bytes"],
            )
        )
        return (
            index_root,
            [
                _unknown(
                    "production capabilities cannot be derived from an unreadable knowledge index",
                    kind="production_capabilities",
                )
            ],
        )
    if not isinstance(parsed, Mapping):
        return (
            _unknown(
                "knowledge index JSON root is not an object",
                kind="taey_presence_knowledge_index",
                path=_display_path(selected),
                sha256=index_root["sha256"],
                bytes=index_root["bytes"],
            ),
            [
                _unknown(
                    "production capabilities cannot be derived from a non-object knowledge index",
                    kind="production_capabilities",
                )
            ],
        )
    index_root.update(
        {
            "index_id": _text(parsed.get("index_id")),
            "generated_at_commit": _text(parsed.get("generated_at_commit")),
            "live_url": _text(parsed.get("live_url")),
        }
    )
    capabilities = [
        _capability_root(section_name, capability)
        for section_name, capability in _iter_index_capabilities(parsed)
        if str(capability.get("status") or "").strip() == "production"
    ]
    capabilities.sort(key=lambda item: (item.get("section", ""), item.get("id", "")))
    if not capabilities:
        capabilities = [
            _unknown(
                "knowledge index contained no status=production capabilities",
                kind="production_capabilities",
                path=_display_path(selected),
            )
        ]
    return index_root, capabilities


def _identity_payload(roots: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "roots": roots,
    }


def build_world_manifest_v0(
    *,
    as_of: Optional[str] = None,
    system_map_path: Optional[str] = None,
    knowledge_index_path: Optional[str] = None,
) -> dict[str, Any]:
    """Build a digest-of-roots manifest without writing state."""
    index_root, production_capabilities = _knowledge_index_roots(knowledge_index_path)
    roots = {
        "system_connection_map": _system_map_root(system_map_path),
        "orchestrator_repo": _orchestrator_repo_root(),
        "taey_presence_index": index_root,
        "production_capabilities": production_capabilities,
        "causal_ledger": _unknown(
            "no ledger checkpoint is published in World Manifest v0",
            kind="causal_ledger",
        ),
    }
    world_id = f"world:{_sha256_json(_identity_payload(roots))}"
    return {
        "schema_version": SCHEMA_VERSION,
        "world_id": world_id,
        "world_id_source": "sha256 of canonical JSON over schema_version and roots",
        "as_of": as_of or _now(),
        "roots": roots,
    }


def _write_manifest(path: Path, manifest: Mapping[str, Any]) -> str:
    body = canonical_json(manifest) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        handle.write(body)
        handle.flush()
        os.fsync(handle.fileno())
    return _sha256_bytes(body.encode("utf-8"))


def publish_world_manifest_v0(
    *,
    subject: Optional[Mapping[str, Any]] = None,
    parents: Optional[Sequence[Any]] = None,
    actor_attestation_id: Optional[str] = None,
    packet_id: Optional[str] = None,
    packet_provenance_hash: Optional[str] = None,
    manifest_path: Optional[str] = None,
    ledger_path: Optional[str] = None,
    as_of: Optional[str] = None,
    system_map_path: Optional[str] = None,
    knowledge_index_path: Optional[str] = None,
) -> dict[str, Any]:
    """Write World Manifest v0 and append its publication causal event."""
    manifest = build_world_manifest_v0(
        as_of=as_of,
        system_map_path=system_map_path,
        knowledge_index_path=knowledge_index_path,
    )
    path = world_manifest_path(manifest_path)
    manifest_sha256 = _write_manifest(path, manifest)
    row = append_event(
        "world_manifest_published",
        subject=dict(subject or {"world_id": manifest["world_id"]}),
        parents=parents,
        actor_attestation_id=actor_attestation_id,
        packet_id=packet_id,
        packet_provenance_hash=packet_provenance_hash,
        payload={
            "world_id": manifest["world_id"],
            "manifest_path": str(path),
            "manifest_sha256": manifest_sha256,
            "manifest": manifest,
        },
        path=ledger_path,
    )
    event = row.get("event") if isinstance(row, Mapping) else {}
    return {
        "manifest": manifest,
        "world_id": manifest["world_id"],
        "manifest_path": str(path),
        "manifest_sha256": manifest_sha256,
        "event_id": str(event.get("event_id") or ""),
        "row": row,
    }
