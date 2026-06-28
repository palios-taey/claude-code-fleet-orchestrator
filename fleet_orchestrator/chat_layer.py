from __future__ import annotations

import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Request

from fleet_orchestrator.config import OrchConfig, get_redis_async, get_redis_sync
from fleet_orchestrator.decision_receipt import maybe_emit_receipt as maybe_emit_decision_receipt

_NOTIFY_KEY_PREFIX = os.environ.get("NOTIFY_KEY_PREFIX", "taey")
CHAT_KEY_PREFIX = f"{_NOTIFY_KEY_PREFIX}:chat:"
OPENQ_KEY_PREFIX = f"{_NOTIFY_KEY_PREFIX}:openq:"
NEEDS_YOU_KEY_PREFIX = f"{_NOTIFY_KEY_PREFIX}:needs_you:"
MEMORY_BASE = Path.home() / ".claude" / "projects"
MAX_LINEAGE_LEN = 160
MAX_MESSAGE_LEN = 20000
# Review-gate fix: roles a CLIENT may set via the HTTP endpoint. Internal
# callers (escalate -> role="system") are trusted and bypass this; the allowlist
# is enforced at the chat_post boundary so a request can't inject role="system"
# (which would render into the agent transcript as a privileged instruction).
CLIENT_ROLES = {"user", "assistant"}

router = APIRouter(prefix="/api/chat", tags=["chat"])
CHAT_POST_NEXT_STEP = (
    'Use lineage matching [A-Za-z0-9._-]+ with no "..", for example session-1-codex. '
    'POST /api/chat/<lineage> with body '
    '{"text":"<message>","sender":"<session-id>","role":"user"}; '
    'role must be user or assistant.'
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _normalize_lineage(lineage: str) -> str:
    value = str(lineage or "").strip()
    if not value:
        raise ValueError(f"lineage must be non-empty. {CHAT_POST_NEXT_STEP}")
    if len(value) > MAX_LINEAGE_LEN:
        raise ValueError(f"lineage must be <= {MAX_LINEAGE_LEN} characters. {CHAT_POST_NEXT_STEP}")
    # CL-3 fix: lineage is a flat key used as a directory component in
    # promote_reply_to_memory. The old charset allowed '/', '.', '~' enabling
    # traversal ('../x'), absolute-reset ('/tmp/x') and home-ish paths. Strict
    # allowlist only; reject '..' explicitly. (promote_reply_to_memory also
    # asserts the resolved path stays under MEMORY_BASE — defense in depth.)
    if not re.fullmatch(r"[A-Za-z0-9._-]+", value):
        raise ValueError(f"lineage contains unsupported characters. {CHAT_POST_NEXT_STEP}")
    if ".." in value:
        raise ValueError(f"lineage must not contain '..'. {CHAT_POST_NEXT_STEP}")
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


def _message_record(
    lineage: str,
    sender: str,
    text: str,
    *,
    role: str = "user",
    message_type: str = "chat",
    metadata: Optional[Dict[str, Any]] = None,
) -> tuple[str, Dict[str, Any]]:
    lineage_value = _normalize_lineage(lineage)
    body = str(text or "").strip()
    if not body:
        raise ValueError(f"text must be non-empty. {CHAT_POST_NEXT_STEP}")
    if len(body) > MAX_MESSAGE_LEN:
        raise ValueError(f"text must be <= {MAX_MESSAGE_LEN} characters. {CHAT_POST_NEXT_STEP}")

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
    return lineage_value, record


def _emit_chat_receipt(record: Dict[str, Any], lineage_value: str, *,
                       config: Optional[OrchConfig] = None) -> None:
    if record["type"] == "escalation":
        return
    maybe_emit_decision_receipt(
        "chat_send",
        {
            "why_this_context": "chat message appended to durable lineage",
            "refs_used": [],
            "rule_tier_applied": "chat",
            "observable_state": {"chat_record": record},
            "lineage": lineage_value,
            "next_contract": "lineage conversation is available to future wake context",
        },
        config=config,
    )


def append_message_sync(
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
    lineage_value, record = _message_record(
        lineage,
        sender,
        text,
        role=role,
        message_type=message_type,
        metadata=metadata,
    )
    client = redis_client or get_redis_sync(config)
    client.rpush(_message_key(lineage_value), json.dumps(record, separators=(",", ":")))
    _emit_chat_receipt(record, lineage_value, config=config)
    return record


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
    lineage_value, record = _message_record(
        lineage,
        sender,
        text,
        role=role,
        message_type=message_type,
        metadata=metadata,
    )
    client = redis_client or get_redis_async(config)
    await client.rpush(_message_key(lineage_value), json.dumps(record, separators=(",", ":")))
    _emit_chat_receipt(record, lineage_value, config=config)
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
    maybe_emit_decision_receipt(
        "chat_escalate",
        {
            "why_this_context": "chat escalation opened a needs-you question",
            "refs_used": [],
            "rule_tier_applied": "chat",
            "observable_state": {"open_question": record},
            "lineage": lineage_value,
            "blocked_on": record["id"],
            "next_contract": "human or supervising session answers the open question",
        },
        config=config,
    )
    return record


async def promote_reply_to_memory(
    reply: str,
    *,
    lineage: str,
    sender: str = "operator",
    memory_root: Path = MEMORY_BASE,
) -> Dict[str, Any]:
    lineage_value = _normalize_lineage(lineage)
    body = str(reply or "").strip()
    if not body:
        raise ValueError("reply must be non-empty")
    memory_dir = memory_root / lineage_value / "memory"
    # CL-3 defense-in-depth: the resolved write path MUST stay under memory_root,
    # regardless of the lineage value (belt + suspenders over the normalize allowlist).
    base_resolved = Path(memory_root).resolve(strict=False)
    target_resolved = memory_dir.resolve(strict=False)
    if base_resolved != target_resolved and base_resolved not in target_resolved.parents:
        raise ValueError("promote target escapes memory_root")
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
        metadata = data.get("metadata") if isinstance(data.get("metadata"), dict) else {}
        role = str(data.get("role") or "user").strip().lower() or "user"
        if role not in CLIENT_ROLES:
            raise ValueError(f"role must be one of {sorted(CLIENT_ROLES)}. {CHAT_POST_NEXT_STEP}")
        sender = data.get("sender") or "operator"
        text = data.get("text") or data.get("message") or ""
        reply_to_question_id = str(
            data.get("reply_to_question_id")
            or data.get("question_id")
            or metadata.get("reply_to_question_id")
            or metadata.get("question_id")
            or ""
        ).strip()
        bound_gate_reply: Optional[Dict[str, Any]] = None
        if reply_to_question_id:
            if role != "user":
                raise ValueError("gate-bound replies must use role=user. POST /api/chat/<lineage> with reply_to_question_id and role=user.")
            from fleet_orchestrator.orch_schema import complete_human_review_gate

            bound_gate_reply = complete_human_review_gate(
                reply_to_question_id,
                str(text or ""),
                str(sender or "operator"),
                config=OrchConfig(),
            )
            if not bound_gate_reply.get("ok"):
                raise HTTPException(
                    status_code=404,
                    detail={
                        "error": "question not found or not an open human-review gate",
                        "question_id": reply_to_question_id,
                        "next_step": (
                            f"Inspect GET /api/chat/{_normalize_lineage(lineage)} for open_questions, "
                            "then reply with the exact reply_to_question_id from the NEEDS-YOU message."
                        ),
                    },
                )
            metadata = {
                **metadata,
                "reply_to_question_id": reply_to_question_id,
                "gate_task_id": bound_gate_reply.get("gate_task_id") or "",
                "human_review_reply": True,
                "verified": bool(bound_gate_reply.get("verified")),
            }
        message = await append_message(
            lineage,
            sender=sender,
            role=role,
            text=text,
            metadata=metadata,
        )
        if message["role"] == "user" and not reply_to_question_id:
            # CL-4 fix: a user reply clears the needs-you BADGE (human responded)
            # but must NOT delete the open-question records — that silently wiped
            # all pending escalations on any chat message. Resolve explicitly via
            # the dedicated path, not as a side effect of every user message.
            await clear_needs_you(lineage)
        response = {"ok": True, "message": message}
        if bound_gate_reply:
            response["bound_gate_reply"] = bound_gate_reply
        return response
    except HTTPException:
        raise
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
            sender=data.get("sender") or "operator",
        )
    except ValueError as exc:
        raise _http_error(exc)
