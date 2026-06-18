from __future__ import annotations

import copy
import hashlib
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fleet_orchestrator.config import OrchConfig, get_redis_sync
from fleet_orchestrator.feature_flags import decision_receipts_enabled


logger = logging.getLogger(__name__)
RECEIPT_STREAM = "orch:streams:decision_receipts"
RECEIPT_FIELDS = (
    "why_this_context",
    "refs_used",
    "rule_tier_applied",
    "observable_state_hash",
    "blocked_on",
    "next_contract",
)


def maybe_emit_receipt(
    kind: str,
    ctx: Dict[str, Any],
    *,
    config: Optional[OrchConfig] = None,
    redis_client: Any = None,
) -> Optional[Dict[str, Any]]:
    if not decision_receipts_enabled():
        return None
    try:
        return emit_receipt(kind, ctx, config=config, redis_client=redis_client)
    except Exception:
        logger.exception("decision receipt emission failed kind=%s", kind)
        return None


def emit_receipt(
    kind: str,
    ctx: Dict[str, Any],
    *,
    config: Optional[OrchConfig] = None,
    redis_client: Any = None,
) -> Optional[Dict[str, Any]]:
    receipt = build_receipt(kind, ctx)
    payload = _canonical_json(receipt)
    try:
        client = redis_client or get_redis_sync(config)
        client.xadd(
            RECEIPT_STREAM,
            {"type": "decision_receipt", "kind": receipt["kind"], "receipt": payload},
            maxlen=100_000,
            approximate=True,
        )
    except Exception:
        logger.exception("decision receipt sink failed kind=%s", kind)
        return None
    return receipt


def build_receipt(kind: str, ctx: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(ctx, dict):
        raise TypeError("ctx must be a dict")

    observable_state = copy.deepcopy(ctx.get("observable_state") or _default_observable_state(ctx))
    receipt = {
        "id": str(ctx.get("receipt_id") or uuid.uuid4()),
        "kind": str(kind or ctx.get("kind") or "decision").strip() or "decision",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "why_this_context": _string(ctx.get("why_this_context") or ctx.get("why")),
        "refs_used": _refs_used(ctx),
        "rule_tier_applied": _string(ctx.get("rule_tier_applied") or ctx.get("rule_tier") or ctx.get("tier")),
        "observable_state_hash": _state_hash(observable_state),
        "blocked_on": _string(ctx.get("blocked_on")),
        "next_contract": _string(ctx.get("next_contract")),
    }
    for field in RECEIPT_FIELDS:
        receipt.setdefault(field, [] if field == "refs_used" else "")
    return receipt


def _default_observable_state(ctx: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: copy.deepcopy(ctx.get(key))
        for key in (
            "session",
            "target",
            "lineage",
            "project",
            "task_id",
            "packet_id",
            "provenance_hash",
            "blocked_on",
            "next_contract",
            "context",
            "chat_record",
            "wake",
        )
        if key in ctx
    }


def _refs_used(ctx: Dict[str, Any]) -> list[Any]:
    refs = ctx.get("refs_used")
    if refs is None:
        refs = ctx.get("refs")
    if refs is None:
        refs = []
        context = ctx.get("context")
        if isinstance(context, dict):
            for tier in ("overall", "supervisor", "project", "phase", "task"):
                tier_refs = context.get(f"{tier}_refs") or []
                if isinstance(tier_refs, list):
                    refs.extend(copy.deepcopy(tier_refs))
    if isinstance(refs, list):
        return copy.deepcopy(refs)
    return [copy.deepcopy(refs)]


def _state_hash(state: Any) -> str:
    return hashlib.sha256(_canonical_json(state).encode("utf-8")).hexdigest()


def _string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return _canonical_json(value)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"), ensure_ascii=True)


def read_recent_receipts(
    limit: int = 10,
    *,
    kind: Optional[str] = None,
    config: Optional[OrchConfig] = None,
    redis_client: Any = None,
    newest_first: bool = True,
) -> list[Dict[str, Any]]:
    count = max(0, int(limit))
    if count == 0:
        return []
    client = redis_client or get_redis_sync(config)
    scan_count = count * 10 if kind else count
    if newest_first:
        entries = client.xrevrange(RECEIPT_STREAM, max="+", min="-", count=scan_count)
    else:
        entries = client.xrange(RECEIPT_STREAM, min="-", max="+", count=scan_count)
    receipts = [parse_receipt_stream_entry(entry) for entry in entries]
    if kind:
        wanted = kind.strip()
        receipts = [
            receipt
            for receipt in receipts
            if receipt.get("kind") == wanted or receipt.get("_stream_kind") == wanted
        ]
    return receipts[:count]


def parse_receipt_stream_entry(entry: Any) -> Dict[str, Any]:
    stream_id, raw_fields = entry
    fields = {_decode_redis_value(key): _decode_redis_value(value) for key, value in dict(raw_fields).items()}
    raw_receipt = fields.get("receipt") or "{}"
    try:
        receipt = json.loads(raw_receipt)
    except json.JSONDecodeError as exc:
        receipt = {"receipt_parse_error": str(exc), "raw_receipt": raw_receipt}
    if not isinstance(receipt, dict):
        receipt = {"raw_receipt": receipt}
    receipt["_stream"] = RECEIPT_STREAM
    receipt["_stream_id"] = _decode_redis_value(stream_id)
    receipt["_event_type"] = fields.get("type", "")
    receipt["_stream_kind"] = fields.get("kind", "")
    return receipt


def _decode_redis_value(value: Any) -> Any:
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


# TODO(dynctx-wiring::w2-build): emit STOP-event receipts after the peer-liveness
# stop-engine work lands. This branch intentionally avoids fleet_orchestrator/orch_schema.py.
