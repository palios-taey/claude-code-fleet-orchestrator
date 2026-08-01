"""Fail-closed verifier for provenance-kernel closure claims."""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, Optional

from fleet_orchestrator.causal_ledger import UNKNOWN, read_ledger_rows, verify_chain, verify_checkpoint_integrity
from fleet_orchestrator.world_manifest import reverify_world_manifest, world_manifest_path


class ProvenanceKernelVerificationError(RuntimeError):
    def __init__(self, errors: Sequence[str]):
        self.errors = list(errors)
        super().__init__("provenance kernel verification failed: " + "; ".join(self.errors))


def _unknown(value: Any) -> bool:
    return not str(value or "").strip() or str(value) == UNKNOWN


def _event(row: Mapping[str, Any]) -> dict[str, Any]:
    raw = row.get("event")
    return dict(raw) if isinstance(raw, Mapping) else {}


def _payload(event: Mapping[str, Any]) -> dict[str, Any]:
    raw = event.get("payload")
    return dict(raw) if isinstance(raw, Mapping) else {}


def _normalize_event_ids(event_ids: Sequence[str]) -> list[str]:
    normalized: list[str] = []
    seen: set[str] = set()
    for event_id in event_ids:
        value = str(event_id or "").strip()
        if not value:
            continue
        if value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    return normalized


def _witnessed_checkpoints(
    rows: Sequence[Mapping[str, Any]],
    checkpoint_reports: Mapping[str, Mapping[str, Any]],
) -> dict[str, dict[str, Any]]:
    checkpoints: dict[str, dict[str, Any]] = {
        checkpoint_id: {**dict(report), "anchors": [], "witness_errors": []}
        for checkpoint_id, report in checkpoint_reports.items()
    }
    anchors: list[dict[str, Any]] = []
    for row in rows:
        event = _event(row)
        if event.get("event_type") == "external_witness_anchor":
            anchors.append(event)
    for anchor in anchors:
        payload = _payload(anchor)
        checkpoint_event_id = str(payload.get("checkpoint_event_id") or "")
        if not checkpoint_event_id:
            parents = anchor.get("parents") if isinstance(anchor.get("parents"), list) else []
            checkpoint_event_id = str(parents[0] if parents else "")
        checkpoint = checkpoints.get(checkpoint_event_id)
        if checkpoint is None:
            continue
        witness_errors: list[str] = []
        if payload.get("payload_policy") != "roots_and_counts_only":
            witness_errors.append(f"witness_payload_policy_mismatch:{checkpoint_event_id}")
        if payload.get("batch_root") != checkpoint.get("stored_batch_root"):
            witness_errors.append(f"witness_stored_batch_root_mismatch:{checkpoint_event_id}")
        if payload.get("ledger_root") != checkpoint.get("stored_ledger_root"):
            witness_errors.append(f"witness_stored_ledger_root_mismatch:{checkpoint_event_id}")
        if payload.get("batch_root") != checkpoint.get("recomputed_batch_root"):
            witness_errors.append(f"witness_recomputed_batch_root_mismatch:{checkpoint_event_id}")
        if payload.get("ledger_root") != checkpoint.get("recomputed_ledger_root"):
            witness_errors.append(f"witness_recomputed_ledger_root_mismatch:{checkpoint_event_id}")
        checkpoint["witness_errors"].extend(witness_errors)
        if not witness_errors:
            checkpoint["anchors"].append(anchor)
    return checkpoints


def reverify_recorded_world_manifests(
    *,
    ledger_path: Optional[str] = None,
    manifest_path: Optional[str] = None,
    max_age_days: Optional[float] = 7.0,
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    errors: list[str] = []
    if manifest_path:
        check = reverify_world_manifest(
            manifest_path=manifest_path,
            max_age_days=max_age_days,
            causal_ledger_path=ledger_path,
        )
        checks.append(check)
        if not check.get("ok"):
            errors.extend(str(item) for item in check.get("errors", []))
    else:
        try:
            rows = read_ledger_rows(ledger_path)
        except ValueError as exc:
            return {"ok": False, "errors": [str(exc)], "checks": []}
        for row in rows:
            event = _event(row)
            if event.get("event_type") != "world_manifest_published":
                continue
            payload = _payload(event)
            manifest = payload.get("manifest")
            if isinstance(manifest, Mapping):
                check = reverify_world_manifest(
                    manifest=manifest,
                    max_age_days=max_age_days,
                    causal_ledger_path=ledger_path,
                )
                checks.append(check)
                if not check.get("ok"):
                    errors.extend(str(item) for item in check.get("errors", []))
            elif payload.get("manifest_path"):
                check = reverify_world_manifest(
                    manifest_path=str(payload.get("manifest_path")),
                    max_age_days=max_age_days,
                    causal_ledger_path=ledger_path,
                )
                checks.append(check)
                if not check.get("ok"):
                    errors.extend(str(item) for item in check.get("errors", []))
    return {"ok": not errors, "errors": errors, "checks": checks}


def verify_provenance_kernel_closure(
    *,
    event_ids: Sequence[str],
    actor_attestation_id: Optional[str],
    packet_id: Optional[str],
    packet_provenance_hash: Optional[str],
    ledger_path: Optional[str] = None,
    require_witness: bool = True,
    witness_waiver_reason: Optional[str] = None,
) -> dict[str, Any]:
    errors: list[str] = []
    waiver_reason = str(witness_waiver_reason or "").strip()
    normalized_event_ids = _normalize_event_ids(event_ids)
    if not normalized_event_ids:
        errors.append("missing_event_ids")
    if not require_witness and not waiver_reason:
        errors.append("missing_witness_waiver_reason")
    if _unknown(actor_attestation_id):
        errors.append("missing_actor_attestation")
    if _unknown(packet_id):
        errors.append("missing_packet_id")
    if _unknown(packet_provenance_hash):
        errors.append("missing_packet_provenance_hash")

    chain = verify_chain(ledger_path)
    if not chain.get("ok"):
        errors.append(f"ledger_chain_invalid:{chain}")
        return {"ok": False, "errors": errors, "rows": int(chain.get("rows") or 0)}

    rows = read_ledger_rows(ledger_path)
    by_event_id = {str(_event(row).get("event_id") or ""): row for row in rows}
    named_rows: list[dict[str, Any]] = []
    for event_id in normalized_event_ids:
        row = by_event_id.get(event_id)
        if row is None:
            errors.append(f"missing_event_id:{event_id}")
            continue
        named_rows.append(row)

    if actor_attestation_id and not any(_event(row).get("actor_attestation_id") == actor_attestation_id for row in named_rows):
        errors.append("actor_attestation_not_bound_to_events")

    matching_packet_events = 0
    for row in named_rows:
        event = _event(row)
        event_packet_id = str(event.get("packet_id") or UNKNOWN)
        event_packet_hash = str(event.get("packet_provenance_hash") or UNKNOWN)
        if event_packet_id == packet_id:
            matching_packet_events += 1
            if event_packet_hash != packet_provenance_hash:
                errors.append(f"packet_hash_mismatch:{event.get('event_id')}")
        elif event_packet_id != UNKNOWN:
            errors.append(f"packet_id_mismatch:{event.get('event_id')}")
        if event_packet_hash != UNKNOWN and event_packet_hash != packet_provenance_hash:
            errors.append(f"packet_hash_mismatch:{event.get('event_id')}")
    if packet_id and packet_provenance_hash and named_rows and matching_packet_events == 0:
        errors.append("packet_binding_not_found")

    checkpoint_integrity = verify_checkpoint_integrity(ledger_path)
    checkpoint_errors_by_id: dict[str, list[str]] = {
        checkpoint_id: [str(item) for item in report.get("errors", [])]
        for checkpoint_id, report in checkpoint_integrity.get("checkpoints", {}).items()
        if isinstance(report, Mapping)
    }
    witnessed_checkpoint_ids: set[str] = set()
    if require_witness and named_rows:
        checkpoints = _witnessed_checkpoints(rows, checkpoint_integrity.get("checkpoints", {}))
        row_numbers = {str(_event(row).get("event_id") or ""): int(row.get("ledger_row_number") or 0) for row in rows}
        covered: set[str] = set()
        for checkpoint_id, checkpoint in checkpoints.items():
            checkpoint_errors = list(checkpoint_errors_by_id.get(checkpoint_id, []))
            witness_errors = [str(item) for item in checkpoint.get("witness_errors", [])]
            try:
                from_row = int(checkpoint.get("from_row"))
                to_row = int(checkpoint.get("to_row"))
            except (TypeError, ValueError):
                continue
            for event_id in normalized_event_ids:
                row_number = row_numbers.get(event_id, 0)
                if from_row <= row_number <= to_row:
                    if checkpoint_errors:
                        errors.extend(checkpoint_errors)
                    if witness_errors:
                        errors.extend(witness_errors)
                    if not checkpoint_errors and not witness_errors and checkpoint.get("anchors"):
                        covered.add(event_id)
                        witnessed_checkpoint_ids.add(checkpoint_id)
        missing_witness = [event_id for event_id in normalized_event_ids if event_id not in covered and event_id in by_event_id]
        if missing_witness:
            errors.append("missing_witness_roots:" + ",".join(missing_witness))

    deduped_errors = list(dict.fromkeys(errors))
    return {
        "ok": not deduped_errors,
        "errors": deduped_errors,
        "rows": int(chain.get("rows") or 0),
        "event_ids": normalized_event_ids,
        "witness_required": bool(require_witness),
        "witness_waiver_reason": waiver_reason or UNKNOWN,
        "checkpoint_integrity_ok": bool(checkpoint_integrity.get("ok")),
        "witnessed_checkpoints": sorted(witnessed_checkpoint_ids),
    }


def assert_provenance_kernel_closure(**kwargs: Any) -> dict[str, Any]:
    result = verify_provenance_kernel_closure(**kwargs)
    if not result.get("ok"):
        raise ProvenanceKernelVerificationError([str(item) for item in result.get("errors", [])])
    return result


def load_manifest_file(path: Optional[str] = None) -> dict[str, Any]:
    selected = world_manifest_path(path)
    return json.loads(Path(selected).read_text(encoding="utf-8"))
