"""Unix-socket issuer for audit supervisor capabilities.

Principal: SO_PEERCRED **uid** mapped by ORCH_AUDIT_CAPABILITY_UID_MAP.
tmux pane ancestry and ORCH_SESSION_ID environ are not authority — the shared
mira uid owns the tmux server and can spawn a process in any session name.

Deploy: systemd User=orch-cap, socket 0660 orch-cap:orch-audit-sup, private key
0600 owned by orch-cap. Socket activation via LISTEN_FDS (do not unlink/rebind
the inherited fd). API processes load the public key only.
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
_CRED_FMT = "3i"
_CRED_SIZE = struct.calcsize(_CRED_FMT)
SD_LISTEN_FDS_START = 3

PeerCred = Tuple[int, int, int]  # pid, uid, gid
UidMapLoader = Callable[[], Dict[int, str]]
ProjectSupervisorLoader = Callable[[str], Optional[str]]
PeerCredResolver = Callable[[socket.socket], PeerCred]
EuidGetter = Callable[[], int]
StatUidGetter = Callable[[Path], Tuple[int, int]]

_UID_MAP_LOADER: Optional[UidMapLoader] = None
_SUPERVISOR_LOADER: Optional[ProjectSupervisorLoader] = None
_PEER_CRED_RESOLVER: Optional[PeerCredResolver] = None
_EUID_GETTER: Optional[EuidGetter] = None
_STAT_UID_GETTER: Optional[StatUidGetter] = None


def set_issuer_hooks(
    *,
    uid_map_loader: Optional[UidMapLoader] = None,
    supervisor_loader: Optional[ProjectSupervisorLoader] = None,
    peer_cred_resolver: Optional[PeerCredResolver] = None,
    euid_getter: Optional[EuidGetter] = None,
    stat_uid_getter: Optional[StatUidGetter] = None,
    peer_resolver: Any = None,
) -> None:
    """Isolated-test hooks. peer_resolver is rejected — session strings are not authority."""
    if peer_resolver is not None:
        raise AuditContractError(
            "peer_resolver/session-string hooks are not an authority channel; "
            "inject uid_map_loader / peer_cred_resolver (pid,uid,gid) only"
        )
    global _UID_MAP_LOADER, _SUPERVISOR_LOADER, _PEER_CRED_RESOLVER, _EUID_GETTER, _STAT_UID_GETTER
    _UID_MAP_LOADER = uid_map_loader
    _SUPERVISOR_LOADER = supervisor_loader
    _PEER_CRED_RESOLVER = peer_cred_resolver
    _EUID_GETTER = euid_getter
    _STAT_UID_GETTER = stat_uid_getter


def load_uid_principal_map(env: Optional[Dict[str, str]] = None) -> Dict[int, str]:
    if _UID_MAP_LOADER is not None:
        mapping = _UID_MAP_LOADER()
        if not mapping:
            raise AuditContractError("uid principal map is empty")
        return {int(k): str(v) for k, v in mapping.items()}
    values = os.environ if env is None else env
    raw = str(values.get("ORCH_AUDIT_CAPABILITY_UID_MAP") or "").strip()
    if not raw:
        raise AuditContractError(
            "ORCH_AUDIT_CAPABILITY_UID_MAP unset: provision distinct supervisor OS uids "
            "mapped to fleet session ids (tmux/ORCH_SESSION_ID are not authority). "
            "Next step: set ORCH_AUDIT_CAPABILITY_UID_MAP JSON object of uid->session "
            "and run scripts/orch-audit-capabilityd under systemd User=orch-cap"
        )
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AuditContractError(f"ORCH_AUDIT_CAPABILITY_UID_MAP is not JSON: {exc}") from exc
    if not isinstance(payload, dict) or not payload:
        raise AuditContractError("ORCH_AUDIT_CAPABILITY_UID_MAP must be a non-empty JSON object")
    out: Dict[int, str] = {}
    for key, value in payload.items():
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


def resolve_peer_principal(*, peer_uid: int, env: Optional[Dict[str, str]] = None) -> str:
    mapping = load_uid_principal_map(env)
    if int(peer_uid) not in mapping:
        raise AuditContractError(
            f"issuer denied: peer uid {peer_uid} is not a provisioned supervisor principal "
            "(shared-UID workers and tmux-spawned processes are not in ORCH_AUDIT_CAPABILITY_UID_MAP)"
        )
    return mapping[int(peer_uid)]


def assert_private_key_ownership(path: Path, env: Optional[Dict[str, str]] = None) -> None:
    """Issuer euid must own mode-0600 private key (distinct-UID deploy)."""
    if _STAT_UID_GETTER is not None:
        st_uid, mode = _STAT_UID_GETTER(path)
    else:
        st = path.stat()
        st_uid, mode = int(st.st_uid), int(st.st_mode)
    euid = int(_EUID_GETTER() if _EUID_GETTER is not None else os.geteuid())
    if (mode & 0o777) != 0o600:
        raise AuditContractError(
            f"issuer private key mode must be 0600, got {oct(mode & 0o777)} at {path}"
        )
    if st_uid != euid:
        raise AuditContractError(
            f"issuer process euid={euid} does not own private key st_uid={st_uid} at {path}; "
            "workers sharing the API uid must not mint. Next step: run orch-audit-capabilityd "
            "as User=orch-cap with key owned by that uid"
        )


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


def issue_for_peer_uid(
    *,
    peer_uid: int,
    task_id: str,
    action: str,
    ttl_sec: int = 300,
    peer_pid: int = 0,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    act = str(action or "").strip()
    if act not in CAPABILITY_ACTIONS:
        raise AuditContractError(f"unsupported action {act!r}")
    principal = resolve_peer_principal(peer_uid=int(peer_uid), env=env)
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
        "task_id": task_id,
        "action": act,
        "supervisor": supervisor,
        "peer_uid": int(peer_uid),
        "peer_pid": int(peer_pid),
    }


def handle_issuer_request(
    request: Dict[str, Any],
    *,
    peer_uid: int,
    peer_pid: int,
    env: Optional[Dict[str, str]] = None,
) -> Dict[str, Any]:
    cmd = str(request.get("cmd") or "issue").strip()
    if cmd == "ping":
        return {
            "ok": True,
            "pong": True,
            "peer_uid": int(peer_uid),
            "principal": resolve_peer_principal(peer_uid=peer_uid, env=env),
        }
    if cmd != "issue":
        raise AuditContractError(f"unknown issuer cmd {cmd!r}")
    if "peer_uid" in request or "session_id" in request or "from" in request:
        raise AuditContractError(
            "client cannot supply peer_uid/session_id/from; SO_PEERCRED uid is authoritative"
        )
    return issue_for_peer_uid(
        peer_uid=int(peer_uid),
        peer_pid=int(peer_pid),
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


def serve_client(conn: socket.socket, _addr: Any = None, env: Optional[Dict[str, str]] = None) -> None:
    try:
        if _PEER_CRED_RESOLVER is not None:
            pid, uid, _gid = _PEER_CRED_RESOLVER(conn)
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
    except Exception as exc:  # noqa: BLE001 — keep the issuer loop alive
        try:
            _send_json(conn, {"ok": False, "error": f"issuer internal error: {exc}"})
        except OSError:
            pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def systemd_listen_sockets() -> list[socket.socket]:
    """Consume LISTEN_FDS from systemd socket activation. Empty when not activated."""
    try:
        n = int(os.environ.get("LISTEN_FDS") or "0")
    except ValueError:
        return []
    if n <= 0:
        return []
    listen_pid = str(os.environ.get("LISTEN_PID") or "").strip()
    if listen_pid and listen_pid != str(os.getpid()):
        return []
    socks: list[socket.socket] = []
    for index in range(n):
        fd = SD_LISTEN_FDS_START + index
        os.set_inheritable(fd, False)
        socks.append(socket.socket(fileno=fd))
    return socks


def run_socket_server(
    socket_path: Optional[Path] = None,
    *,
    init_keys: bool = False,
    env: Optional[Dict[str, str]] = None,
) -> None:
    """Blocking socket server. Prefer systemd LISTEN_FDS; never unlink an inherited socket."""
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

    inherited = systemd_listen_sockets()
    owned_bind = False
    if inherited:
        server = inherited[0]
    else:
        sock_path.parent.mkdir(parents=True, exist_ok=True)
        if sock_path.exists():
            sock_path.unlink()
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(sock_path))
        os.chmod(sock_path, 0o660)
        server.listen(16)
        owned_bind = True
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
        server.close()
        if owned_bind and sock_path.exists():
            sock_path.unlink()


def issue_audit_capability(
    *,
    task_id: str,
    action: str,
    ttl_sec: int = 300,
    socket_path: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    inprocess_peer_uid: Optional[int] = None,
) -> Dict[str, Any]:
    """Client: connect to the issuer socket, or in-process uid simulation for tests."""
    if inprocess_peer_uid is not None:
        return issue_for_peer_uid(
            peer_uid=int(inprocess_peer_uid),
            task_id=task_id,
            action=action,
            ttl_sec=ttl_sec,
            env=env,
        )

    paths = default_key_paths(env)
    path = Path(socket_path or paths["socket"])
    if not path.exists():
        raise AuditContractError(
            f"audit capability issuer socket missing at {path}; "
            "start orch-audit-capabilityd via systemd socket activation "
            "(User=orch-cap, Group=orch-audit-sup, LISTEN_FDS)"
        )
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        client.connect(str(path))
        req = {"cmd": "issue", "task_id": task_id, "action": action, "ttl_sec": int(ttl_sec)}
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
