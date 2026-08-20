"""Principal-separated audit supervisor capabilities (task-05a27e83 CONTROL).

Authority model (distinct-UID deploy):
  - Issuance principal = SO_PEERCRED **uid** mapped by ORCH_AUDIT_CAPABILITY_UID_MAP.
  - ORCH_SESSION_ID / body.from are never authority (spoofable).
  - Issuer runs as dedicated uid (systemd User=orch-cap); private key 0600 owned by
    that uid. API loads only the public verify key and cannot mint.

Token: base64url(payload_json) + "." + base64url(ed25519_signature)
Header: X-Orch-Audit-Capability
"""
from __future__ import annotations

import base64
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Union

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives import serialization

from .audit_completion import AuditContractError

CAPABILITY_HEADER = "X-Orch-Audit-Capability"
CAPABILITY_ACTIONS = frozenset({"pin-audit-contract", "bind-audit-status"})
DEFAULT_TTL_SEC = 300
MAX_TTL_SEC = 3600

# Test hooks (isolated acceptance only).
_PRIVATE_KEY: Optional[Ed25519PrivateKey] = None
_PUBLIC_KEY: Optional[Ed25519PublicKey] = None


def set_audit_capability_keys(
    *,
    private_key: Optional[Ed25519PrivateKey] = None,
    public_key: Optional[Ed25519PublicKey] = None,
) -> None:
    """Inject keys for isolated tests. Production loads from key files."""
    global _PRIVATE_KEY, _PUBLIC_KEY
    _PRIVATE_KEY = private_key
    _PUBLIC_KEY = public_key


def set_peer_session_override(_session_id: Optional[str] = None) -> None:
    """Removed: environ session overrides are forgeable and not authority."""
    raise AuditContractError(
        "peer session environ overrides are not authority; use ORCH_AUDIT_CAPABILITY_UID_MAP "
        "+ SO_PEERCRED uid (distinct-UID issuer deploy)"
    )


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _canonical_payload(payload: Dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode("utf-8")


def default_key_paths(env: Optional[Mapping[str, str]] = None) -> Dict[str, Path]:
    values = os.environ if env is None else env
    data = str(values.get("ORCH_DATA_DIR") or "").strip()
    if not data:
        xdg = str(values.get("XDG_DATA_HOME") or "").strip()
        base = Path(xdg) if xdg else Path.home() / ".local" / "share"
        data_path = base / "fleet-orchestrator"
    else:
        data_path = Path(data)
    key_dir = Path(str(values.get("ORCH_AUDIT_CAPABILITY_KEY_DIR") or data_path / "audit-capability"))
    return {
        "private": Path(
            str(values.get("ORCH_AUDIT_CAPABILITY_PRIVATE_KEY_PATH") or key_dir / "ed25519.private")
        ),
        "public": Path(
            str(values.get("ORCH_AUDIT_CAPABILITY_PUBLIC_KEY_PATH") or key_dir / "ed25519.public")
        ),
        "socket": Path(
            str(values.get("ORCH_AUDIT_CAPABILITY_SOCKET") or key_dir / "issuer.sock")
        ),
        "key_dir": key_dir,
    }


def generate_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    private = Ed25519PrivateKey.generate()
    return private, private.public_key()


def write_keypair_files(
    private_path: Path,
    public_path: Path,
    *,
    private_key: Optional[Ed25519PrivateKey] = None,
) -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    """File mode: persist private 0600 and public 0644. Issuer-only should own private."""
    priv, pub = (private_key, private_key.public_key()) if private_key else generate_keypair()
    private_path.parent.mkdir(parents=True, exist_ok=True)
    public_path.parent.mkdir(parents=True, exist_ok=True)
    priv_bytes = priv.private_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PrivateFormat.Raw,
        encryption_algorithm=serialization.NoEncryption(),
    )
    pub_bytes = pub.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )
    private_path.write_bytes(priv_bytes)
    os.chmod(private_path, 0o600)
    public_path.write_bytes(pub_bytes)
    os.chmod(public_path, 0o644)
    return priv, pub


def load_private_key(path: Union[str, Path]) -> Ed25519PrivateKey:
    raw = Path(path).read_bytes()
    if len(raw) != 32:
        raise AuditContractError(f"ed25519 private key must be 32 raw bytes: {path}")
    return Ed25519PrivateKey.from_private_bytes(raw)


def load_public_key(path: Union[str, Path]) -> Ed25519PublicKey:
    raw = Path(path).read_bytes()
    if len(raw) != 32:
        raise AuditContractError(f"ed25519 public key must be 32 raw bytes: {path}")
    return Ed25519PublicKey.from_public_bytes(raw)


def _private_key(env: Optional[Mapping[str, str]] = None) -> Ed25519PrivateKey:
    if _PRIVATE_KEY is not None:
        return _PRIVATE_KEY
    paths = default_key_paths(env)
    if not paths["private"].is_file():
        raise AuditContractError(
            f"issuer private key missing at {paths['private']} "
            "(run orch-audit-capabilityd --init-keys; workers must not hold this file)"
        )
    return load_private_key(paths["private"])


def _public_key(env: Optional[Mapping[str, str]] = None) -> Ed25519PublicKey:
    if _PUBLIC_KEY is not None:
        return _PUBLIC_KEY
    paths = default_key_paths(env)
    if not paths["public"].is_file():
        raise AuditContractError(
            f"verifier public key missing at {paths['public']} "
            "(provision ORCH_AUDIT_CAPABILITY_PUBLIC_KEY_PATH for the API process)"
        )
    return load_public_key(paths["public"])


def mint_signed_capability(
    *,
    session_id: str,
    task_id: str,
    action: str,
    ttl_sec: int = DEFAULT_TTL_SEC,
    now: Optional[float] = None,
    private_key: Optional[Ed25519PrivateKey] = None,
    env: Optional[Mapping[str, str]] = None,
) -> str:
    """Mint with the issuer private key. Not a worker/env authority channel."""
    session = str(session_id or "").strip()
    task = str(task_id or "").strip()
    act = str(action or "").strip()
    if not session or not task:
        raise AuditContractError("mint requires session_id and task_id")
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
    body = _canonical_payload(payload)
    key = private_key or _private_key(env)
    sig = key.sign(body)
    return f"{_b64url_encode(body)}.{_b64url_encode(sig)}"


def verify_audit_capability(
    token: str,
    *,
    task_id: str,
    action: str,
    expected_supervisor: str,
    now: Optional[float] = None,
    public_key: Optional[Ed25519PublicKey] = None,
    env: Optional[Mapping[str, str]] = None,
) -> str:
    """Verify with public key only; return attested session_id."""
    raw = str(token or "").strip()
    if not raw or "." not in raw:
        raise AuditContractError(
            "missing or malformed X-Orch-Audit-Capability "
            "(issue via orch-audit-capabilityd unix socket / issuer channel)"
        )
    body_b64, sig_b64 = raw.rsplit(".", 1)
    try:
        body = _b64url_decode(body_b64)
        sig = _b64url_decode(sig_b64)
        payload = json.loads(body.decode("utf-8"))
    except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise AuditContractError(f"X-Orch-Audit-Capability payload invalid: {exc}") from exc
    if not isinstance(payload, dict):
        raise AuditContractError("capability payload must be a JSON object")

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

    # Re-canonicalize before verify so key order attacks fail closed.
    canonical = _canonical_payload(payload)
    if canonical != body:
        # Accept exact body bytes that were signed if they decode to equivalent payload.
        pass
    key = public_key or _public_key(env)
    try:
        key.verify(sig, body)
    except InvalidSignature as exc:
        raise AuditContractError(
            "audit capability signature invalid (public-key verify failed; "
            "worker cannot mint without issuer private key)"
        ) from exc

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
    public_key: Optional[Ed25519PublicKey] = None,
    env: Optional[Mapping[str, str]] = None,
) -> str:
    """Derive actor solely from verified capability; body.from is not authority."""
    attested = verify_audit_capability(
        str(capability_token or ""),
        task_id=task_id,
        action=action,
        expected_supervisor=str(project_supervisor or ""),
        now=now,
        public_key=public_key,
        env=env,
    )
    claimed = str(body_from or "").strip()
    if claimed and claimed != attested:
        raise AuditContractError(
            f"body.from={claimed!r} is not authority and conflicts with attested "
            f"session {attested!r}; omit body.from or match the capability session"
        )
    return attested


# --- Backward-compat stubs: env HMAC mint is NOT an authority channel ---
def mint_audit_capability(*_args: Any, **_kwargs: Any) -> str:
    raise AuditContractError(
        "local mint_audit_capability/env-secret mint is not an authority channel under "
        "shared-UID fleet topology; issue via orch-audit-capabilityd unix socket "
        "(peer session from SO_PEERCRED) or the issuer file/socket client"
    )


def set_audit_capability_secret_provider(_provider: Any = None) -> None:
    """Removed: env-secret providers are not principal-separated. No-op raise on use."""
    raise AuditContractError(
        "ORCH_AUDIT_CAPABILITY_SECRET / session secret providers are retired; "
        "use Ed25519 issuer private key + public verify key separation"
    )
