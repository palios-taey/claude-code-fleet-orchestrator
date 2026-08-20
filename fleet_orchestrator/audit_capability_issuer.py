"""Unix-socket / channel issuer for audit supervisor capabilities.

Deployable unit: ``scripts/orch-audit-capabilityd`` (socket server).
Client: ``issue_audit_capability(task_id, action)`` connects to the socket.

Principal under shared-UID topology (not env, not JSON body.from):
  SO_PEERCRED pid → ancestor walk of /proc/<pid>/stat ppid → tmux pane pid
  → tmux session name. ORCH_SESSION_ID in the peer environ is ignored and
  cannot forge the supervisor name.

Same-UID workers are denied because their pane belongs to a different tmux
session than the project supervisor.
"""
from __future__ import annotations

import json
import os
import socket
import struct
import subprocess
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .audit_completion import AuditContractError
from .audit_supervisor_capability import (
    CAPABILITY_ACTIONS,
    default_key_paths,
    mint_signed_capability,
    write_keypair_files,
    _private_key,
)

# Linux SO_PEERCRED
_SO_PEERCRED = getattr(socket, "SO_PEERCRED", 17)
_CRED_FMT = "3i"  # pid, uid, gid
_CRED_SIZE = struct.calcsize(_CRED_FMT)

PeerSessionResolver = Callable[[Optional[int]], str]
ProjectSupervisorLoader = Callable[[str], Optional[str]]

_PEER_RESOLVER: Optional[PeerSessionResolver] = None
_SUPERVISOR_LOADER: Optional[ProjectSupervisorLoader] = None


def set_issuer_hooks(
    *,
    peer_resolver: Optional[PeerSessionResolver] = None,
    supervisor_loader: Optional[ProjectSupervisorLoader] = None,
) -> None:
    """Isolated-test hooks for peer session + supervisor lookup."""
    global _PEER_RESOLVER, _SUPERVISOR_LOADER
    _PEER_RESOLVER = peer_resolver
    _SUPERVISOR_LOADER = supervisor_loader


def read_ppid(pid: int) -> int:
    path = Path(f"/proc/{int(pid)}/stat")
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise AuditContractError(f"cannot read peer stat for pid={pid}: {exc}") from exc
    close = raw.rfind(")")
    if close < 0:
        raise AuditContractError(f"malformed /proc/{pid}/stat")
    fields = raw[close + 1 :].split()
    if len(fields) < 2:
        raise AuditContractError(f"malformed /proc/{pid}/stat ppid")
    try:
        return int(fields[1])
    except ValueError as exc:
        raise AuditContractError(f"malformed /proc/{pid}/stat ppid") from exc


def tmux_pane_pid_sessions() -> Dict[int, str]:
    try:
        result = subprocess.run(
            ["tmux", "list-panes", "-a", "-F", "#{pane_pid} #{session_name}"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
    except (FileNotFoundError, OSError, subprocess.TimeoutExpired) as exc:
        raise AuditContractError(
            f"tmux pane ownership map unavailable (fail-closed, env is not authority): {exc}"
        ) from exc
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or f"exit {result.returncode}").strip()
        raise AuditContractError(f"tmux list-panes failed: {detail}")
    mapping: Dict[int, str] = {}
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        pid_text, sep, session = line.partition(" ")
        if not sep or not session.strip():
            continue
        try:
            mapping[int(pid_text)] = session.strip()
        except ValueError:
            continue
    if not mapping:
        raise AuditContractError("tmux pane ownership map is empty; cannot attest principal")
    return mapping


def tmux_session_for_pid(pid: int) -> str:
    mapping = tmux_pane_pid_sessions()
    seen: set[int] = set()
    current = int(pid)
    while current > 1 and current not in seen:
        seen.add(current)
        session = mapping.get(current)
        if session:
            return session
        try:
            current = read_ppid(current)
        except AuditContractError:
            break
    raise AuditContractError(
        f"peer pid={pid} is not owned by a tmux session pane; "
        "ORCH_SESSION_ID/environ is not authority under shared-UID topology"
    )


def resolve_peer_session(pid: Optional[int]) -> str:
    if _PEER_RESOLVER is not None:
        session = str(_PEER_RESOLVER(pid) or "").strip()
        if not session:
            raise AuditContractError("peer session resolver returned empty identity")
        return session
    if pid is None:
        raise AuditContractError("peer pid required to resolve session identity")
    return tmux_session_for_pid(int(pid))


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


def peercred_pid(conn: socket.socket) -> int:
    try:
        creds = conn.getsockopt(socket.SOL_SOCKET, _SO_PEERCRED, _CRED_SIZE)
    except OSError as exc:
        raise AuditContractError(f"SO_PEERCRED unavailable: {exc}") from exc
    pid, _uid, _gid = struct.unpack(_CRED_FMT, creds)
    if int(pid) <= 0:
        raise AuditContractError("SO_PEERCRED returned invalid pid")
    return int(pid)


def issue_for_peer_session(
    *,
    peer_session: str,
    task_id: str,
    action: str,
    ttl_sec: int = 300,
) -> Dict[str, Any]:
    """Core issuer: peer session must equal project supervisor."""
    act = str(action or "").strip()
    if act not in CAPABILITY_ACTIONS:
        raise AuditContractError(f"unsupported action {act!r}")
    supervisor = load_project_supervisor(task_id)
    session = str(peer_session or "").strip()
    if session != supervisor:
        raise AuditContractError(
            f"issuer denied: peer session {session!r} is not project supervisor "
            f"{supervisor!r} for task {task_id!r} (same-UID workers cannot forge peercred)"
        )
    token = mint_signed_capability(
        session_id=session,
        task_id=task_id,
        action=act,
        ttl_sec=ttl_sec,
    )
    return {
        "ok": True,
        "capability": token,
        "session_id": session,
        "task_id": task_id,
        "action": act,
        "supervisor": supervisor,
    }


def handle_issuer_request(
    request: Dict[str, Any],
    *,
    peer_session: str,
) -> Dict[str, Any]:
    cmd = str(request.get("cmd") or "issue").strip()
    if cmd == "ping":
        return {"ok": True, "pong": True, "peer_session": peer_session}
    if cmd != "issue":
        raise AuditContractError(f"unknown issuer cmd {cmd!r}")
    return issue_for_peer_session(
        peer_session=peer_session,
        task_id=str(request.get("task_id") or ""),
        action=str(request.get("action") or ""),
        ttl_sec=int(request.get("ttl_sec") or 300),
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


def serve_client(conn: socket.socket, _addr: Any = None) -> None:
    try:
        pid = peercred_pid(conn)
        peer_session = resolve_peer_session(pid)
        request = _recv_json(conn)
        result = handle_issuer_request(request, peer_session=peer_session)
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


def run_socket_server(
    socket_path: Optional[Path] = None,
    *,
    init_keys: bool = False,
    env: Optional[Dict[str, str]] = None,
) -> None:
    """Blocking socket server (deployable unit entry)."""
    paths = default_key_paths(env)
    sock_path = Path(socket_path or paths["socket"])
    if init_keys or not paths["private"].is_file() or not paths["public"].is_file():
        write_keypair_files(paths["private"], paths["public"])
    # Load and pin issuer private key in-process (workers/API never get this handle).
    from .audit_supervisor_capability import load_public_key, set_audit_capability_keys

    priv = _private_key(env)
    pub = load_public_key(paths["public"])
    set_audit_capability_keys(private_key=priv, public_key=pub)

    sock_path.parent.mkdir(parents=True, exist_ok=True)
    if sock_path.exists():
        sock_path.unlink()
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(sock_path))
    os.chmod(sock_path, 0o660)
    server.listen(16)
    try:
        while True:
            conn, addr = server.accept()
            threading.Thread(target=serve_client, args=(conn, addr), daemon=True).start()
    finally:
        server.close()
        if sock_path.exists():
            sock_path.unlink()


def issue_audit_capability(
    *,
    task_id: str,
    action: str,
    ttl_sec: int = 300,
    socket_path: Optional[Path] = None,
    env: Optional[Dict[str, str]] = None,
    # In-process channel for isolated tests (no real socket): supply peer_session.
    inprocess_peer_session: Optional[str] = None,
) -> Dict[str, Any]:
    """Client: issue through socket channel, or in-process peer simulation for tests."""
    if inprocess_peer_session is not None:
        return issue_for_peer_session(
            peer_session=inprocess_peer_session,
            task_id=task_id,
            action=action,
            ttl_sec=ttl_sec,
        )

    paths = default_key_paths(env)
    path = Path(socket_path or paths["socket"])
    if not path.exists():
        raise AuditContractError(
            f"audit capability issuer socket missing at {path}; "
            "start scripts/orch-audit-capabilityd (supervisor deployable unit)"
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
