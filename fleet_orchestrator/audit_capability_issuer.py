"""Distinct-UID unix-socket issuer for audit supervisor capabilities.

CONTROL (task-05a27e83): ORCH_SESSION_ID from /proc/PID/environ is forgeable.
Principal is derived from SO_PEERCRED **uid** via ORCH_AUDIT_CAPABILITY_UID_MAP
(provisioned supervisor OS users → fleet session ids). Environ is never authority.

Deploy: systemd unit User=orch-cap (or other dedicated uid). Private key file
mode 0600 owned by that uid; API holds only the public key. Workers sharing
mira uid cannot read the private key and are not in the uid→supervisor map.

Deployable unit: ``scripts/orch-audit-capabilityd`` + ``deploy/systemd/orch-audit-capabilityd.*``.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional, Tuple

from .audit_completion import AuditContractError
from .audit_supervisor_capability import (
    CAPABILITY_ACTIONS,
    default_key_paths,
    mint_signed_capability,
    write_keypair_files,
    _private_key,
)

_SO_PEERCRED = getattr(socket, "SO_PEERCRED", 17)
_CRED_FMT = "3i"  # pid, uid, gid
_CRED_SIZE = struct.calcsize(_CRED_FMT)

PeerCred = Tuple[int, int, int]  # pid, uid, gid
PeerCredResolver = Callable[[], PeerCred]
ProjectSupervisorLoader = Callable[[str], Optional[str]]
UidMapLoader = Callable[[], Dict[int, str]]
EuidGetter = Callable[[], int]
StatUidGetter = Callable[[Path], Tuple[int, int]]  # (st_uid, mode)

_PEER_CRED_RESOLVER: Optional[PeerCredResolver] = None
_SUPERVISOR_LOADER: Optional[ProjectSupervisorLoader] = None
_UID_MAP_LOADER: Optional[UidMapLoader] = None
_EUID_GETTER: Optional[EuidGetter] = None
_STAT_UID_GETTER: Optional[StatUidGetter] = None


def set_issuer_hooks(
    *,
    peer_resolver: Optional[Callable[[Optional[int]], str]] = None,  # legacy unused
    supervisor_loader: Optional[ProjectSupervisorLoader] = None,
    peer_cred_resolver: Optional[PeerCredResolver] = None,
    uid_map_loader: Optional[UidMapLoader] = None,
    euid_getter: Optional[EuidGetter] = None,
    stat_uid_getter: Optional[StatUidGetter] = None,
) -> None:
    """Isolated-test hooks. peer_resolver retained as unused kw for call-site compat."""
    global _PEER_CRED_RESOLVER, _SUPERVISOR_LOADER, _UID_MAP_LOADER, _EUID_GETTER, _STAT_UID_GETTER
    del peer_resolver  # environ-based resolvers are not authority
    _PEER_CRED_RESOLVER = peer_cred_resolver
    _SUPERVISOR_LOADER = supervisor_loader
    _UID_MAP_LOADER = uid_map_loader
    _EUID_GETTER = euid_getter
    _STAT_UID_GETTER = stat_uid_getter


def load_uid_principal_map(env: Optional[Dict[str, str]] = None) -> Dict[int, str]:
    """ORCH_AUDIT_CAPABILITY_UID_MAP: JSON object {\"1001\":\"conductor-codex\", ...}."""
    if _UID_MAP_LOADER is not None:
        return dict(_UID_MAP_LOADER())
    values = os.environ if env is None else env
    raw = str(values.get("ORCH_AUDIT_CAPABILITY_UID_MAP") or "").strip()
    if not raw:
        raise AuditContractError(
            "ORCH_AUDIT_CAPABILITY_UID_MAP unset: provision distinct supervisor OS uids "
            "mapped to fleet session ids (environ ORCH_SESSION_ID is not authority)"
        )
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AuditContractError("ORCH_AUDIT_CAPABILITY_UID_MAP must be JSON object") from exc
    if not isinstance(parsed, dict) or not parsed:
        raise AuditContractError("ORCH_AUDIT_CAPABILITY_UID_MAP must be a non-empty JSON object")
    out: Dict[int, str] = {}
    for key, value in parsed.items():
        try:
            uid = int(key)
        except (TypeError, ValueError) as exc:
            raise AuditContractError(f"uid map key must be int-like, got {key!r}") from exc
        session = str(value or "").strip()
        if not session:
            raise AuditContractError(f"uid map entry for {uid} has empty session id")
        out[uid] = session
    return out


def peercred(conn: socket.socket) -> PeerCred:
    try:
        creds = conn.getsockopt(socket.SOL_SOCKET, _SO_PEERCRED, _CRED_SIZE)
    except OSError as exc:
        raise AuditContractError(f"SO_PEERCRED unavailable: {exc}") from exc
    pid, uid, gid = struct.unpack(_CRED_FMT, creds)
    if int(pid) <= 0:
        raise AuditContractError("SO_PEERCRED returned invalid pid")
    return int(pid), int(uid), int(gid)


def resolve_peer_principal(
    *,
    peer_uid: int,
    peer_pid: Optional[int] = None,
    env: Optional[Dict[str, str]] = None,
) -> str:
    """Map peer UID → fleet supervisor session. Environ is intentionally ignored."""
    del peer_pid  # pid is for audit logs only; not principal
    mapping = load_uid_principal_map(env)
    if int(peer_uid) not in mapping:
        raise AuditContractError(
            f"issuer denied: peer uid {peer_uid} is not a provisioned supervisor principal "
            f"in ORCH_AUDIT_CAPABILITY_UID_MAP (spoofed ORCH_SESSION_ID is ignored)"
        )
    return mapping[int(peer_uid)]


def _default_supervisor_loader(task_id: str) -> Optional[str]:
    from .completion_guard import _task_project_supervisor
    from .config import OrchConfig

    return _task_project_supervisor(task_id, OrchConfig())


def load_project_supervisor(task_id: str) -> str:
    loader = _SUPERVISOR_LOADER or _default_supervisor_loader
    supervisor = str(loader(task_id) or "").strip()
    if not supervisor or supervisor.lower() in {"unassigned", "unknown", "none", "null"}:
        raise AuditContractError(
            f"project supervisor unset for task {task_id!r}; cannot issue audit capability"
        )
    return supervisor


def assert_private_key_ownership(path: Path, *, env: Optional[Dict[str, str]] = None) -> None:
    """Fail closed unless issuer euid owns mode-0600 private key (distinct-UID deploy)."""
    del env
    if _STAT_UID_GETTER is not None:
        st_uid, mode = _STAT_UID_GETTER(path)
    else:
        if not path.is_file():
            raise AuditContractError(f"issuer private key missing at {path}")
        st = path.stat()
        st_uid, mode = int(st.st_uid), int(st.st_mode)
    euid = int(_EUID_GETTER() if _EUID_GETTER is not None else os.geteuid())
    if mode & 0o077:
        raise AuditContractError(
            f"issuer private key {path} must be mode 0600 (no group/other access); "
            f"got {oct(mode & 0o777)}"
        )
    if st_uid != euid:
        raise AuditContractError(
            f"issuer process euid={euid} does not own private key st_uid={st_uid} at {path}; "
            "run orch-audit-capabilityd as the dedicated key owner (e.g. User=orch-cap)"
        )


def issue_for_peer_principal(
    *,
    peer_uid: int,
    task_id: str,
    action: str,
    ttl_sec: int = 300,
    env: Optional[Dict[str, str]] = None,
    peer_pid: Optional[int] = None,
) -> Dict[str, Any]:
    """Core issuer: peer UID map principal must equal project supervisor."""
    act = str(action or "").strip()
    if act not in CAPABILITY_ACTIONS:
        raise AuditContractError(f"unsupported action {act!r}")
    principal = resolve_peer_principal(peer_uid=peer_uid, peer_pid=peer_pid, env=env)
    supervisor = load_project_supervisor(task_id)
    if principal != supervisor:
        raise AuditContractError(
            f"issuer denied: peer principal {principal!r} (uid={peer_uid}) is not project "
            f"supervisor {supervisor!r} for task {task_id!r}"
        )
    token = mint_signed_capability(
        session_id=principal,
        task_id=task_id,
        action=act,
        ttl_sec=ttl_sec,
        env=env,
    )
    return {
        "ok": True,
        "capability": token,
        "session_id": principal,
        "peer_uid": int(peer_uid),
        "task_id": task_id,
        "action": act,
        "supervisor": supervisor,
    }


# Backward-compatible name used by older tests — now requires peer_uid, not session string.
def issue_for_peer_session(
    *,
    peer_session: str = "",
    peer_uid: Optional[int] = None,
    task_id: str,
    action: str,
    ttl_sec: int = 300,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    """Deprecated session-string entry: only valid when peer_uid maps to that session."""
    if peer_uid is None:
        raise AuditContractError(
            "issue_for_peer_session requires peer_uid (environ session strings are not authority)"
        )
    result = issue_for_peer_principal(
        peer_uid=int(peer_uid),
        task_id=task_id,
        action=action,
        ttl_sec=ttl_sec,
        env=env,
    )
    claimed = str(peer_session or "").strip()
    if claimed and claimed != result["session_id"]:
        raise AuditContractError(
            f"claimed peer_session {claimed!r} conflicts with uid-mapped principal "
            f"{result['session_id']!r} (environ spoof ignored)"
        )
    return result


def handle_issuer_request(
    request: Dict[str, Any],
    *,
    peer_uid: int,
    peer_pid: Optional[int] = None,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    cmd = str(request.get("cmd") or "issue").strip()
    if cmd == "ping":
        return {
            "ok": True,
            "pong": True,
            "peer_uid": int(peer_uid),
            "principal": resolve_peer_principal(peer_uid=peer_uid, peer_pid=peer_pid, env=env),
        }
    if cmd != "issue":
        raise AuditContractError(f"unknown issuer cmd {cmd!r}")
    # Ignore any client-supplied session_id / from fields.
    if any(k in request for k in ("session_id", "from", "ORCH_SESSION_ID", "peer_session")):
        # Soft ignore for identity; hard-reject if they try to override uid.
        if "peer_uid" in request:
            raise AuditContractError("client cannot supply peer_uid; SO_PEERCRED is authoritative")
    return issue_for_peer_principal(
        peer_uid=int(peer_uid),
        peer_pid=peer_pid,
        task_id=str(request.get("task_id") or ""),
        action=str(request.get("action") or ""),
        ttl_sec=int(request.get("ttl_sec") or 300),
        env=env,
    )


def _recv_json(conn: socket.socket, limit: int = 1 << 16) -> Dict[str, Any]:
    buf = bytearray()
    while b"\n" not in buf and len(buf) < limit:
        chunk = conn.recv(4096)
        if not chunk:
            break
        buf.extend(chunk)
    if not buf:
        raise AuditContractError("empty issuer request")
    line = bytes(buf).split(b"\n", 1)[0]
    try:
        payload = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuditContractError(f"issuer request is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise AuditContractError("issuer request must be a JSON object")
    return payload


def _send_json(conn: socket.socket, payload: Dict[str, Any]) -> None:
    conn.sendall((json.dumps(payload, sort_keys=True) + "\n").encode("utf-8"))


def serve_client(conn: socket.socket, _addr: Any = None, *, env: Optional[Dict[str, str]] = None) -> None:
    try:
        if _PEER_CRED_RESOLVER is not None:
            pid, uid, _gid = _PEER_CRED_RESOLVER()
        else:
            pid, uid, _gid = peercred(conn)
        request = _recv_json(conn)
        result = handle_issuer_request(request, peer_uid=uid, peer_pid=pid, env=env)
        _send_json(conn, result)
    except AuditContractError as exc:
        try:
            _send_json(conn, {"ok": False, "error": str(exc)})
        except OSError:
            pass
    except Exception as exc:  # noqa: BLE001
        try:
            _send_json(conn, {"ok": False, "error": f"issuer internal error: {exc}"})
        except OSError:
            pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def _take_systemd_listen_socket() -> Optional[socket.socket]:
    """Consume systemd socket activation FD (LISTEN_FDS). Never unlink/rebind that path."""
    values = os.environ
    listen_pid = str(values.get("LISTEN_PID") or "").strip()
    listen_fds = str(values.get("LISTEN_FDS") or "").strip()
    if not listen_pid or not listen_fds:
        return None
    try:
        if int(listen_pid) != int(os.getpid()):
            return None
        n_fds = int(listen_fds)
    except ValueError:
        return None
    if n_fds < 1:
        return None
    # sd_listen_fds: first FD is always 3
    fd = 3
    # fromfd duplicates; close the original systemd FD number to avoid leaks while
    # keeping the listening socket on the duplicate.
    server = socket.fromfd(fd, socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        os.close(fd)
    except OSError:
        pass
    return server


def run_socket_server(
    socket_path: Optional[Path] = None,
    *,
    init_keys: bool = False,
    env: Optional[Dict[str, str]] = None,
    # Test hook: pre-bound listening socket (simulates LISTEN_FDS without systemd).
    prebound_server: Optional[socket.socket] = None,
) -> None:
    """Blocking socket server (deployable unit entry; run as dedicated uid).

    Under systemd socket activation, inherits the listening FD and must NOT
    unlink/rebind the socket pathname (that abandons the activated connection).
    """
    paths = default_key_paths(env)
    sock_path = Path(socket_path or paths["socket"])
    if init_keys or not paths["private"].is_file() or not paths["public"].is_file():
        write_keypair_files(paths["private"], paths["public"])
    assert_private_key_ownership(paths["private"], env=env)

    from .audit_supervisor_capability import load_public_key, set_audit_capability_keys

    priv = _private_key(env)
    pub = load_public_key(paths["public"])
    set_audit_capability_keys(private_key=priv, public_key=pub)
    load_uid_principal_map(env)

    activated = False
    if prebound_server is not None:
        server = prebound_server
        activated = True
    else:
        inherited = _take_systemd_listen_socket()
        if inherited is not None:
            server = inherited
            activated = True
        else:
            sock_path.parent.mkdir(parents=True, exist_ok=True)
            if sock_path.exists():
                sock_path.unlink()
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            server.bind(str(sock_path))
            # Manual bind path: issuer uid owns socket; client group is deploy-time.
            os.chmod(sock_path, 0o660)
            server.listen(16)

    try:
        while True:
            conn, addr = server.accept()
            threading.Thread(
                target=serve_client,
                args=(conn, addr),
                kwargs={"env": env},
                daemon=True,
            ).start()
    finally:
        try:
            server.close()
        except OSError:
            pass
        # Never unlink an activated systemd socket path — the unit owns it.
        if not activated and sock_path.exists():
            try:
                sock_path.unlink()
            except OSError:
                pass

def issue_audit_capability(
    *,
    task_id: str,
    action: str,
    ttl_sec: int = 300,
    socket_path: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    # In-process test channel: supply peer_uid (NOT session string).
    inprocess_peer_uid: Optional[int] = None,
    inprocess_peer_session: Optional[str] = None,  # if set, only checked against uid map result
) -> Dict[str, Any]:
    """Client: issue through socket, or in-process peer_uid simulation for tests."""
    if inprocess_peer_uid is not None:
        return issue_for_peer_session(
            peer_uid=int(inprocess_peer_uid),
            peer_session=str(inprocess_peer_session or ""),
            task_id=task_id,
            action=action,
            ttl_sec=ttl_sec,
            env=env,
        )
    if inprocess_peer_session is not None and inprocess_peer_uid is None:
        raise AuditContractError(
            "inprocess_peer_session alone is not authority; supply inprocess_peer_uid "
            "(spoofed environ session strings are rejected)"
        )

    paths = default_key_paths(env)
    path = Path(socket_path or paths["socket"])
    if not path.exists():
        raise AuditContractError(
            f"audit capability issuer socket missing at {path}; "
            "start orch-audit-capabilityd as the dedicated User=orch-cap unit"
        )
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.connect(str(path))
        req = {
            "cmd": "issue",
            "task_id": task_id,
            "action": action,
            "ttl_sec": int(ttl_sec),
        }
        _send_json(client, req)
        raw = bytearray()
        while b"\n" not in raw:
            chunk = client.recv(4096)
            if not chunk:
                break
            raw.extend(chunk)
        if not raw:
            raise AuditContractError("empty response from capability issuer")
        payload = json.loads(bytes(raw).split(b"\n", 1)[0].decode("utf-8"))
    except OSError as exc:
        raise AuditContractError(f"issuer socket connect failed: {exc}") from exc
    finally:
        try:
            client.close()
        except OSError:
            pass
    if not isinstance(payload, dict) or not payload.get("ok"):
        raise AuditContractError(
            str((payload or {}).get("error") if isinstance(payload, dict) else payload)
            or "issuer denied"
        )
    return payload
