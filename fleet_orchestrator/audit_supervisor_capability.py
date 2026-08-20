"""Server-verifiable supervisor capability for audit pin/bind transitions.

CONTROL (task-05a27e83 / PR345): body.from is caller-controlled and ORCH_AUTH_TOKEN
only authenticates API access — it does not bind a fleet session identity. Privileged
audit transitions therefore require an HMAC capability attesting session_id + task_id
+ action, keyed by a secret workers with only the shared API token do not hold.

Secrets (fail-closed):
  - ORCH_SESSION_CAPABILITY_SECRETS: JSON object {session_id: secret}
  - ORCH_AUDIT_CAPABILITY_SECRET: master secret; derives per-session key as
    HMAC_SHA256(master, \"session:\" + session_id) when the map entry is absent

Capability header: X-Orch-Audit-Capability = base64url(payload_json) + \".\" + hex_hmac
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from typing import Any, Dict, Mapping, Optional

from .audit_completion import AuditContractError

CAPABILITY_HEADER = "X-Orch-Audit-Capability"
CAPABILITY_ACTIONS = frozenset({"pin-audit-contract", "bind-audit-status"})
DEFAULT_TTL_SEC = 300
MAX_TTL_SEC = 3600

# Test hook: override env-backed secret resolution (isolated acceptance only).
_SECRET_PROVIDER: Optional[Any] = None


def set_audit_capability_secret_provider(provider: Optional[Any]) -> None:
    """Inject ``provider(session_id) -> secret|None`` for isolated tests."""
    global _SECRET_PROVIDER
    _SECRET_PROVIDER = provider


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _load_session_secret_map(env: Optional[Mapping[str, str]] = None) -> Dict[str, str]:
    values = os.environ if env is None else env
    raw = str(values.get("ORCH_SESSION_CAPABILITY_SECRETS") or "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AuditContractError(
            "ORCH_SESSION_CAPABILITY_SECRETS must be a JSON object of session_id→secret"
        ) from exc
    if not isinstance(parsed, dict):
        raise AuditContractError(
            "ORCH_SESSION_CAPABILITY_SECRETS must be a JSON object of session_id→secret"
        )
    out: Dict[str, str] = {}
    for key, value in parsed.items():
        session = str(key or "").strip()
        secret = str(value or "").strip()
        if session and secret:
            out[session] = secret
    return out


def session_capability_secret(
    session_id: str,
    *,
    env: Optional[Mapping[str, str]] = None,
) -> str:
    """Resolve the HMAC key for a session. Fail closed if no secret is configured."""
    session = str(session_id or "").strip()
    if not session:
        raise AuditContractError("session_id is required for audit capability secret")
    if _SECRET_PROVIDER is not None:
        secret = _SECRET_PROVIDER(session)
        if not secret:
            raise AuditContractError(
                f"no audit capability secret configured for session {session!r}"
            )
        return str(secret)
    values = os.environ if env is None else env
    mapped = _load_session_secret_map(values)
    if session in mapped:
        return mapped[session]
    master = str(values.get("ORCH_AUDIT_CAPABILITY_SECRET") or "").strip()
    if not master:
        raise AuditContractError(
            "audit supervisor capability secret unset: configure "
            "ORCH_SESSION_CAPABILITY_SECRETS or ORCH_AUDIT_CAPABILITY_SECRET "
            "(distinct from ORCH_AUTH_TOKEN; workers must not hold supervisor secrets)"
        )
    # Derive per-session key so a leaked single-session map entry does not mint for others
    # when using the master; possession of master still mints any session — provision master
    # only on supervisor hosts.
    return hmac.new(
        master.encode("utf-8"),
        f"session:{session}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def _canonical_payload(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def mint_audit_capability(
    *,
    session_id: str,
    task_id: str,
    action: str,
    ttl_sec: int = DEFAULT_TTL_SEC,
    now: Optional[float] = None,
    env: Optional[Mapping[str, str]] = None,
) -> str:
    """Mint a capability token for a supervisor session (CLI / internal issuer)."""
    session = str(session_id or "").strip()
    task = str(task_id or "").strip()
    act = str(action or "").strip()
    if not session or not task:
        raise AuditContractError("mint_audit_capability requires session_id and task_id")
    if act not in CAPABILITY_ACTIONS:
        raise AuditContractError(
            f"action must be one of {sorted(CAPABILITY_ACTIONS)}, got {act!r}"
        )
    try:
        ttl = int(ttl_sec)
    except (TypeError, ValueError) as exc:
        raise AuditContractError("ttl_sec must be an integer") from exc
    if ttl <= 0 or ttl > MAX_TTL_SEC:
        raise AuditContractError(f"ttl_sec must be in 1..{MAX_TTL_SEC}")
    issued = float(now if now is not None else time.time())
    payload = {
        "v": 1,
        "session_id": session,
        "task_id": task,
        "action": act,
        "iat": int(issued),
        "exp": int(issued) + ttl,
    }
    secret = session_capability_secret(session, env=env)
    body = _canonical_payload(payload)
    sig = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"{_b64url_encode(body)}.{sig}"


def verify_audit_capability(
    token: str,
    *,
    task_id: str,
    action: str,
    expected_supervisor: str,
    now: Optional[float] = None,
    env: Optional[Mapping[str, str]] = None,
) -> str:
    """Verify capability and return attested session_id (must equal project supervisor)."""
    raw = str(token or "").strip()
    if not raw or "." not in raw:
        raise AuditContractError(
            "missing or malformed X-Orch-Audit-Capability "
            "(need base64url(payload).hmac from mint_audit_capability)"
        )
    body_b64, sig = raw.rsplit(".", 1)
    if not body_b64 or not sig:
        raise AuditContractError("malformed X-Orch-Audit-Capability token")
    try:
        body = _b64url_decode(body_b64)
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AuditContractError(f"X-Orch-Audit-Capability payload is not valid JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise AuditContractError("X-Orch-Audit-Capability payload must be a JSON object")

    session = str(payload.get("session_id") or "").strip()
    token_task = str(payload.get("task_id") or "").strip()
    token_action = str(payload.get("action") or "").strip()
    if not session:
        raise AuditContractError("capability payload missing session_id")
    if token_task != str(task_id or "").strip():
        raise AuditContractError(
            f"capability task_id mismatch: token={token_task!r} request={task_id!r}"
        )
    if token_action != str(action or "").strip():
        raise AuditContractError(
            f"capability action mismatch: token={token_action!r} required={action!r}"
        )
    try:
        exp = int(payload.get("exp"))
    except (TypeError, ValueError) as exc:
        raise AuditContractError("capability payload missing integer exp") from exc
    current = float(now if now is not None else time.time())
    if current > float(exp):
        raise AuditContractError("audit capability expired")

    secret = session_capability_secret(session, env=env)
    expected_sig = hmac.new(secret.encode("utf-8"), _canonical_payload(payload), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected_sig, str(sig).strip().lower()):
        # Also accept original case hex
        if not hmac.compare_digest(expected_sig, str(sig).strip()):
            raise AuditContractError("audit capability HMAC mismatch (invalid supervisor credential)")

    supervisor = str(expected_supervisor or "").strip()
    if not supervisor or supervisor.lower() in {"unassigned", "unknown", "none", "null"}:
        raise AuditContractError(
            f"capability verified for {session!r} but project supervisor is unset"
        )
    if session != supervisor:
        raise AuditContractError(
            f"attested session {session!r} is not project supervisor {supervisor!r}"
        )
    return session


def resolve_attested_audit_actor(
    *,
    capability_token: Optional[str],
    task_id: str,
    action: str,
    project_supervisor: Optional[str],
    body_from: Optional[str] = None,
    now: Optional[float] = None,
    env: Optional[Mapping[str, str]] = None,
) -> str:
    """Derive actor solely from verified capability; body.from is not authority.

    If body.from is present and differs from the attested session, reject (spoof signal).
    """
    attested = verify_audit_capability(
        str(capability_token or ""),
        task_id=task_id,
        action=action,
        expected_supervisor=str(project_supervisor or ""),
        now=now,
        env=env,
    )
    claimed = str(body_from or "").strip()
    if claimed and claimed != attested:
        raise AuditContractError(
            f"body.from={claimed!r} is not authority and conflicts with attested "
            f"session {attested!r}; omit body.from or match the capability session"
        )
    return attested
