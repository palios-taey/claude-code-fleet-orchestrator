"""External witness adapter for causal-ledger checkpoints."""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Optional

from fleet_orchestrator.causal_ledger import SCHEMA_VERSION, UNKNOWN, append_event

WITNESS_ENABLED_ENV = "ORCH_PROVENANCE_WITNESS_ENABLED"
WITNESS_PRINCIPAL_ENV = "ORCH_PROVENANCE_WITNESS_PRINCIPAL"
WITNESS_PATH_ENV = "ORCH_PROVENANCE_WITNESS_PATH"
WITNESS_ADAPTER_ENV = "ORCH_PROVENANCE_WITNESS_ADAPTER"


class WitnessConfigError(RuntimeError):
    """Raised when anchoring is invoked before explicit witness configuration."""


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _optional_env(name: str) -> Optional[str]:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return None
    return raw.strip()


def _enabled(value: Optional[bool]) -> bool:
    if value is not None:
        return bool(value)
    raw = _optional_env(WITNESS_ENABLED_ENV)
    if raw is None:
        return False
    normalized = raw.lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise WitnessConfigError(f"{WITNESS_ENABLED_ENV} must be one of 1/0/true/false/on/off")


def _require_mapping(name: str, value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{name} must be an object")
    return dict(value)


def _checkpoint_event(checkpoint_row: Mapping[str, Any]) -> dict[str, Any]:
    event = _require_mapping("checkpoint event", checkpoint_row.get("event"))
    if event.get("event_type") != "ledger_checkpoint":
        raise ValueError("external witness anchoring requires a ledger_checkpoint event")
    return event


def _checkpoint_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    payload = _require_mapping("checkpoint payload", event.get("payload"))
    for key in ("rows", "batch_rows", "batch_root", "ledger_root"):
        if not str(payload.get(key) or "").strip():
            raise ValueError(f"checkpoint payload missing {key}")
    return payload


def _witness_object(
    event: Mapping[str, Any],
    payload: Mapping[str, Any],
    principal: str,
    observed_at: str,
) -> dict[str, Any]:
    body = {
        "schema_version": SCHEMA_VERSION,
        "ledger": str(payload.get("ledger") or "orchestrator-causal"),
        "checkpoint_event_id": str(event.get("event_id") or UNKNOWN),
        "rows": int(payload.get("rows") or 0),
        "batch_rows": int(payload.get("batch_rows") or 0),
        "from_event": str(payload.get("from_event") or UNKNOWN),
        "to_event": str(payload.get("to_event") or UNKNOWN),
        "batch_root": str(payload.get("batch_root") or UNKNOWN),
        "ledger_root": str(payload.get("ledger_root") or UNKNOWN),
        "observed_at": observed_at,
        "witness_principal": principal,
        "payload_policy": "roots_and_counts_only",
    }
    return {**body, "witness_object_id": f"witness:{_sha256_json(body)}"}


def _append_witness_object(path: Path, witness_object: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            handle.write(_canonical(witness_object) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def publish_external_witness_anchor(
    checkpoint_row: Mapping[str, Any],
    *,
    enabled: Optional[bool] = None,
    principal: Optional[str] = None,
    witness_path: Optional[str] = None,
    ledger_path: Optional[str] = None,
    observed_at: Optional[str] = None,
    ts: Optional[float] = None,
) -> dict[str, Any]:
    if not _enabled(enabled):
        raise WitnessConfigError(
            f"external witness anchoring is disabled; set {WITNESS_ENABLED_ENV}=1 only after Jesse selects the witness principal"
        )
    selected_principal = str(principal or _optional_env(WITNESS_PRINCIPAL_ENV) or "").strip()
    if not selected_principal or selected_principal == UNKNOWN:
        raise WitnessConfigError(
            f"{WITNESS_PRINCIPAL_ENV} is unset; witness principal selection is an InterruptJesse C/I decision"
        )
    adapter = str(_optional_env(WITNESS_ADAPTER_ENV) or "jsonl").strip().lower()
    if adapter != "jsonl":
        raise WitnessConfigError(f"{WITNESS_ADAPTER_ENV} currently supports only jsonl")
    selected_path = str(witness_path or _optional_env(WITNESS_PATH_ENV) or "").strip()
    if not selected_path:
        raise WitnessConfigError(f"{WITNESS_PATH_ENV} is required for the jsonl witness adapter")

    event = _checkpoint_event(checkpoint_row)
    payload = _checkpoint_payload(event)
    observed = observed_at or _now()
    witness_object = _witness_object(event, payload, selected_principal, observed)
    path = Path(selected_path).expanduser().resolve(strict=False)
    _append_witness_object(path, witness_object)
    row = append_event(
        "external_witness_anchor",
        subject={
            "ledger": witness_object["ledger"],
            "checkpoint_event_id": witness_object["checkpoint_event_id"],
            "witness_principal": selected_principal,
        },
        parents=[str(event.get("event_id") or "")],
        authority_roots=[witness_object["batch_root"], witness_object["ledger_root"]],
        payload={
            "schema_version": SCHEMA_VERSION,
            "ledger": witness_object["ledger"],
            "checkpoint_event_id": witness_object["checkpoint_event_id"],
            "rows": witness_object["rows"],
            "batch_rows": witness_object["batch_rows"],
            "batch_root": witness_object["batch_root"],
            "ledger_root": witness_object["ledger_root"],
            "witness_principal": selected_principal,
            "witness_object_id": witness_object["witness_object_id"],
            "witness_path": str(path),
            "observed_at": observed,
            "payload_policy": "roots_and_counts_only",
        },
        path=ledger_path,
        ts=ts,
    )
    anchored_event = row.get("event") if isinstance(row, Mapping) else {}
    return {
        "row": row,
        "event_id": str(anchored_event.get("event_id") or ""),
        "witness_path": str(path),
        "witness_object": witness_object,
    }
