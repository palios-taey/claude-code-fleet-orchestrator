"""Append-only causal event ledger for dispatch/runtime provenance.

This mirrors the accountability ledger's local hash-chain discipline: one JSONL
row per event, flock around scan+append, fsync before unlock, and no rewrite API.
The ledger is tamper-evident local evidence, not an external witness.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Optional

from fleet_orchestrator.paths import data_dir

SCHEMA_VERSION = 0
UNKNOWN = "Unknown"
CAUSAL_EVENT_TYPES = frozenset(
    {
        "dispatch_claimed",
        "wake_packet_assembled",
        "wake_delivered",
        "dispatch_delivery_failed",
        "worker_outcome_recorded",
        "commit_observed",
        "completion_evidence_verified",
        "ledger_checkpoint",
        "external_witness_anchor",
        "world_manifest_published",
    }
)

_HEADER = (
    "# APPEND-ONLY CAUSAL EVENT LEDGER - one canonical event per row. "
    "Rows are hash-chained and fsync'd; do not delete or rewrite lines."
)
_GENESIS = "0" * 64
_FULL_SHA_RE = re.compile(r"\b[0-9a-fA-F]{40}\b")


def _ledger_path(path: Optional[str] = None) -> str:
    if path:
        return path
    configured = os.environ.get("ORCH_CAUSAL_LEDGER_PATH")
    if configured and configured.strip():
        return str(Path(configured).expanduser())
    return str(data_dir() / "provenance" / "causal-events.jsonl")


def _canonical(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _typed_hash(kind: str, payload: Any) -> str:
    body = _canonical({"kind": kind, "schema_version": SCHEMA_VERSION, "payload": payload})
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def payload_oid(payload: Any) -> str:
    return f"sha256:{_typed_hash('causal_payload', payload)}"


def _row_hash(prev_row_hash: str, event: Mapping[str, Any], ts_str: str) -> str:
    body = _canonical(
        {
            "prev_row_hash": prev_row_hash,
            "event": event,
            "ts": ts_str,
        }
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _scan_last_hash(f=None, path: Optional[str] = None) -> str:
    if f is None:
        target_path = _ledger_path(path)
        if not os.path.exists(target_path):
            return _GENESIS
        with open(target_path, encoding="utf-8") as existing:
            return _scan_last_hash(existing)
    if path is not None:
        raise ValueError("pass either an open ledger handle or path, not both")
    f.seek(0)
    last = _GENESIS
    for i, raw in enumerate(f, 1):
        line = raw.rstrip("\n")
        if i == 1 and line.startswith("#"):
            continue
        if not line.strip():
            continue
        row = json.loads(line)
        last = row["row_hash"]
    return last


def _require_mapping(name: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return dict(value)


def _string_list(name: str, value: Optional[Sequence[Any]]) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError(f"{name} must be a list of strings")
    normalized: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{name} entries must be non-empty strings")
        normalized.append(item.strip())
    return normalized


def _ts_str(ts: Optional[float]) -> str:
    return repr(float(ts) if ts is not None else time.time())


def append_event(
    event_type: str,
    *,
    subject: Mapping[str, Any],
    parents: Optional[Sequence[Any]] = None,
    actor_attestation_id: Optional[str] = None,
    authority_roots: Optional[Sequence[Any]] = None,
    packet_id: Optional[str] = None,
    packet_provenance_hash: Optional[str] = None,
    payload: Optional[Mapping[str, Any]] = None,
    payload_hash: Optional[str] = None,
    ts: Optional[float] = None,
    path: Optional[str] = None,
) -> dict[str, Any]:
    """Append one validated causal event and return the written JSONL row."""
    if event_type not in CAUSAL_EVENT_TYPES:
        raise ValueError(f"unknown causal event type: {event_type!r}")
    event_ts = _ts_str(ts)
    event_payload = _require_mapping("payload", payload or {})
    event_body: dict[str, Any] = {
        "event_type": event_type,
        "schema_version": SCHEMA_VERSION,
        "ts": event_ts,
        "parents": _string_list("parents", parents),
        "subject": _require_mapping("subject", subject),
        "actor_attestation_id": str(actor_attestation_id or UNKNOWN),
        "authority_roots": _string_list("authority_roots", authority_roots),
        "packet_id": str(packet_id or UNKNOWN),
        "packet_provenance_hash": str(packet_provenance_hash or UNKNOWN),
        "payload_oid": str(payload_hash or payload_oid(event_payload)),
        "payload": event_payload,
    }
    event_body["event_id"] = f"event:{_typed_hash('causal_event', event_body)}"

    target_path = _ledger_path(path)
    directory = os.path.dirname(target_path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    with open(target_path, "a+", encoding="utf-8") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            if os.fstat(f.fileno()).st_size == 0:
                f.write(_HEADER + "\n")
                f.flush()
            prev = _scan_last_hash(f)
            row: dict[str, Any] = {
                "ts": event_ts,
                "prev_row_hash": prev,
                "event": event_body,
            }
            row["row_hash"] = _row_hash(prev, event_body, event_ts)
            f.write(_canonical(row) + "\n")
            f.flush()
            os.fsync(f.fileno())
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
    return row


def verify_chain(path: Optional[str] = None) -> dict[str, Any]:
    target_path = _ledger_path(path)
    if not os.path.exists(target_path):
        return {"ok": True, "rows": 0}
    prev = _GENESIS
    rows = 0
    with open(target_path, encoding="utf-8") as f:
        for line_no, raw in enumerate(f, 1):
            line = raw.rstrip("\n")
            if line_no == 1 and line.startswith("#"):
                continue
            if not line.strip():
                return {"ok": False, "rows": rows, "broken_at": line_no, "reason": "unexpected blank line"}
            if line.startswith("#"):
                return {
                    "ok": False,
                    "rows": rows,
                    "broken_at": line_no,
                    "reason": "unexpected comment line",
                }
            try:
                row = json.loads(line)
            except Exception:
                return {"ok": False, "rows": rows, "broken_at": line_no, "reason": "unparseable line"}
            if not isinstance(row, dict) or "row_hash" not in row or "prev_row_hash" not in row:
                return {"ok": False, "rows": rows, "broken_at": line_no, "reason": "malformed row shape"}
            if row.get("prev_row_hash") != prev:
                return {"ok": False, "rows": rows, "broken_at": line_no, "reason": "prev_row_hash mismatch"}
            expected = _row_hash(prev, row.get("event"), row.get("ts"))
            if row.get("row_hash") != expected:
                return {"ok": False, "rows": rows, "broken_at": line_no, "reason": "row_hash mismatch"}
            prev = row["row_hash"]
            rows += 1
    return {"ok": True, "rows": rows}


def unknown(reason: str) -> dict[str, str]:
    return {"register": UNKNOWN, "reason": reason}


def extract_reported_commit_sha(details: Optional[str]) -> Optional[str]:
    match = _FULL_SHA_RE.search(str(details or ""))
    return match.group(0).lower() if match else None


def build_actor_attestation(
    *,
    seat_id: str,
    supervisor: Optional[str],
    task_id: str,
    dispatch_event_id: str,
    packet_id: str,
    packet_provenance_hash: str,
    prompt_sha256: str,
    cli: str,
    dispatcher: str,
    binding_nonce: Optional[float],
    repo: Optional[str] = None,
    branch: Optional[str] = None,
    worktree: Optional[str] = None,
    ts: Optional[float] = None,
) -> dict[str, Any]:
    issued_at = _ts_str(ts)
    body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "issued_at": issued_at,
        "issued_by": "orchestrator-runtime",
        "actor_id": seat_id,
        "seat_id": seat_id,
        "supervisor": supervisor or UNKNOWN,
        "dispatcher": dispatcher,
        "task_id": task_id,
        "dispatch_event_id": dispatch_event_id,
        "packet_id": packet_id or UNKNOWN,
        "packet_provenance_hash": packet_provenance_hash or UNKNOWN,
        "prompt_sha256": prompt_sha256,
        "model_endpoint": {
            "kind": "cli",
            "cli": cli or UNKNOWN,
            "endpoint_ref": UNKNOWN,
            "endpoint_ref_register": unknown("dispatch runtime exposes the CLI, not the backing model endpoint/root"),
        },
        "runtime": {
            "session": seat_id,
            "runtime_generation_id": UNKNOWN,
            "runtime_generation_id_register": unknown("runtime generation ID is not exposed to dispatch"),
            "tmux_session": seat_id,
        },
        "work": {
            "repo": repo or UNKNOWN,
            "branch": branch or UNKNOWN,
            "worktree": worktree or UNKNOWN,
            "resulting_commit": None,
        },
        "outcome": {
            "status": "issued",
            "record_outcome_event_id": None,
            "completion_receipt_ref": None,
        },
        "binding_nonce": binding_nonce,
        "identity_source": "orchestrator runtime state and dispatched session ID; git author is not actor identity",
    }
    attestation_id = f"attestation:{_typed_hash('actor_attestation', body)}"
    return {"attestation_id": attestation_id, **body}
