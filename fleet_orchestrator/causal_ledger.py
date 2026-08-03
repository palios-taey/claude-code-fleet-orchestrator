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
        "consult_completed",
    }
)

_HEADER = (
    "# APPEND-ONLY CAUSAL EVENT LEDGER - one canonical event per row. "
    "Rows are hash-chained and fsync'd; do not delete or rewrite lines."
)
_GENESIS = "0" * 64
_GENESIS_LEDGER_ROOT = f"sha256:{_GENESIS}"
_REPORTED_SHA_MARKER_RE = re.compile(r"(?<![A-Za-z0-9_])sha=([0-9a-fA-F]{40})(?![A-Za-z0-9_])")
_EVENT_ID_RE = re.compile(r"\Aevent:[0-9a-f]{64}\Z")
_SHA256_OID_RE = re.compile(r"\Asha256:[0-9a-f]{64}\Z")


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


def sha256_oid_is_well_formed(value: Any) -> bool:
    return isinstance(value, str) and bool(_SHA256_OID_RE.fullmatch(value.strip()))


def event_id_is_well_formed(value: Any) -> bool:
    return isinstance(value, str) and bool(_EVENT_ID_RE.fullmatch(value.strip()))


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


def _read_rows_unverified(path: Optional[str] = None) -> list[dict[str, Any]]:
    target_path = _ledger_path(path)
    if not os.path.exists(target_path):
        return []
    rows: list[dict[str, Any]] = []
    with open(target_path, encoding="utf-8") as f:
        ledger_row_number = 0
        for line_no, raw in enumerate(f, 1):
            line = raw.rstrip("\n")
            if line_no == 1 and line.startswith("#"):
                continue
            if not line.strip():
                continue
            row = json.loads(line)
            if isinstance(row, dict):
                ledger_row_number += 1
                materialized = dict(row)
                materialized["ledger_row_number"] = ledger_row_number
                rows.append(materialized)
    return rows


def read_ledger_rows(path: Optional[str] = None) -> list[dict[str, Any]]:
    chain = verify_chain(path)
    if not chain.get("ok"):
        raise ValueError(f"causal ledger chain does not verify: {chain}")
    return _read_rows_unverified(path)


def merkle_root(event_ids: Sequence[str]) -> str:
    leaves: list[str] = []
    for event_id in event_ids:
        value = str(event_id or "").strip()
        if not value:
            raise ValueError("event_ids must contain non-empty values")
        leaves.append(hashlib.sha256(f"leaf:{value}".encode("utf-8")).hexdigest())
    if not leaves:
        raise ValueError("cannot build a Merkle root for an empty batch")
    level = leaves
    while len(level) > 1:
        next_level: list[str] = []
        for index in range(0, len(level), 2):
            left = level[index]
            right = level[index + 1] if index + 1 < len(level) else left
            next_level.append(hashlib.sha256(f"node:{left}:{right}".encode("utf-8")).hexdigest())
        level = next_level
    return f"sha256:{level[0]}"


def _require_mapping(name: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return dict(value)


def event_id_for_event(event: Mapping[str, Any]) -> str:
    event_body = _require_mapping("event", event)
    material = dict(event_body)
    material.pop("event_id", None)
    return f"event:{_typed_hash('causal_event', material)}"


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


def _find_existing_event_locked(
    f,
    *,
    event_type: str,
    idempotency_key: Mapping[str, Any],
) -> Optional[dict[str, Any]]:
    f.seek(0)
    for line_no, raw in enumerate(f, 1):
        line = raw.rstrip("\n")
        if line_no == 1 and line.startswith("#"):
            continue
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, Mapping):
            continue
        event = row.get("event")
        if not isinstance(event, Mapping) or event.get("event_type") != event_type:
            continue
        payload = event.get("payload")
        if not isinstance(payload, Mapping):
            continue
        if all(payload.get(key) == value for key, value in idempotency_key.items()):
            return dict(row)
    return None


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
    idempotency_key: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Append one validated causal event and return the written JSONL row."""
    if event_type not in CAUSAL_EVENT_TYPES:
        raise ValueError(f"unknown causal event type: {event_type!r}")
    event_ts = _ts_str(ts)
    event_payload = _require_mapping("payload", payload or {})
    canonical_payload_oid = payload_oid(event_payload)
    if payload_hash is not None and str(payload_hash) != canonical_payload_oid:
        raise ValueError("payload_hash does not match canonical payload_oid")
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
        "payload_oid": canonical_payload_oid,
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
            if idempotency_key:
                existing = _find_existing_event_locked(
                    f,
                    event_type=event_type,
                    idempotency_key=_require_mapping("idempotency_key", idempotency_key),
                )
                if existing is not None:
                    return existing
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


def build_consult_actor_attestation(
    *,
    source_family_id: str,
    request_oid: str,
    rendered_prompt_oid: str,
    response_oid: str,
    platform: str,
    seat_role: str,
    requester: str,
    session_url: str,
    parents: Optional[Sequence[Any]] = None,
    ts: Optional[float] = None,
) -> dict[str, Any]:
    issued_at = _ts_str(ts)
    body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "issued_at": issued_at,
        "issued_by": "orchestrator-runtime",
        "actor_id": "taeys-hands",
        "source_seat": "taeys-hands",
        "source_family_id": source_family_id,
        "request_oid": request_oid,
        "rendered_prompt_oid": rendered_prompt_oid,
        "response_oid": response_oid,
        "platform": platform,
        "seat_role": seat_role,
        "requester": requester,
        "session_url": session_url,
        "parents": _string_list("parents", parents),
        "runtime": {
            "kind": "orchestrator",
            "identity_issuer": "orchestrator-runtime",
            "source_seat": "taeys-hands",
        },
        "identity_source": "orchestrator receive-side attests taeys-hands consult completion; caller identity is not reused",
    }
    attestation_id = f"attestation:{_typed_hash('actor_attestation', body)}"
    return {"attestation_id": attestation_id, **body}


def append_consult_completed_event(
    *,
    source_family_id: str,
    request_oid: str,
    rendered_prompt_oid: str,
    response_oid: str,
    platform: str,
    seat_role: str,
    requester: str,
    session_url: str,
    parents: Optional[Sequence[Any]] = None,
    path: Optional[str] = None,
    ts: Optional[float] = None,
) -> dict[str, Any]:
    attestation = build_consult_actor_attestation(
        source_family_id=source_family_id,
        request_oid=request_oid,
        rendered_prompt_oid=rendered_prompt_oid,
        response_oid=response_oid,
        platform=platform,
        seat_role=seat_role,
        requester=requester,
        session_url=session_url,
        parents=parents,
        ts=ts,
    )
    row = append_event(
        "consult_completed",
        subject={
            "source_seat": "taeys-hands",
            "source_family_id": source_family_id,
            "request_oid": request_oid,
            "platform": platform,
            "seat_role": seat_role,
        },
        parents=parents,
        actor_attestation_id=str(attestation["attestation_id"]),
        authority_roots=[request_oid, rendered_prompt_oid, response_oid],
        payload={
            "source_seat": "taeys-hands",
            "source_family_id": source_family_id,
            "request_oid": request_oid,
            "rendered_prompt_oid": rendered_prompt_oid,
            "response_oid": response_oid,
            "platform": platform,
            "seat_role": seat_role,
            "requester": requester,
            "session_url": session_url,
            "attestation": attestation,
        },
        path=path,
        ts=ts,
        idempotency_key={"request_oid": request_oid},
    )
    return {
        "row": row,
        "event_id": str(row["event"]["event_id"]),
        "attestation_id": str(row["event"]["actor_attestation_id"]),
        "row_hash": str(row["row_hash"]),
    }


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


def _checkpoint_payload(row: Mapping[str, Any]) -> dict[str, Any]:
    event = row.get("event") if isinstance(row.get("event"), Mapping) else {}
    payload = event.get("payload") if isinstance(event, Mapping) and isinstance(event.get("payload"), Mapping) else {}
    return dict(payload)


def _ledger_root_payload(prev_ledger_root: str, batch_root: str, n: int) -> dict[str, Any]:
    return {
        "prev_ledger_root": prev_ledger_root,
        "batch_root": batch_root,
        "n": int(n),
    }


def _ledger_root(prev_ledger_root: str, batch_root: str, n: int) -> str:
    body = _canonical(_ledger_root_payload(prev_ledger_root, batch_root, n))
    return f"sha256:{hashlib.sha256(body.encode('utf-8')).hexdigest()}"


def verify_checkpoint_integrity(path: Optional[str] = None) -> dict[str, Any]:
    chain = verify_chain(path)
    if not chain.get("ok"):
        return {
            "ok": False,
            "errors": [f"ledger_chain_invalid:{chain}"],
            "checkpoints": {},
            "checkpoint_order": [],
        }
    rows = read_ledger_rows(path)
    errors: list[str] = []
    checkpoints: dict[str, dict[str, Any]] = {}
    checkpoint_order: list[str] = []
    previous_checkpoint_row = 0
    previous_ledger_root = _GENESIS_LEDGER_ROOT

    for row in rows:
        event = row.get("event") if isinstance(row.get("event"), Mapping) else {}
        if not isinstance(event, Mapping) or event.get("event_type") != "ledger_checkpoint":
            continue
        row_number = int(row.get("ledger_row_number") or 0)
        checkpoint_errors: list[str] = []
        checkpoint_event_id = str(event.get("event_id") or "")
        try:
            computed_checkpoint_event_id = event_id_for_event(event)
        except ValueError:
            computed_checkpoint_event_id = ""
            checkpoint_errors.append(f"checkpoint_event_malformed:row:{row_number}")
        if computed_checkpoint_event_id and checkpoint_event_id != computed_checkpoint_event_id:
            checkpoint_errors.append(f"checkpoint_event_id_mismatch:row:{row_number}")

        payload = _checkpoint_payload(row)
        if event.get("payload_oid") != payload_oid(payload):
            checkpoint_errors.append(f"checkpoint_payload_oid_mismatch:{checkpoint_event_id or row_number}")
        missing = [
            key for key in (
                "rows",
                "batch_rows",
                "from_row",
                "to_row",
                "from_event",
                "to_event",
                "batch_root",
                "ledger_root",
                "prev_ledger_root",
            )
            if not str(payload.get(key) or "").strip()
        ]
        for key in missing:
            checkpoint_errors.append(f"checkpoint_payload_missing:{checkpoint_event_id or row_number}:{key}")

        try:
            from_row = int(payload.get("from_row"))
            to_row = int(payload.get("to_row"))
            stored_rows = int(payload.get("rows"))
            stored_batch_rows = int(payload.get("batch_rows"))
        except (TypeError, ValueError):
            from_row = previous_checkpoint_row + 1
            to_row = row_number - 1
            stored_rows = -1
            stored_batch_rows = -1
            checkpoint_errors.append(f"checkpoint_range_malformed:{checkpoint_event_id or row_number}")

        expected_from_row = previous_checkpoint_row + 1
        expected_to_row = row_number - 1
        if from_row != expected_from_row:
            checkpoint_errors.append(f"checkpoint_from_row_mismatch:{checkpoint_event_id or row_number}")
        if to_row != expected_to_row:
            checkpoint_errors.append(f"checkpoint_to_row_mismatch:{checkpoint_event_id or row_number}")
        if stored_rows != to_row:
            checkpoint_errors.append(f"checkpoint_rows_mismatch:{checkpoint_event_id or row_number}")
        if stored_batch_rows != max(0, to_row - from_row + 1):
            checkpoint_errors.append(f"checkpoint_batch_rows_mismatch:{checkpoint_event_id or row_number}")

        range_valid = 1 <= from_row <= to_row < row_number <= len(rows)
        actual_event_ids: list[str] = []
        if range_valid:
            for batch_row in rows[from_row - 1:to_row]:
                batch_event = batch_row.get("event") if isinstance(batch_row.get("event"), Mapping) else {}
                batch_row_number = int(batch_row.get("ledger_row_number") or 0)
                try:
                    computed_event_id = event_id_for_event(batch_event)
                except ValueError:
                    checkpoint_errors.append(f"checkpoint_batch_event_malformed:row:{batch_row_number}")
                    continue
                stored_event_id = str(batch_event.get("event_id") or "")
                if stored_event_id != computed_event_id:
                    checkpoint_errors.append(f"checkpoint_batch_event_id_mismatch:row:{batch_row_number}")
                batch_payload = batch_event.get("payload") if isinstance(batch_event.get("payload"), Mapping) else {}
                if batch_event.get("payload_oid") != payload_oid(batch_payload):
                    checkpoint_errors.append(f"checkpoint_batch_payload_oid_mismatch:row:{batch_row_number}")
                actual_event_ids.append(computed_event_id)
        else:
            checkpoint_errors.append(f"checkpoint_range_out_of_bounds:{checkpoint_event_id or row_number}")

        recomputed_batch_root = merkle_root(actual_event_ids) if actual_event_ids else UNKNOWN
        recomputed_ledger_root = (
            _ledger_root(previous_ledger_root, recomputed_batch_root, len(actual_event_ids))
            if recomputed_batch_root != UNKNOWN
            else UNKNOWN
        )
        if actual_event_ids and payload.get("from_event") != actual_event_ids[0]:
            checkpoint_errors.append(f"checkpoint_from_event_mismatch:{checkpoint_event_id or row_number}")
        if actual_event_ids and payload.get("to_event") != actual_event_ids[-1]:
            checkpoint_errors.append(f"checkpoint_to_event_mismatch:{checkpoint_event_id or row_number}")
        if payload.get("prev_ledger_root") != previous_ledger_root:
            checkpoint_errors.append(f"checkpoint_prev_ledger_root_mismatch:{checkpoint_event_id or row_number}")
        if payload.get("batch_root") != recomputed_batch_root:
            checkpoint_errors.append(f"checkpoint_batch_root_mismatch:{checkpoint_event_id or row_number}")
        if payload.get("ledger_root") != recomputed_ledger_root:
            checkpoint_errors.append(f"checkpoint_ledger_root_mismatch:{checkpoint_event_id or row_number}")

        report = {
            "checkpoint_event_id": checkpoint_event_id,
            "row_number": row_number,
            "from_row": from_row,
            "to_row": to_row,
            "stored_batch_root": str(payload.get("batch_root") or UNKNOWN),
            "recomputed_batch_root": recomputed_batch_root,
            "stored_ledger_root": str(payload.get("ledger_root") or UNKNOWN),
            "recomputed_ledger_root": recomputed_ledger_root,
            "stored_prev_ledger_root": str(payload.get("prev_ledger_root") or UNKNOWN),
            "recomputed_prev_ledger_root": previous_ledger_root,
            "event_ids": actual_event_ids,
            "errors": checkpoint_errors,
            "ok": not checkpoint_errors,
        }
        if checkpoint_event_id:
            checkpoints[checkpoint_event_id] = report
            checkpoint_order.append(checkpoint_event_id)
        errors.extend(checkpoint_errors)
        previous_checkpoint_row = row_number
        previous_ledger_root = recomputed_ledger_root
    return {
        "ok": not errors,
        "errors": errors,
        "checkpoints": checkpoints,
        "checkpoint_order": checkpoint_order,
    }


def checkpoint_ledger(
    *,
    path: Optional[str] = None,
    subject: Optional[Mapping[str, Any]] = None,
    ts: Optional[float] = None,
) -> dict[str, Any]:
    rows = read_ledger_rows(path)
    if not rows:
        raise ValueError("cannot checkpoint an empty causal ledger")

    checkpoint_rows = [
        row for row in rows
        if isinstance(row.get("event"), Mapping) and row["event"].get("event_type") == "ledger_checkpoint"
    ]
    previous_checkpoint = checkpoint_rows[-1] if checkpoint_rows else None
    previous_payload = _checkpoint_payload(previous_checkpoint) if previous_checkpoint else {}
    previous_ledger_root = str(previous_payload.get("ledger_root") or _GENESIS_LEDGER_ROOT)
    previous_checkpoint_row = int(previous_checkpoint.get("ledger_row_number", 0)) if previous_checkpoint else 0
    batch_rows = [row for row in rows if int(row.get("ledger_row_number", 0)) > previous_checkpoint_row]
    if not batch_rows:
        raise ValueError("no new causal events to checkpoint")

    event_ids = [str(row["event"]["event_id"]) for row in batch_rows]
    from_row = int(batch_rows[0]["ledger_row_number"])
    to_row = int(batch_rows[-1]["ledger_row_number"])
    from_event = event_ids[0]
    to_event = event_ids[-1]
    batch_root = merkle_root(event_ids)
    ledger_root = _ledger_root(previous_ledger_root, batch_root, len(event_ids))
    created_at = _ts_str(ts)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "ledger": "orchestrator-causal",
        "rows": to_row,
        "batch_rows": len(event_ids),
        "from_row": from_row,
        "to_row": to_row,
        "from_event": from_event,
        "to_event": to_event,
        "batch_root": batch_root,
        "ledger_root": ledger_root,
        "prev_ledger_root": previous_ledger_root,
        "created_at": created_at,
    }
    row = append_event(
        "ledger_checkpoint",
        subject=dict(subject or {"ledger": "orchestrator-causal", "rows": to_row}),
        parents=[to_event],
        authority_roots=[batch_root, ledger_root],
        payload=payload,
        ts=ts,
        path=path,
    )
    event = row.get("event") if isinstance(row, Mapping) else {}
    return {
        "row": row,
        "event_id": str(event.get("event_id") or ""),
        "ledger_root": ledger_root,
        "batch_root": batch_root,
        "rows": to_row,
        "batch_rows": len(event_ids),
        "payload": payload,
    }


def latest_checkpoint_root(path: Optional[str] = None) -> dict[str, Any]:
    target_path = _ledger_path(path)
    if not os.path.exists(target_path):
        return {
            "register": UNKNOWN,
            "kind": "causal_ledger",
            "reason": "no ledger checkpoint is published yet",
        }
    chain = verify_chain(path)
    if not chain.get("ok"):
        return {
            "register": UNKNOWN,
            "kind": "causal_ledger",
            "reason": f"causal ledger chain does not verify: {chain}",
        }
    checkpoint_rows = [
        row for row in _read_rows_unverified(path)
        if isinstance(row.get("event"), Mapping) and row["event"].get("event_type") == "ledger_checkpoint"
    ]
    if not checkpoint_rows:
        return {
            "register": UNKNOWN,
            "kind": "causal_ledger",
            "reason": "no ledger checkpoint is published yet",
        }
    integrity = verify_checkpoint_integrity(path)
    if not integrity.get("ok"):
        return {
            "register": UNKNOWN,
            "kind": "causal_ledger",
            "reason": "ledger checkpoint integrity does not verify",
            "errors": list(integrity.get("errors") or []),
        }
    checkpoint = checkpoint_rows[-1]
    event = checkpoint.get("event") if isinstance(checkpoint.get("event"), Mapping) else {}
    payload = _checkpoint_payload(checkpoint)
    return {
        "register": "Observed",
        "kind": "causal_ledger",
        "path": target_path,
        "checkpoint_event_id": str(event.get("event_id") or UNKNOWN),
        "rows": int(payload.get("rows") or 0),
        "batch_rows": int(payload.get("batch_rows") or 0),
        "from_event": str(payload.get("from_event") or UNKNOWN),
        "to_event": str(payload.get("to_event") or UNKNOWN),
        "batch_root": str(payload.get("batch_root") or UNKNOWN),
        "ledger_root": str(payload.get("ledger_root") or UNKNOWN),
    }


def unknown(reason: str) -> dict[str, str]:
    return {"register": UNKNOWN, "reason": reason}


def extract_reported_commit_sha(details: Optional[str]) -> Optional[str]:
    matches = list(_REPORTED_SHA_MARKER_RE.finditer(str(details or "")))
    if len(matches) != 1:
        return None
    return matches[0].group(1).lower()


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
