"""GitHub outward broker — separate credential principal over a Unix socket.

Workers never receive GH_TOKEN, never learn the inner ``gh`` path, and cannot
mutate GitHub except by sending argv to this broker. The broker process holds
the token in memory, fail-closes unknown/mutating argv through live
``authorize_outward_capability``, then execs inner gh with broker-only env.

Production shape (CONTROL deploy, not this PR): systemd ``User=github-broker``
with a 0600 EnvironmentFile the worker UID cannot read. Isolated tests prove
the worker namespace has no token and no inner binary even as the same UID.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import subprocess
from pathlib import Path
from typing import Any, Optional

from .current_task_binding import lookup_outward_handle
from .outward_capability import (
    OutwardAuthorizationError,
    github_argv_requires_outward_capability,
    require_github_argv_capability,
)

WORKER_UNSET_KEYS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN")
LIVE_PREFIXES = (
    Path("/usr"),
    Path("/usr/local"),
    Path("/bin"),
    Path("/home/mira/.local"),
)


class GitHubBrokerInstallError(RuntimeError):
    """Raised when a broker install would touch live paths or miss inputs."""


class GitHubBrokerClientError(RuntimeError):
    """Raised when the worker client cannot reach the broker socket."""


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve()


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
        binding = lookup_outward_handle(handle, redis_client=redis_client)
        if not binding:
            return {
                "rc": 1,
                "stdout": "",
                "stderr": "SAFETY DENY: missing or revoked outward possession handle\n",
            }
        bound_session = str(binding.get("session") or "").strip()
        if claimed_session and claimed_session != bound_session:
            return {
                "rc": 1,
                "stdout": "",
                "stderr": "SAFETY DENY: possession handle cannot be exchanged for another session\n",
            }
        try:
            require_github_argv_capability(
                argv,
                session_id=bound_session,
                redis_client=redis_client,
                task_loader=task_loader,
            )
        except OutwardAuthorizationError as exc:
            return {"rc": 1, "stdout": "", "stderr": f"SAFETY DENY: {exc}\n"}
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


def serve_broker(
    socket_path: str,
    *,
    inner_gh: str,
    token: str,
    redis_client: Any = None,
    task_loader: Any = None,
) -> None:
    path = Path(socket_path)
    if path.exists():
        path.unlink()
    path.parent.mkdir(parents=True, exist_ok=True)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.bind(str(path))
    os.chmod(str(path), stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IWGRP)
    sock.listen(16)
    while True:
        conn, _unused = sock.accept()
        with conn:
            raw = b""
            while b"\n" not in raw:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                raw += chunk
            if not raw.strip():
                continue
            try:
                request = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                conn.sendall(b'{"rc":1,"stdout":"","stderr":"SAFETY DENY: invalid broker request\\n"}\n')
                continue
            argv = request.get("argv") if isinstance(request, dict) else None
            if not isinstance(argv, list):
                conn.sendall(b'{"rc":1,"stdout":"","stderr":"SAFETY DENY: argv required\\n"}\n')
                continue
            payload = handle_broker_request(
                [str(item) for item in argv],
                handle=str(request.get("handle") or ""),
                claimed_session=str(request.get("session") or ""),
                inner_gh=inner_gh,
                token=token,
                redis_client=redis_client,
                task_loader=task_loader,
            )
            conn.sendall((json.dumps(payload) + "\n").encode("utf-8"))


def call_broker(
    socket_path: str,
    argv: list[str],
    *,
    handle: str = "",
    claimed_session: str = "",
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
                    "argv": list(argv),
                    "handle": str(handle or ""),
                    "session": str(claimed_session or ""),
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
    for directory in (worker_bin, usr_bin, broker_dir):
        directory.mkdir(parents=True, exist_ok=True)

    real_gh = broker_dir / "gh-real"
    shutil.copy2(inner_gh, real_gh)
    real_gh.chmod(real_gh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    system_gh = usr_bin / "gh"
    shutil.copy2(inner_gh, system_gh)
    system_gh.chmod(system_gh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    socket_path = broker_dir / "github-broker.sock"
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
        "worker_env": str(worker_env),
        "broker_dir": str(broker_dir),
        "worker_root": str(prefix / "worker"),
    }
