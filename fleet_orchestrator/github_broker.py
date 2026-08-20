"""GitHub outward broker — separate credential principal over Unix sockets.

Workers never receive GH_TOKEN, never learn the inner ``gh`` path, and cannot
mutate GitHub except by sending argv to the **exec** socket. Mint and revoke
happen only on a separate authenticated **control** socket, atomic with
bind/unbind. The broker maps SO_PEERCRED uid to a broker-owned principal set.

Production shape (CONTROL deploy, not this PR): systemd ``User=github-broker``
with a 0600 EnvironmentFile the worker UID cannot read, distinct worker UIDs,
and a 0600 control socket the worker principal cannot open.
"""
from __future__ import annotations

import json
import os
import secrets
import select
import shutil
import socket
import stat
import struct
import subprocess
from pathlib import Path
from typing import Any, Optional

from .outward_capability import (
    OutwardAuthorizationError,
    github_argv_requires_outward_capability,
    require_github_argv_capability,
)

# Broker-process possession map. Never written to Redis or current_task.
_HANDLES: dict[str, dict[str, Any]] = {}

WORKER_UNSET_KEYS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN")
LIVE_PREFIXES = (
    Path("/usr"),
    Path("/usr/local"),
    Path("/bin"),
    Path("/home/mira/.local"),
)
# Linux SO_PEERCRED; socket.SO_PEERCRED is not always present in Python.
_SO_PEERCRED = getattr(socket, "SO_PEERCRED", 17)
_PEERCRED_SIZE = struct.calcsize("3i")


class GitHubBrokerInstallError(RuntimeError):
    """Raised when a broker install would touch live paths or miss inputs."""


class GitHubBrokerClientError(RuntimeError):
    """Raised when the worker client cannot reach the broker socket."""


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve()


def default_control_socket_path(exec_socket: str) -> str:
    """Control socket lives in a broker-private sibling dir, not worker PATH."""
    parent = Path(exec_socket).expanduser()
    return str(parent.parent / "control" / "github-broker-control.sock")


def parse_uid_set(raw: str) -> set[int]:
    uids: set[int] = set()
    for part in str(raw or "").replace(",", " ").split():
        uids.add(int(part, 10))
    return uids


def load_principal_map(
    *,
    control_uids: Optional[set[int]] = None,
    worker_uids: Optional[set[int]] = None,
) -> dict[str, set[int]]:
    """Broker-owned uid → role map. Empty control set defaults to broker uid."""
    control = set(control_uids) if control_uids is not None else parse_uid_set(
        os.environ.get("ORCH_GITHUB_BROKER_CONTROL_UIDS", "")
    )
    worker = set(worker_uids) if worker_uids is not None else parse_uid_set(
        os.environ.get("ORCH_GITHUB_BROKER_WORKER_UIDS", "")
    )
    if not control:
        control = {os.getuid()}
    return {"control": control, "worker": worker}


def peer_credentials(conn: socket.socket) -> tuple[int, int, int]:
    """Return (pid, uid, gid) from SO_PEERCRED. Fail closed on error."""
    try:
        creds = conn.getsockopt(socket.SOL_SOCKET, _SO_PEERCRED, _PEERCRED_SIZE)
        pid, uid, gid = struct.unpack("3i", creds)
        return int(pid), int(uid), int(gid)
    except OSError as exc:
        raise GitHubBrokerClientError(f"SO_PEERCRED unavailable: {exc}") from exc


def peer_may_control(uid: int, mapping: dict[str, set[int]]) -> bool:
    return int(uid) in mapping["control"]


def peer_may_exec(uid: int, mapping: dict[str, set[int]]) -> bool:
    workers = mapping["worker"]
    if not workers:
        return True
    uid_i = int(uid)
    return uid_i in workers or uid_i in mapping["control"]


def mint_broker_handle(session: str, task_id: str, started_at: Any = None) -> str:
    handle = secrets.token_urlsafe(32)
    _HANDLES[handle] = {
        "session": str(session),
        "task_id": str(task_id),
        "started_at": started_at,
    }
    return handle


def lookup_broker_handle(handle: str) -> Optional[dict[str, Any]]:
    value = str(handle or "").strip()
    if not value:
        return None
    binding = _HANDLES.get(value)
    if not isinstance(binding, dict):
        return None
    if not str(binding.get("session") or "").strip():
        return None
    return dict(binding)


def revoke_broker_handles(session: str, task_id: str = "") -> int:
    session_id = str(session or "").strip()
    task = str(task_id or "").strip()
    removed = 0
    for handle, binding in list(_HANDLES.items()):
        if str(binding.get("session") or "") != session_id:
            continue
        if task and str(binding.get("task_id") or "") != task:
            continue
        del _HANDLES[handle]
        removed += 1
    return removed


def prefix_is_live(prefix: Path) -> bool:
    target = _resolved(prefix)
    for live in LIVE_PREFIXES:
        live_r = live.resolve()
        if target == live_r or live_r in target.parents:
            return True
    return False


def handle_broker_request(
    argv: list[str],
    *,
    handle: str,
    claimed_session: str = "",
    inner_gh: str,
    token: str,
    redis_client: Any = None,
    task_loader: Any = None,
) -> dict[str, Any]:
    """Authorize mutating argv from a possession handle, never a claimed session."""
    mutating = github_argv_requires_outward_capability(argv)
    if mutating:
        binding = lookup_broker_handle(handle)
        if not binding:
            return {
                "rc": 1,
                "stdout": "",
                "stderr": "SAFETY DENY: missing or revoked outward possession handle\n",
            }
        bound_session = str(binding.get("session") or "").strip()
        bound_task = str(binding.get("task_id") or "").strip()
        if claimed_session and claimed_session != bound_session:
            return {
                "rc": 1,
                "stdout": "",
                "stderr": "SAFETY DENY: possession handle cannot be exchanged for another session\n",
            }
        try:
            decision = require_github_argv_capability(
                argv,
                session_id=bound_session,
                redis_client=redis_client,
                task_loader=task_loader,
            )
        except OutwardAuthorizationError as exc:
            return {"rc": 1, "stdout": "", "stderr": f"SAFETY DENY: {exc}\n"}
        if (
            decision is not None
            and bound_task
            and str(decision.task_id or "").strip()
            and bound_task != str(decision.task_id)
        ):
            return {
                "rc": 1,
                "stdout": "",
                "stderr": "SAFETY DENY: possession handle does not match live bound task\n",
            }
    inner = str(inner_gh or "").strip()
    if not inner or not Path(inner).is_file():
        return {"rc": 1, "stdout": "", "stderr": "SAFETY DENY: inner gh missing in broker principal\n"}
    env = {key: value for key, value in os.environ.items() if key not in WORKER_UNSET_KEYS}
    env["GH_TOKEN"] = str(token)
    result = subprocess.run(
        [inner, *argv],
        capture_output=True,
        text=True,
        check=False,
        env=env,
    )
    return {
        "rc": int(result.returncode),
        "stdout": result.stdout or "",
        "stderr": result.stderr or "",
    }


def _deny_payload(message: str) -> dict[str, Any]:
    return {"rc": 1, "stdout": "", "stderr": f"SAFETY DENY: {message}\n"}


def _send_json(conn: socket.socket, payload: dict[str, Any]) -> None:
    conn.sendall((json.dumps(payload) + "\n").encode("utf-8"))


def _recv_request(conn: socket.socket) -> Optional[dict[str, Any]]:
    raw = b""
    while b"\n" not in raw:
        chunk = conn.recv(65536)
        if not chunk:
            break
        raw += chunk
    if not raw.strip():
        return None
    try:
        request = json.loads(raw.decode("utf-8"))
    except json.JSONDecodeError:
        return {"_invalid": True}
    if not isinstance(request, dict):
        return {"_invalid": True}
    return request


def _bind_unix_socket(path: Path, mode: int) -> socket.socket:
    if path.exists():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    os.chmod(str(path), mode)
    sock.listen(16)
    return sock


def _serve_one(
    conn: socket.socket,
    *,
    channel: str,
    inner_gh: str,
    token: str,
    redis_client: Any,
    task_loader: Any,
    mapping: dict[str, set[int]],
) -> None:
    try:
        _pid, uid, _gid = peer_credentials(conn)
    except GitHubBrokerClientError:
        _send_json(conn, _deny_payload("peer credentials required"))
        return
    request = _recv_request(conn)
    if request is None:
        return
    if request.get("_invalid"):
        _send_json(conn, _deny_payload("invalid broker request"))
        return
    op = str(request.get("op") or "exec").strip()
    if channel == "exec":
        if not peer_may_exec(uid, mapping):
            _send_json(conn, _deny_payload("exec principal not mapped"))
            return
        if op in {"mint", "revoke"}:
            _send_json(
                conn,
                _deny_payload("mint/revoke only on authenticated control channel"),
            )
            return
        if op == "resolve":
            binding = lookup_broker_handle(str(request.get("handle") or ""))
            if not binding:
                _send_json(conn, _deny_payload("missing or revoked outward possession handle"))
                return
            _send_json(
                conn,
                {
                    "rc": 0,
                    "session": str(binding.get("session") or ""),
                    "task_id": str(binding.get("task_id") or ""),
                },
            )
            return
        if op != "exec":
            _send_json(conn, _deny_payload("exec socket accepts op=exec or op=resolve only"))
            return
        argv = request.get("argv")
        if not isinstance(argv, list):
            _send_json(conn, _deny_payload("argv required"))
            return
        payload = handle_broker_request(
            [str(item) for item in argv],
            handle=str(request.get("handle") or ""),
            claimed_session=str(request.get("session") or ""),
            inner_gh=inner_gh,
            token=token,
            redis_client=redis_client,
            task_loader=task_loader,
        )
        _send_json(conn, payload)
        return
    if channel == "control":
        if not peer_may_control(uid, mapping):
            _send_json(conn, _deny_payload("control principal not mapped"))
            return
        if op == "mint":
            session = str(request.get("session") or "").strip()
            task_id = str(request.get("task_id") or "").strip()
            if not session or not task_id:
                _send_json(conn, _deny_payload("mint requires session and task_id"))
                return
            handle = mint_broker_handle(session, task_id, request.get("started_at"))
            _send_json(conn, {"rc": 0, "handle": handle})
            return
        if op == "revoke":
            session = str(request.get("session") or "").strip()
            if not session:
                _send_json(conn, _deny_payload("revoke requires session"))
                return
            removed = revoke_broker_handles(session, str(request.get("task_id") or ""))
            _send_json(conn, {"rc": 0, "removed": removed})
            return
        _send_json(conn, _deny_payload("control socket accepts op=mint or op=revoke only"))
        return
    _send_json(conn, _deny_payload("unknown broker channel"))


def serve_broker(
    socket_path: str,
    *,
    control_socket_path: str = "",
    inner_gh: str,
    token: str,
    redis_client: Any = None,
    task_loader: Any = None,
    control_uids: Optional[set[int]] = None,
    worker_uids: Optional[set[int]] = None,
) -> None:
    exec_path = Path(socket_path)
    control_path = Path(control_socket_path or default_control_socket_path(socket_path))
    mapping = load_principal_map(control_uids=control_uids, worker_uids=worker_uids)
    exec_sock = _bind_unix_socket(
        exec_path, stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IWGRP
    )
    control_sock = _bind_unix_socket(control_path, stat.S_IRUSR | stat.S_IWUSR)
    os.chmod(str(control_path.parent), stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)
    try:
        while True:
            readable, _unused_w, _unused_x = select.select([exec_sock, control_sock], [], [])
            for sock in readable:
                conn, _unused_addr = sock.accept()
                channel = "control" if sock is control_sock else "exec"
                with conn:
                    _serve_one(
                        conn,
                        channel=channel,
                        inner_gh=inner_gh,
                        token=token,
                        redis_client=redis_client,
                        task_loader=task_loader,
                        mapping=mapping,
                    )
    finally:
        exec_sock.close()
        control_sock.close()


def call_broker(
    socket_path: str,
    argv: Optional[list[str]] = None,
    *,
    op: str = "exec",
    handle: str = "",
    claimed_session: str = "",
    session: str = "",
    task_id: str = "",
    started_at: Any = None,
) -> dict[str, Any]:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.connect(socket_path)
    except OSError as exc:
        raise GitHubBrokerClientError(
            f"github broker socket unreachable at {socket_path}: {exc}"
        ) from exc
    with sock:
        sock.sendall(
            json.dumps(
                {
                    "op": str(op or "exec"),
                    "argv": list(argv or []),
                    "handle": str(handle or ""),
                    "session": str(claimed_session or session or ""),
                    "task_id": str(task_id or ""),
                    "started_at": started_at,
                }
            ).encode("utf-8")
            + b"\n"
        )
        raw = b""
        while b"\n" not in raw:
            chunk = sock.recv(65536)
            if not chunk:
                break
            raw += chunk
    if not raw.strip():
        raise GitHubBrokerClientError("github broker returned empty response")
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        raise GitHubBrokerClientError("github broker returned non-object JSON")
    return payload


def deliver_handle_via_tmux(session: str, handle: str) -> None:
    """Put the handle in the worker tmux session env. Not Redis/current_task."""
    session_id = str(session or "").strip()
    value = str(handle or "").strip()
    if not session_id or not value:
        return
    for args in (
        ["tmux", "set-environment", "-t", session_id, "ORCH_OUTWARD_HANDLE", value],
        ["tmux", "set-environment", "-t", session_id, "ORCH_OUTWARD_SESSION", session_id],
    ):
        subprocess.run(args, capture_output=True, text=True, check=False, timeout=2)


def clear_handle_via_tmux(session: str) -> None:
    session_id = str(session or "").strip()
    if not session_id:
        return
    subprocess.run(
        ["tmux", "set-environment", "-t", session_id, "-u", "ORCH_OUTWARD_HANDLE"],
        capture_output=True,
        text=True,
        check=False,
        timeout=2,
    )


def control_socket_configured() -> str:
    return str(os.environ.get("ORCH_GITHUB_BROKER_CONTROL_SOCKET") or "").strip()


def mint_and_deliver_outward_handle(
    session: str,
    task_id: str,
    started_at: Any = None,
    *,
    outward_handle_out: Optional[dict[str, Any]] = None,
) -> str:
    """Control-channel mint + tmux delivery. No-op if control socket unset."""
    socket_path = control_socket_configured()
    if not socket_path:
        if outward_handle_out is not None:
            outward_handle_out["handle"] = ""
        return ""
    payload = call_broker(
        socket_path,
        op="mint",
        session=session,
        task_id=task_id,
        started_at=started_at,
    )
    handle = str(payload.get("handle") or "")
    rc = payload.get("rc")
    if (1 if rc is None else int(rc)) != 0 or not handle:
        raise GitHubBrokerClientError(f"control mint failed: {payload}")
    deliver_handle_via_tmux(session, handle)
    if outward_handle_out is not None:
        outward_handle_out["handle"] = handle
    return handle


def revoke_and_clear_outward_handle(session: str, task_id: str = "") -> int:
    """Control-channel revoke + tmux unset. No-op if control socket unset."""
    socket_path = control_socket_configured()
    if not socket_path:
        return 0
    clear_handle_via_tmux(session)
    payload = call_broker(
        socket_path,
        op="revoke",
        session=session,
        task_id=task_id,
    )
    rc = payload.get("rc")
    if (1 if rc is None else int(rc)) != 0:
        raise GitHubBrokerClientError(f"control revoke failed: {payload}")
    return int(payload.get("removed") or 0)


def install_github_broker(
    prefix: Path,
    *,
    inner_gh: Path,
    client_script: Path,
    token: str,
    allow_live: bool = False,
    python_executable: str = "",
) -> dict[str, str]:
    """Install worker client + broker-private inner gh. Never writes the token."""
    del token  # token stays in the broker process env; never land it on disk
    prefix = _resolved(prefix)
    inner_gh = _resolved(inner_gh)
    client_script = _resolved(client_script)
    if prefix_is_live(prefix) and not allow_live:
        raise GitHubBrokerInstallError(
            f"refusing live prefix {prefix}; isolated --prefix only (no deploy)"
        )
    if not inner_gh.is_file():
        raise GitHubBrokerInstallError(f"inner gh missing: {inner_gh}")
    if not client_script.is_file():
        raise GitHubBrokerInstallError(f"client script missing: {client_script}")

    worker_bin = prefix / "worker" / "bin"
    usr_bin = prefix / "worker" / "usr" / "bin"
    broker_dir = prefix / "broker"
    control_dir = broker_dir / "control"
    for directory in (worker_bin, usr_bin, broker_dir, control_dir):
        directory.mkdir(parents=True, exist_ok=True)
    os.chmod(str(control_dir), stat.S_IRUSR | stat.S_IWUSR | stat.S_IXUSR)

    real_gh = broker_dir / "gh-real"
    shutil.copy2(inner_gh, real_gh)
    real_gh.chmod(real_gh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    system_gh = usr_bin / "gh"
    shutil.copy2(inner_gh, system_gh)
    system_gh.chmod(system_gh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    socket_path = broker_dir / "github-broker.sock"
    control_socket = control_dir / "github-broker-control.sock"
    python = python_executable or os.environ.get("PYTHON", "") or shutil.which("python3") or "python3"
    orch_root = client_script.parent.parent
    broker_client = worker_bin / "gh"
    broker_client.write_text(
        "#!/bin/sh\n"
        f"export ORCH_GITHUB_BROKER_SOCKET={str(socket_path)!r}\n"
        f"export PYTHONPATH={str(orch_root)!r}${{PYTHONPATH:+:$PYTHONPATH}}\n"
        f"exec {python!r} {str(client_script)!r} \"$@\"\n",
        encoding="utf-8",
    )
    broker_client.chmod(broker_client.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    worker_env = prefix / "worker" / "env" / "worker-github.env"
    worker_env.parent.mkdir(parents=True, exist_ok=True)
    worker_env.write_text("".join(f"unset {key}\n" for key in WORKER_UNSET_KEYS), encoding="utf-8")

    return {
        "prefix": str(prefix),
        "worker_gh": str(broker_client),
        "system_gh": str(system_gh),
        "inner_gh": str(real_gh),
        "socket": str(socket_path),
        "control_socket": str(control_socket),
        "worker_env": str(worker_env),
        "broker_dir": str(broker_dir),
        "control_dir": str(control_dir),
        "worker_root": str(prefix / "worker"),
    }
