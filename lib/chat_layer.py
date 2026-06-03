from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request

from lib.config import OrchConfig, get_redis_async

CHAT_KEY_PREFIX = "taey:chat:"
OPENQ_KEY_PREFIX = "taey:openq:"
NEEDS_YOU_KEY_PREFIX = "taey:needs_you:"
MEMORY_BASE = Path.home() / ".claude" / "projects"
MAX_LINEAGE_LEN = 160
MAX_MESSAGE_LEN = 20000

router = APIRouter(prefix="/api/chat", tags=["chat"])


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_lineage(lineage: str) -> str:
    value = str(lineage or "").strip()
    if not value:
        raise ValueError("lineage must be non-empty")
    if len(value) > MAX_LINEAGE_LEN:
        raise ValueError(f"lineage must be <= {MAX_LINEAGE_LEN} characters")
    if not re.fullmatch(r"[A-Za-z0-9._:@/+~-]+", value):
        raise ValueError("lineage contains unsupported characters")
    return value


def _message_key(lineage: str) -> str:
    return f"{CHAT_KEY_PREFIX}{_normalize_lineage(lineage)}"


def _openq_key(lineage: str) -> str:
    return f"{OPENQ_KEY_PREFIX}{_normalize_lineage(lineage)}"


def _needs_you_key(lineage: str) -> str:
    return f"{NEEDS_YOU_KEY_PREFIX}{_normalize_lineage(lineage)}"


def _json_loads(raw: str) -> Dict[str, Any]:
    value = json.loads(raw)
    if isinstance(value, dict):
        return value
    return {"value": value}


async def append_message(
    lineage: str,
    sender: str,
    text: str,
    *,
    role: str = "user",
    message_type: str = "chat",
    metadata: Optional[Dict[str, Any]] = None,
    redis_client: Any = None,
    config: Optional[OrchConfig] = None,
) -> Dict[str, Any]:
    lineage_value = _normalize_lineage(lineage)
    body = str(text or "").strip()
    if not body:
        raise ValueError("text must be non-empty")
    if len(body) > MAX_MESSAGE_LEN:
        raise ValueError(f"text must be <= {MAX_MESSAGE_LEN} characters")

    record = {
        "id": uuid.uuid4().hex,
        "lineage": lineage_value,
        "sender": str(sender or "unknown").strip() or "unknown",
        "role": str(role or "user").strip() or "user",
        "type": str(message_type or "chat").strip() or "chat",
        "text": body,
        "metadata": metadata or {},
        "ts": _now_iso(),
    }
    client = redis_client or get_redis_async(config)
    await client.rpush(_message_key(lineage_value), json.dumps(record, separators=(",", ":")))
    return record


async def get_conversation(
    lineage: str,
    *,
    start: int = 0,
    stop: int = -1,
    redis_client: Any = None,
    config: Optional[OrchConfig] = None,
) -> List[Dict[str, Any]]:
    lineage_value = _normalize_lineage(lineage)
    client = redis_client or get_redis_async(config)
    return [_json_loads(item) for item in await client.lrange(_message_key(lineage_value), start, stop)]


async def get_open_questions(
    lineage: str,
    *,
    redis_client: Any = None,
    config: Optional[OrchConfig] = None,
) -> List[Dict[str, Any]]:
    lineage_value = _normalize_lineage(lineage)
    client = redis_client or get_redis_async(config)
    return [_json_loads(item) for item in await client.lrange(_openq_key(lineage_value), 0, -1)]


async def needs_you(
    lineage: str,
    *,
    redis_client: Any = None,
    config: Optional[OrchConfig] = None,
) -> Optional[Dict[str, Any]]:
    lineage_value = _normalize_lineage(lineage)
    client = redis_client or get_redis_async(config)
    raw = await client.get(_needs_you_key(lineage_value))
    return _json_loads(raw) if raw else None


async def clear_needs_you(
    lineage: str,
    *,
    redis_client: Any = None,
    config: Optional[OrchConfig] = None,
) -> None:
    lineage_value = _normalize_lineage(lineage)
    client = redis_client or get_redis_async(config)
    await client.delete(_needs_you_key(lineage_value))


async def resolve_open_questions(
    lineage: str,
    *,
    redis_client: Any = None,
    config: Optional[OrchConfig] = None,
) -> None:
    lineage_value = _normalize_lineage(lineage)
    client = redis_client or get_redis_async(config)
    await client.delete(_openq_key(lineage_value), _needs_you_key(lineage_value))


async def escalate(
    session: str,
    reason: str,
    *,
    lineage: Optional[str] = None,
    redis_client: Any = None,
    config: Optional[OrchConfig] = None,
) -> Dict[str, Any]:
    lineage_value = _normalize_lineage(lineage or session)
    reason_text = str(reason or "").strip()
    if not reason_text:
        raise ValueError("reason must be non-empty")

    client = redis_client or get_redis_async(config)
    record = {
        "id": uuid.uuid4().hex,
        "lineage": lineage_value,
        "session": str(session or lineage_value).strip() or lineage_value,
        "reason": reason_text,
        "status": "open",
        "ts": _now_iso(),
    }
    encoded = json.dumps(record, separators=(",", ":"))
    await client.rpush(_openq_key(lineage_value), encoded)
    await client.set(_needs_you_key(lineage_value), encoded)
    await append_message(
        lineage_value,
        record["session"],
        reason_text,
        role="system",
        message_type="escalation",
        metadata={"open_question_id": record["id"], "needs_you": True},
        redis_client=client,
    )
    return record


async def promote_reply_to_memory(
    reply: str,
    *,
    lineage: str,
    sender: str = "jesse",
    memory_root: Path = MEMORY_BASE,
) -> Dict[str, Any]:
    lineage_value = _normalize_lineage(lineage)
    body = str(reply or "").strip()
    if not body:
        raise ValueError("reply must be non-empty")
    memory_dir = memory_root / lineage_value / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = memory_dir / f"chat-feedback-{stamp}-{uuid.uuid4().hex[:8]}.md"
    text = "\n".join([
        "---",
        f"name: chat-feedback-{stamp}",
        "type: feedback/user",
        f"description: Promoted chat reply for lineage {lineage_value}",
        "---",
        "",
        f"sender: {sender}",
        f"lineage: {lineage_value}",
        f"promoted_at: {_now_iso()}",
        "",
        body,
        "",
    ])
    path.write_text(text, encoding="utf-8")
    return {"ok": True, "path": str(path), "lineage": lineage_value}


def _http_error(exc: ValueError) -> HTTPException:
    return HTTPException(status_code=400, detail=str(exc))


@router.get("/{lineage}")
async def chat_history(lineage: str) -> Dict[str, Any]:
    try:
        return {
            "lineage": _normalize_lineage(lineage),
            "messages": await get_conversation(lineage),
            "open_questions": await get_open_questions(lineage),
            "needs_you": await needs_you(lineage),
        }
    except ValueError as exc:
        raise _http_error(exc)


@router.post("/{lineage}")
async def chat_post(lineage: str, req: Request) -> Dict[str, Any]:
    data = await req.json()
    try:
        message = await append_message(
            lineage,
            sender=data.get("sender") or "jesse",
            role=data.get("role") or "user",
            text=data.get("text") or data.get("message") or "",
            metadata=data.get("metadata") if isinstance(data.get("metadata"), dict) else None,
        )
        if message["role"] == "user":
            await resolve_open_questions(lineage)
        return {"ok": True, "message": message}
    except ValueError as exc:
        raise _http_error(exc)


@router.post("/{lineage}/escalate")
async def chat_escalate(lineage: str, req: Request) -> Dict[str, Any]:
    data = await req.json()
    try:
        record = await escalate(
            session=data.get("session") or lineage,
            lineage=lineage,
            reason=data.get("reason") or "",
        )
        return {"ok": True, "open_question": record}
    except ValueError as exc:
        raise _http_error(exc)


@router.post("/{lineage}/promote")
async def chat_promote(lineage: str, req: Request) -> Dict[str, Any]:
    data = await req.json()
    try:
        return await promote_reply_to_memory(
            data.get("reply") or data.get("text") or "",
            lineage=lineage,
            sender=data.get("sender") or "jesse",
        )
    except ValueError as exc:
        raise _http_error(exc)
