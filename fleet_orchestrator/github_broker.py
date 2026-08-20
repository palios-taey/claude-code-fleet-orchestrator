"""GitHub credential broker — authorization at the credential boundary.

Workers must not retain a usable GH_TOKEN or a system ``gh`` that can mutate
GitHub after unbind. The broker:

- is the only ``gh`` on the worker PATH
- holds credentials in a 0600 file the worker env does not source
- fail-closes every non-classified-read argv through live outward capability
- execs the inner gh with broker credentials only after authorization

Install is prefix-scoped. Live system prefixes are refused so this module
cannot deploy by accident.
"""
from __future__ import annotations

import os
import shutil
import stat
from pathlib import Path
from typing import Optional

BROKER_CREDENTIAL_KEYS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_HOST", "GH_ENTERPRISE_TOKEN")
LIVE_PREFIXES = (
    Path("/usr"),
    Path("/usr/local"),
    Path("/bin"),
    Path("/home/mira/.local"),
)
WORKER_UNSET_KEYS = ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN")


class GitHubBrokerInstallError(RuntimeError):
    """Raised when a broker install would touch live paths or miss inputs."""


def _resolved(path: Path) -> Path:
    return path.expanduser().resolve()


def prefix_is_live(prefix: Path) -> bool:
    target = _resolved(prefix)
    for live in LIVE_PREFIXES:
        live_r = live.resolve()
        if target == live_r or live_r in target.parents or target in live_r.parents:
            # /usr/local/bin would be under /usr/local; refuse those trees.
            if target == live_r or live_r in target.parents:
                return True
    return False


def parse_credential_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        if key in BROKER_CREDENTIAL_KEYS:
            values[key] = value.strip().strip("'").strip('"')
    return values


def broker_exec_env(
    *,
    credential_path: Optional[str] = None,
    base_env: Optional[dict[str, str]] = None,
) -> dict[str, str]:
    """Env for inner gh: worker tokens stripped, broker file injected."""
    env = dict(base_env or os.environ)
    for key in WORKER_UNSET_KEYS:
        env.pop(key, None)
    path = str(credential_path or env.get("GH_BROKER_CREDENTIALS") or "").strip()
    if path:
        parsed = parse_credential_file(Path(path))
        env.update(parsed)
    return env


def write_credential_file(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"{key}={values[key]}\n" for key in BROKER_CREDENTIAL_KEYS if values.get(key)]
    path.write_text("".join(lines), encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR)


def write_worker_env_unset(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "".join(f"unset {key}\n" for key in WORKER_UNSET_KEYS)
    path.write_text(body, encoding="utf-8")
    path.chmod(stat.S_IRUSR | stat.S_IWUSR | stat.S_IRGRP | stat.S_IROTH)


def install_github_broker(
    prefix: Path,
    *,
    inner_gh: Path,
    broker_script: Path,
    token: str,
    allow_live: bool = False,
    python_executable: str = "",
) -> dict[str, str]:
    """Install broker into an isolated prefix. Refuses live system trees."""
    prefix = _resolved(prefix)
    inner_gh = _resolved(inner_gh)
    broker_script = _resolved(broker_script)
    if prefix_is_live(prefix) and not allow_live:
        raise GitHubBrokerInstallError(
            f"refusing live prefix {prefix}; isolated --prefix only (no deploy)"
        )
    if not inner_gh.is_file():
        raise GitHubBrokerInstallError(f"inner gh missing: {inner_gh}")
    if not broker_script.is_file():
        raise GitHubBrokerInstallError(f"broker script missing: {broker_script}")
    if not str(token or "").strip():
        raise GitHubBrokerInstallError("broker token is required")

    bindir = prefix / "bin"
    libexec = prefix / "libexec"
    etc = prefix / "etc"
    usr_bin = prefix / "usr" / "bin"
    for directory in (bindir, libexec, etc, usr_bin):
        directory.mkdir(parents=True, exist_ok=True)

    real_gh = libexec / "gh-real"
    shutil.copy2(inner_gh, real_gh)
    real_gh.chmod(real_gh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    # Simulate /usr/bin/gh remaining as the inner binary without broker creds.
    system_gh = usr_bin / "gh"
    shutil.copy2(real_gh, system_gh)
    system_gh.chmod(system_gh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    creds = etc / "github-broker.env"
    write_credential_file(creds, {"GH_TOKEN": str(token).strip()})
    worker_env = etc / "worker-github.env"
    write_worker_env_unset(worker_env)

    python = python_executable or os.environ.get("PYTHON", "") or shutil.which("python3") or "python3"
    orch_root = broker_script.parent.parent
    broker_dest = bindir / "gh"
    broker_dest.write_text(
        "#!/bin/sh\n"
        f"export GH_OUTWARD_INNER={str(real_gh)!r}\n"
        f"export GH_BROKER_CREDENTIALS={str(creds)!r}\n"
        f"export PYTHONPATH={str(orch_root)!r}${{PYTHONPATH:+:$PYTHONPATH}}\n"
        f"exec {python!r} {str(broker_script)!r} \"$@\"\n",
        encoding="utf-8",
    )
    broker_dest.chmod(broker_dest.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    return {
        "prefix": str(prefix),
        "broker_gh": str(broker_dest),
        "inner_gh": str(real_gh),
        "system_gh": str(system_gh),
        "credentials": str(creds),
        "worker_env": str(worker_env),
    }
