from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from lib.config import OrchConfig, get_redis_sync


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
TRUE_ENV_VALUES = {"1", "true", "yes", "on"}


def decision_receipts_enabled() -> bool:
    return os.environ.get("ORCH_DECISION_RECEIPTS_ENABLED", "").strip().lower() in TRUE_ENV_VALUES


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


# TODO(dynctx-wiring::w2-build): emit STOP-event receipts after the peer-liveness
# stop-engine work lands. This branch intentionally avoids lib/orch_schema.py.
