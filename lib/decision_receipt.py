from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from .config import OrchConfig, get_neo4j_session, get_redis_sync


RECEIPT_KEY = "taey:receipts"
RECEIPT_FIELDS = (
    "why_this_context",
    "refs_used",
    "rule_tier",
    "observable_state_hash",
    "blocked_on",
    "next_contract",
)


def emit_receipt(kind: str, ctx: Dict[str, Any], config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    receipt = _build_receipt(kind, ctx)
    payload = json.dumps(receipt, sort_keys=True, separators=(",", ":"))

    redis_client = get_redis_sync(config)
    redis_client.xadd(RECEIPT_KEY, {"receipt": payload}, maxlen=100_000, approximate=True)

    cfg = config or OrchConfig()
    with get_neo4j_session(cfg) as session:
        session.run(
            """
            MERGE (r:OrchDecisionReceipt {id: $id})
            SET r.kind = $kind,
                r.created_at = datetime($created_at),
                r.why_this_context = $why_this_context,
                r.refs_used = $refs_used,
                r.rule_tier = $rule_tier,
                r.observable_state_hash = $observable_state_hash,
                r.blocked_on = $blocked_on,
                r.next_contract = $next_contract,
                r.payload = $payload
            """,
            id=receipt["id"],
            kind=receipt["kind"],
            created_at=receipt["created_at"],
            why_this_context=receipt["why_this_context"],
            refs_used=json.dumps(receipt["refs_used"], sort_keys=True, separators=(",", ":")),
            rule_tier=receipt["rule_tier"],
            observable_state_hash=receipt["observable_state_hash"],
            blocked_on=receipt["blocked_on"],
            next_contract=receipt["next_contract"],
            payload=payload,
        )

    return receipt


def _build_receipt(kind: str, ctx: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(ctx, dict):
        raise TypeError("ctx must be a dict")

    receipt = {
        "id": str(ctx.get("receipt_id") or uuid.uuid4()),
        "kind": str(kind or ctx.get("kind") or "decision").strip(),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "why_this_context": _string(ctx.get("why_this_context") or ctx.get("why")),
        "refs_used": _refs_used(ctx),
        "rule_tier": _string(ctx.get("rule_tier") or ctx.get("tier")),
        "observable_state_hash": _state_hash(ctx),
        "blocked_on": _string(ctx.get("blocked_on")),
        "next_contract": _string(ctx.get("next_contract")),
    }
    for field in RECEIPT_FIELDS:
        receipt.setdefault(field, "" if field != "refs_used" else [])
    return receipt


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
                    refs.extend(tier_refs)
    if isinstance(refs, list):
        return refs
    return [refs]


def _state_hash(ctx: Dict[str, Any]) -> str:
    # CA-3-class fix (Gaia Gate-2): always compute over the actual state. Returning
    # a caller-supplied observable_state_hash verbatim made the receipt's integrity
    # field self-asserted / forgeable. Callers bind a specific state by passing
    # observable_state (the data), not a pre-computed hash.
    state = ctx.get("observable_state")
    if state is None:
        state = {
            key: ctx.get(key)
            for key in ("session", "project", "task_id", "packet_id", "provenance_hash", "context")
            if key in ctx
        }
    encoded = json.dumps(state, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _string(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True, default=str, separators=(",", ":"))
