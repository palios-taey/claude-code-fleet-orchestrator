"""Easy setup helpers for the standalone orchestrator install flow."""

from __future__ import annotations

import difflib
import hashlib
import json
import os
import shlex
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional

REPO_ROOT = Path(__file__).resolve().parent.parent
MANAGED_BY = "claude-code-fleet-orchestrator"
MANAGED_DENIES = ["AskUserQuestion", "AskUserQuestion(*)"]
STATE_DIR = Path(os.environ.get("ORCH_STATE_DIR", Path.home() / ".local" / "state" / "fleet-orchestrator")).expanduser()
RUNTIME_DIR = STATE_DIR / "run"
LOG_DIR = STATE_DIR / "logs"
SETUP_STATE_PATH = STATE_DIR / "easy_setup_state.json"
CLAUDE_SETTINGS_PATH = Path(os.environ.get("CLAUDE_SETTINGS_PATH", Path.home() / ".claude" / "settings.json")).expanduser()
DEFAULT_API_BASE = os.environ.get("ORCH_API_BASE") or os.environ.get("ORCH_DASHBOARD_URL") or "http://127.0.0.1:5002"
DEFAULT_NOTIFY_DAEMON_PIDFILE = Path(os.environ.get("NOTIFY_DAEMON_PIDFILE", "/tmp/notify-daemons/daemon.pid")).expanduser()
API_PID_PATH = RUNTIME_DIR / "api.pid"
WATCH_PID_PATH = RUNTIME_DIR / "watch.pid"
API_LOG_PATH = LOG_DIR / "api.log"
WATCH_LOG_PATH = LOG_DIR / "watch.log"
DOCKER_COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
PIDENTITY_KEYS = ("pid", "starttime", "cwd", "cmdline")
DEFAULT_FILE_MODE = 0o600


@dataclass
class CheckResult:
    label: str
    ok: bool
    detail: str
    remediation: Optional[str] = None


def ensure_runtime_dirs() -> None:
    for path in (STATE_DIR, RUNTIME_DIR, LOG_DIR):
        path.mkdir(parents=True, exist_ok=True)


def _load_config_module():
    from lib.config import OrchConfig, get_neo4j_driver, get_redis_sync

    return OrchConfig, get_neo4j_driver, get_redis_sync


def package_version() -> str:
    namespace: Dict[str, str] = {}
    version_path = REPO_ROOT / "fleet_orchestrator" / "version.py"
    exec(version_path.read_text(encoding="utf-8"), namespace)
    return str(namespace["__version__"])


def api_base() -> str:
    return os.environ.get("ORCH_API_BASE") or os.environ.get("ORCH_DASHBOARD_URL") or DEFAULT_API_BASE


def _ensure_dotenv_loaded() -> None:
    # Make .env values (ORCH_HOST/ORCH_PORT/ORCH_CHAT_ENABLED) visible to this
    # process before we read them. Idempotent: the loader uses setdefault and
    # never overrides an already-set variable.
    from lib.config import _load_dotenv_candidates

    _load_dotenv_candidates()


def api_host() -> str:
    """Interface the dashboard binds to. Default 127.0.0.1 (this machine only).
    Any non-loopback ORCH_HOST is an explicit operator opt-in for a trusted
    single-user network; the bind is the mutable API's security boundary."""
    _ensure_dotenv_loaded()
    return (os.environ.get("ORCH_HOST") or "127.0.0.1").strip() or "127.0.0.1"


def api_port() -> int:
    _ensure_dotenv_loaded()
    raw = (os.environ.get("ORCH_PORT") or "5002").strip()
    try:
        return int(raw)
    except ValueError as exc:
        raise RuntimeError(f"ORCH_PORT must be an integer, got {raw!r}") from exc


def read_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _existing_mode(path: Path) -> int:
    if path.exists():
        return stat.S_IMODE(path.stat().st_mode)
    return DEFAULT_FILE_MODE


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = _existing_mode(path)
    handle = tempfile.NamedTemporaryFile("w", delete=False, dir=str(path.parent), prefix=f".{path.name}.", encoding="utf-8")
    temp_path = Path(handle.name)
    dir_fd = None
    try:
        handle.write(text)
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()
        os.chmod(temp_path, mode)
        os.replace(temp_path, path)
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        os.fsync(dir_fd)
        verify = path.read_text(encoding="utf-8")
        if verify != text:
            raise RuntimeError(f"atomic write verification failed for {path}")
    finally:
        if dir_fd is not None:
            os.close(dir_fd)
        try:
            handle.close()
        except Exception:
            pass
        if temp_path.exists():
            temp_path.unlink(missing_ok=True)


def atomic_write_json(path: Path, value: Any) -> None:
    atomic_write_text(path, json.dumps(value, indent=2, sort_keys=True) + "\n")


def load_setup_state() -> Dict[str, Any]:
    state = read_json_file(SETUP_STATE_PATH, {})
    return state if isinstance(state, dict) else {}


def save_setup_state(state: Dict[str, Any]) -> None:
    atomic_write_json(SETUP_STATE_PATH, state)


def update_setup_state(mutator: Callable[[Dict[str, Any]], None]) -> Dict[str, Any]:
    state = load_setup_state()
    mutator(state)
    save_setup_state(state)
    return state


def _timestamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def fingerprint_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def render_settings(settings: Dict[str, Any]) -> str:
    return json.dumps(settings, indent=2, sort_keys=False) + "\n"


def unified_diff(original: str, updated: str, label: str) -> str:
    return "".join(
        difflib.unified_diff(
            original.splitlines(keepends=True),
            updated.splitlines(keepends=True),
            fromfile=label,
            tofile=label,
        )
    )


def _normalize_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(settings, dict):
        raise ValueError("settings JSON root must be an object")
    permissions = settings.setdefault("permissions", {})
    if not isinstance(permissions, dict):
        raise ValueError("permissions must be an object")
    deny = permissions.setdefault("deny", [])
    if not isinstance(deny, list):
        raise ValueError("permissions.deny must be a list")
    hooks = settings.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("hooks must be an object")
    return settings


def load_claude_settings(path: Path = CLAUDE_SETTINGS_PATH) -> tuple[Dict[str, Any], str]:
    if path.exists():
        original = path.read_text(encoding="utf-8")
        parsed = json.loads(original)
    else:
        original = "{\n}\n"
        parsed = {}
    return _normalize_settings(parsed), original


def atomic_restore_settings_text(path: Path, text: str) -> None:
    atomic_write_text(path, text)


def _managed_marker(settings: Dict[str, Any]) -> Dict[str, Any]:
    managed = settings.setdefault("_managedBy", {})
    if not isinstance(managed, dict):
        settings["_managedBy"] = {}
        managed = settings["_managedBy"]
    owner = managed.setdefault(MANAGED_BY, {})
    if not isinstance(owner, dict):
        managed[MANAGED_BY] = {}
        owner = managed[MANAGED_BY]
    return owner


def _cleanup_managed_marker(settings: Dict[str, Any]) -> None:
    managed = settings.get("_managedBy")
    if not isinstance(managed, dict):
        return
    owner = managed.get(MANAGED_BY)
    if isinstance(owner, dict) and not owner:
        del managed[MANAGED_BY]
    if not managed:
        settings.pop("_managedBy", None)


def _expected_hook_scripts(notify_root: Path) -> Dict[str, Path]:
    hooks_root = notify_root / "hooks"
    return {
        "PreToolUse": (hooks_root / "pre_tool_activity.py").resolve(),
        "PostToolUse": (hooks_root / "check_notifications.py").resolve(),
        "Stop": (hooks_root / "stop_idle.py").resolve(),
        "UserPromptSubmit": (hooks_root / "prompt_activity.py").resolve(),
    }


def _extract_command_path(command: str) -> Optional[Path]:
    try:
        parts = shlex.split(command)
    except ValueError:
        return None
    for part in reversed(parts):
        if part.endswith(".py") or part.startswith("/") or part.startswith("."):
            return Path(part).expanduser().resolve()
    return None


def _hook_matches_expected(command_path: Optional[Path], expected_path: Path) -> bool:
    if command_path is None:
        return False
    return command_path == expected_path


def snapshot_expected_hook_commands(settings: Dict[str, Any], notify_root: Optional[Path] = None) -> Dict[str, List[str]]:
    root = notify_root or resolve_notify_root()
    expected = _expected_hook_scripts(root)
    hooks = settings.get("hooks", {})
    snapshot: Dict[str, List[str]] = {}
    for event, expected_path in expected.items():
        event_entries = hooks.get(event, [])
        commands: List[str] = []
        if not isinstance(event_entries, list):
            snapshot[event] = commands
            continue
        for group in event_entries:
            if not isinstance(group, dict):
                continue
            for hook in group.get("hooks", []):
                if not isinstance(hook, dict):
                    continue
                command = str(hook.get("command", ""))
                command_path = _extract_command_path(command)
                if _hook_matches_expected(command_path, expected_path):
                    commands.append(command)
        snapshot[event] = commands
    return snapshot


def _command_delta(before: Dict[str, List[str]], after: Dict[str, List[str]]) -> Dict[str, List[str]]:
    delta: Dict[str, List[str]] = {}
    for event, commands in after.items():
        before_paths = {str(_extract_command_path(command)) for command in before.get(event, []) if _extract_command_path(command) is not None}
        added = []
        for command in commands:
            parsed = _extract_command_path(command)
            path_key = str(parsed) if parsed is not None else command
            if path_key not in before_paths:
                added.append(command)
        if added:
            delta[event] = added
    return delta


def _dedupe_expected_hook_commands(settings: Dict[str, Any], notify_root: Optional[Path] = None) -> bool:
    root = notify_root or resolve_notify_root()
    expected = _expected_hook_scripts(root)
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        return False
    changed = False
    for event, expected_path in expected.items():
        event_entries = hooks.get(event)
        if not isinstance(event_entries, list):
            continue
        seen_expected = False
        new_groups = []
        for group in event_entries:
            if not isinstance(group, dict):
                new_groups.append(group)
                continue
            hook_rows = group.get("hooks", [])
            if not isinstance(hook_rows, list):
                new_groups.append(group)
                continue
            kept = []
            for hook in hook_rows:
                if not isinstance(hook, dict):
                    kept.append(hook)
                    continue
                command_path = _extract_command_path(str(hook.get("command", "")))
                if _hook_matches_expected(command_path, expected_path):
                    if seen_expected:
                        changed = True
                        continue
                    seen_expected = True
                kept.append(hook)
            if kept:
                new_group = dict(group)
                new_group["hooks"] = kept
                new_groups.append(new_group)
            else:
                changed = True
        hooks[event] = new_groups
    _cleanup_empty_settings_containers(settings)
    return changed


def _recorded_hook_commands(marker: Dict[str, Any]) -> Dict[str, List[str]]:
    raw = marker.get("hooks")
    if not isinstance(raw, dict):
        return {}
    commands = raw.get("commands_added")
    if not isinstance(commands, dict):
        return {}
    normalized: Dict[str, List[str]] = {}
    for event, entries in commands.items():
        if isinstance(entries, list):
            normalized[event] = [str(entry) for entry in entries]
    return normalized


def _merge_recorded_hook_commands(marker: Dict[str, Any], hook_commands_added: Dict[str, List[str]]) -> None:
    if not hook_commands_added:
        return
    hooks = marker.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        marker["hooks"] = {}
        hooks = marker["hooks"]
    existing = hooks.setdefault("commands_added", {})
    if not isinstance(existing, dict):
        hooks["commands_added"] = {}
        existing = hooks["commands_added"]
    for event, commands in hook_commands_added.items():
        current = [str(item) for item in existing.get(event, [])] if isinstance(existing.get(event), list) else []
        for command in commands:
            if command not in current:
                current.append(command)
        if current:
            existing[event] = current


def _remove_recorded_hook_commands(settings: Dict[str, Any], recorded: Dict[str, List[str]]) -> bool:
    if not recorded:
        return False
    hooks = settings.get("hooks", {})
    if not isinstance(hooks, dict):
        return False
    changed = False
    for event, commands_to_remove in recorded.items():
        event_entries = hooks.get(event)
        if not isinstance(event_entries, list):
            continue
        new_event_entries = []
        for group in event_entries:
            if not isinstance(group, dict):
                new_event_entries.append(group)
                continue
            hook_rows = group.get("hooks", [])
            if not isinstance(hook_rows, list):
                new_event_entries.append(group)
                continue
            kept_hooks = []
            for hook in hook_rows:
                if not isinstance(hook, dict):
                    kept_hooks.append(hook)
                    continue
                command = str(hook.get("command", ""))
                if command in commands_to_remove:
                    changed = True
                    continue
                kept_hooks.append(hook)
            if kept_hooks:
                new_group = dict(group)
                new_group["hooks"] = kept_hooks
                new_event_entries.append(new_group)
        if new_event_entries:
            hooks[event] = new_event_entries
        else:
            hooks.pop(event, None)
    return changed


def _cleanup_empty_settings_containers(settings: Dict[str, Any]) -> None:
    permissions = settings.get("permissions")
    if isinstance(permissions, dict) and not permissions:
        settings.pop("permissions", None)
    hooks = settings.get("hooks")
    if isinstance(hooks, dict) and not hooks:
        settings.pop("hooks", None)


def _managed_owner(settings: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    managed = settings.get("_managedBy")
    if not isinstance(managed, dict):
        return None
    owner = managed.get(MANAGED_BY)
    return owner if isinstance(owner, dict) else None


def snapshot_claude_settings(path: Path = CLAUDE_SETTINGS_PATH, *, original_text: Optional[str] = None) -> Path:
    ensure_runtime_dirs()
    if original_text is None:
        _, original_text = load_claude_settings(path)
    original_fingerprint = fingerprint_text(original_text)

    def _mutate(state: Dict[str, Any]) -> None:
        existing_path = state.get("claude_settings_backup")
        existing_target = state.get("claude_settings_path")
        existing_fingerprint = state.get("claude_settings_backup_fingerprint")
        if existing_path and existing_fingerprint:
            if not existing_target:
                state["claude_settings_path"] = str(path)
            return
        state["claude_settings_path"] = str(path)
        backup = STATE_DIR / f"{path.name}.pre-orch-install.{_timestamp()}.{uuid.uuid4().hex[:8]}.bak"
        atomic_write_text(backup, original_text)
        state["claude_settings_backup"] = str(backup)
        state["claude_settings_backup_fingerprint"] = original_fingerprint

    state = update_setup_state(_mutate)
    backup_path = Path(str(state["claude_settings_backup"]))
    return backup_path


def apply_claude_permission_guard(
    path: Path = CLAUDE_SETTINGS_PATH,
    *,
    apply: bool,
    hook_commands_added: Optional[Dict[str, List[str]]] = None,
) -> Dict[str, Any]:
    settings, original = load_claude_settings(path)
    _dedupe_expected_hook_commands(settings)
    permissions = settings["permissions"]
    deny = permissions["deny"]
    existing_owner = _managed_owner(settings)
    existing_added = existing_owner.get("permissions.deny_added") if isinstance(existing_owner, dict) else None
    recorded_added = [str(item) for item in existing_added] if isinstance(existing_added, list) else []

    newly_added: List[str] = []
    for entry in MANAGED_DENIES:
        if entry not in deny:
            deny.append(entry)
            newly_added.append(entry)
    for entry in newly_added:
        if entry not in recorded_added:
            recorded_added.append(entry)

    marker = existing_owner if isinstance(existing_owner, dict) else None
    hook_commands_added = hook_commands_added or {}
    if recorded_added or hook_commands_added or marker:
        marker = _managed_marker(settings)
        if recorded_added:
            marker["permissions.deny_added"] = recorded_added
        _merge_recorded_hook_commands(marker, hook_commands_added)
        if "permissions.deny_added" not in marker and not _recorded_hook_commands(marker):
            _cleanup_managed_marker(settings)

    updated = render_settings(settings)
    diff = unified_diff(original, updated, str(path))
    changed = updated != original
    if apply and changed:
        atomic_write_text(path, updated)
    return {
        "changed": changed,
        "diff": diff,
        "updated": updated,
        "deny_added": newly_added,
        "recorded_deny_added": recorded_added,
        "recorded_hook_commands": _recorded_hook_commands(marker or {}),
    }


def remove_claude_permission_guard(path: Path = CLAUDE_SETTINGS_PATH, *, apply: bool) -> Dict[str, Any]:
    if not path.exists():
        return {"changed": False, "diff": "", "updated": "{\n}\n"}
    original = path.read_text(encoding="utf-8")
    parsed = json.loads(original)
    if not isinstance(parsed, dict):
        raise ValueError("settings JSON root must be an object")
    marker = _managed_owner(parsed)
    if marker is None:
        return {"changed": False, "diff": "", "updated": original}

    settings = _normalize_settings(parsed)
    permissions = settings.get("permissions", {})
    deny = permissions.get("deny", [])
    changed = False

    deny_added = marker.get("permissions.deny_added")
    recorded_denies = [str(item) for item in deny_added] if isinstance(deny_added, list) else []
    if isinstance(deny, list) and recorded_denies:
        new_deny = []
        removed_budget = {entry: recorded_denies.count(entry) for entry in set(recorded_denies)}
        for item in deny:
            budget = removed_budget.get(item, 0)
            if budget > 0:
                removed_budget[item] = budget - 1
                changed = True
                continue
            new_deny.append(item)
        permissions["deny"] = new_deny

    recorded_hooks = _recorded_hook_commands(marker)
    if _remove_recorded_hook_commands(settings, recorded_hooks):
        changed = True

    marker.pop("permissions.deny_added", None)
    hooks_marker = marker.get("hooks")
    if isinstance(hooks_marker, dict):
        hooks_marker.pop("commands_added", None)
        if not hooks_marker:
            marker.pop("hooks", None)
    _cleanup_empty_settings_containers(settings)
    _cleanup_managed_marker(settings)

    updated = render_settings(settings)
    diff = unified_diff(original, updated, str(path))
    changed = changed or updated != original
    if apply and changed:
        atomic_write_text(path, updated)
    return {"changed": changed, "diff": diff, "updated": updated}


def restore_claude_settings_backup(*, allow_path_drift: bool = False) -> Optional[Path]:
    state = load_setup_state()
    backup_raw = state.get("claude_settings_backup")
    path_raw = state.get("claude_settings_path")
    if not backup_raw or not path_raw:
        return None
    backup = Path(str(backup_raw))
    target = CLAUDE_SETTINGS_PATH
    if not backup.exists():
        return None
    recorded_target = Path(str(path_raw))
    if target != recorded_target and not allow_path_drift:
        raise RuntimeError(
            f"refusing restore: current settings path {target} differs from recorded pristine path {recorded_target}"
        )
    expected_fingerprint = state.get("claude_settings_backup_fingerprint")
    backup_text = backup.read_text(encoding="utf-8")
    if expected_fingerprint and fingerprint_text(backup_text) != str(expected_fingerprint):
        raise RuntimeError("refusing restore: pristine backup fingerprint mismatch")
    atomic_write_text(target, backup_text)
    return backup


def preflight_restore_diff(path: Path = CLAUDE_SETTINGS_PATH) -> str:
    state = load_setup_state()
    backup_raw = state.get("claude_settings_backup")
    if not backup_raw:
        return ""
    backup = Path(str(backup_raw))
    if not backup.exists():
        return ""
    current = path.read_text(encoding="utf-8") if path.exists() else "{\n}\n"
    original = backup.read_text(encoding="utf-8")
    return unified_diff(current, original, str(path))


def resolve_notify_root() -> Path:
    explicit = os.environ.get("ORCH_NOTIFY_LIB_ROOT")
    if explicit:
        path = Path(explicit).expanduser().resolve()
        if path.is_dir():
            return path
        raise RuntimeError("ORCH_NOTIFY_LIB_ROOT does not point to a directory")
    for candidate in (
        REPO_ROOT.parent / "claude-code-fleet-notify",
        REPO_ROOT.parent.parent / "claude-code-fleet-notify",
        Path.home() / "claude-code-fleet-notify",
    ):
        if candidate.is_dir():
            return candidate.resolve()
    try:
        import identity  # type: ignore
    except ImportError as exc:
        raise RuntimeError("ORCH_NOTIFY_LIB_ROOT must be set when fleet-notify is not importable") from exc
    path = Path(identity.__file__).resolve().parent
    if path.is_dir():
        return path
    raise RuntimeError("unable to resolve fleet-notify root")


def notify_script(name: str) -> Path:
    root = resolve_notify_root()
    path = root / "scripts" / name
    if not path.is_file():
        raise RuntimeError(f"notify helper not found: {path}")
    return path


def http_json(url: str, *, method: str = "GET", data: Optional[Dict[str, Any]] = None, timeout: float = 5.0) -> Dict[str, Any]:
    payload = None
    headers = {}
    if data is not None:
        payload = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=payload, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def port_open(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.3)
        try:
            sock.connect((host, port))
            return True
        except OSError:
            return False


def require_command(name: str) -> str:
    found = shutil.which(name)
    if not found:
        raise RuntimeError(f"required command not found: {name}")
    return found


def docker_compose_cmd() -> List[str]:
    docker = require_command("docker")
    result = subprocess.run([docker, "compose", "version"], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError("docker compose is unavailable")
    return [docker, "compose"]


def docker_running() -> bool:
    docker = require_command("docker")
    result = subprocess.run([docker, "info"], capture_output=True, text=True)
    return result.returncode == 0


def _compose_health_ready() -> bool:
    result = subprocess.run(
        docker_compose_cmd() + ["-f", str(DOCKER_COMPOSE_FILE), "ps", "--format", "json"],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        check=False,
    )
    if result.returncode != 0:
        return False
    text = result.stdout.strip()
    if not text:
        return False
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        lines = [json.loads(line) for line in text.splitlines() if line.strip()]
        payload = lines
    rows = payload if isinstance(payload, list) else [payload]
    if len(rows) < 2:
        return False
    all_ready = True
    for row in rows:
        status_text = str(row.get("Health", "") or row.get("Status", ""))
        if "healthy" not in status_text.lower():
            all_ready = False
    return all_ready


def docker_compose_up() -> None:
    subprocess.run(docker_compose_cmd() + ["-f", str(DOCKER_COMPOSE_FILE), "up", "-d"], check=True, cwd=str(REPO_ROOT))
    deadline = time.time() + 60
    while time.time() < deadline:
        if _compose_health_ready():
            return
        time.sleep(1)
    raise RuntimeError("docker compose services did not become healthy within 60s")


def docker_compose_down() -> None:
    subprocess.run(docker_compose_cmd() + ["-f", str(DOCKER_COMPOSE_FILE), "down"], check=False, cwd=str(REPO_ROOT))


def _proc_identity(pid: int) -> Optional[Dict[str, Any]]:
    proc_dir = Path("/proc") / str(pid)
    if not proc_dir.exists():
        return None
    try:
        cmdline_raw = (proc_dir / "cmdline").read_bytes()
        cmdline = [part.decode("utf-8") for part in cmdline_raw.split(b"\x00") if part]
        cwd = os.readlink(proc_dir / "cwd")
        stat_fields = (proc_dir / "stat").read_text(encoding="utf-8").split()
        starttime = stat_fields[21]
    except Exception:
        return None
    return {"pid": pid, "cmdline": cmdline, "cwd": cwd, "starttime": str(starttime)}


def write_pid_record(pid_path: Path, pid: int) -> None:
    ensure_runtime_dirs()
    identity = _proc_identity(pid)
    if identity is None:
        raise RuntimeError(f"unable to capture process identity for pid {pid}")
    atomic_write_json(pid_path, identity)


def read_pid_record(pid_path: Path) -> Optional[Dict[str, Any]]:
    data = read_json_file(pid_path, None)
    return data if isinstance(data, dict) else None


def pid_alive(pid: Optional[int]) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _identity_matches(expected: Dict[str, Any], actual: Optional[Dict[str, Any]], *, suffix: Optional[str] = None) -> bool:
    if not isinstance(actual, dict):
        return False
    for key in PIDENTITY_KEYS:
        if key not in expected or key not in actual:
            return False
    if str(expected["pid"]) != str(actual["pid"]):
        return False
    if str(expected["starttime"]) != str(actual["starttime"]):
        return False
    if str(expected["cwd"]) != str(actual["cwd"]):
        return False
    if suffix is not None and not any(str(item).endswith(suffix) for item in actual.get("cmdline", [])):
        return False
    return True


def stop_pidfile(pid_path: Path, *, suffix: Optional[str] = None) -> bool:
    record = read_pid_record(pid_path)
    if not isinstance(record, dict):
        pid_path.unlink(missing_ok=True)
        return False
    pid = int(record.get("pid", 0))
    actual = _proc_identity(pid)
    if not _identity_matches(record, actual, suffix=suffix):
        pid_path.unlink(missing_ok=True)
        return False
    os.kill(pid, 15)
    deadline = time.time() + 5
    while time.time() < deadline:
        if _proc_identity(pid) is None:
            pid_path.unlink(missing_ok=True)
            return True
        time.sleep(0.1)
    actual = _proc_identity(pid)
    if _identity_matches(record, actual, suffix=suffix):
        os.kill(pid, 9)
    pid_path.unlink(missing_ok=True)
    return True


def _spawn_background(args: List[str], log_path: Path, env: Optional[Dict[str, str]] = None, cwd: Optional[Path] = None) -> int:
    ensure_runtime_dirs()
    with log_path.open("a", encoding="utf-8") as handle:
        proc = subprocess.Popen(
            args,
            cwd=str(cwd or REPO_ROOT),
            env=env,
            stdout=handle,
            stderr=handle,
            start_new_session=True,
        )
    return int(proc.pid)


def venv_python_path() -> Path:
    return REPO_ROOT / ".venv" / "bin" / "python"


def managed_python(required: bool = True) -> str:
    venv_python = venv_python_path()
    if venv_python.is_file():
        return str(venv_python)
    if required:
        raise RuntimeError("managed venv is missing; run scripts/install first")
    return sys.executable


def _default_env() -> Dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONPATH", str(REPO_ROOT))
    env.setdefault("ORCH_API_BASE", api_base())
    return env


def detect_local_infra_ports() -> Dict[str, bool]:
    return {
        "redis": port_open("127.0.0.1", 6379),
        "neo4j": port_open("127.0.0.1", 7687),
    }


def set_compose_managed(value: bool) -> None:
    def _mutate(state: Dict[str, Any]) -> None:
        state["compose_managed"] = bool(value)

    update_setup_state(_mutate)


def compose_managed() -> bool:
    return bool(load_setup_state().get("compose_managed", False))


def _pending_hook_journal(before_hooks: Dict[str, List[str]]) -> Dict[str, Any]:
    return {
        "notify_root": str(resolve_notify_root()),
        "before_hooks": before_hooks,
        "created_at": time.time(),
    }


def _write_pending_hook_transaction(before_hooks: Dict[str, List[str]]) -> None:
    def _mutate(state: Dict[str, Any]) -> None:
        state["pending_hook_transaction"] = _pending_hook_journal(before_hooks)

    update_setup_state(_mutate)


def _clear_pending_hook_transaction() -> None:
    def _mutate(state: Dict[str, Any]) -> None:
        state.pop("pending_hook_transaction", None)

    update_setup_state(_mutate)


def reconcile_pending_hook_transaction(path: Path = CLAUDE_SETTINGS_PATH) -> Dict[str, Any]:
    state = load_setup_state()
    pending = state.get("pending_hook_transaction")
    if not isinstance(pending, dict):
        return {"reconciled": False}
    settings, original = load_claude_settings(path)
    root_raw = pending.get("notify_root")
    notify_root = Path(str(root_raw if root_raw else resolve_notify_root())).resolve()
    before_hooks = pending.get("before_hooks", {})
    before_hooks = before_hooks if isinstance(before_hooks, dict) else {}
    after_hooks = snapshot_expected_hook_commands(settings, notify_root=notify_root)
    added = _command_delta(before_hooks, after_hooks)
    changed = False
    if added:
        marker = _managed_marker(settings)
        _merge_recorded_hook_commands(marker, added)
        changed = render_settings(settings) != original
    if changed:
        atomic_write_text(path, render_settings(settings))
    _clear_pending_hook_transaction()
    return {"reconciled": True, "hook_commands_added": added, "changed": changed}


def ensure_claude_integration(*, dry_run: bool = False) -> Dict[str, Any]:
    settings, original = load_claude_settings(CLAUDE_SETTINGS_PATH)
    if dry_run:
        guard = apply_claude_permission_guard(CLAUDE_SETTINGS_PATH, apply=False)
        return {"backup": None, "guard": guard, "hook_commands_added": {}, "reconciled": {"reconciled": False}}

    reconciled = reconcile_pending_hook_transaction(CLAUDE_SETTINGS_PATH)
    if reconciled.get("reconciled"):
        settings, original = load_claude_settings(CLAUDE_SETTINGS_PATH)
    backup_path = snapshot_claude_settings(CLAUDE_SETTINGS_PATH, original_text=original)
    pre_hooks = snapshot_expected_hook_commands(settings)
    hook_commands_added: Dict[str, List[str]] = {}

    try:
        _write_pending_hook_transaction(pre_hooks)
        # The notify starter launches its daemon with bare `python3`. On a fresh
        # machine the daemon's deps (redis) exist ONLY in this repo's venv, so the
        # daemon dies on import within a second and doctor correctly reports it
        # not-running (stranger-install gate, first red run 2026-06-11). Prefix
        # the venv bin so `python3` inside the starter resolves to an interpreter
        # that can actually run the daemon. No-op when the venv is absent.
        notify_env = os.environ.copy()
        venv_bin = venv_python_path().parent
        if venv_bin.is_dir():
            notify_env["PATH"] = f"{venv_bin}{os.pathsep}{notify_env.get('PATH', '')}"
        subprocess.run([str(notify_script("install-hooks.sh")), "--apply"], check=True, cwd=str(resolve_notify_root()), env=notify_env)
        after_notify, _ = load_claude_settings(CLAUDE_SETTINGS_PATH)
        _dedupe_expected_hook_commands(after_notify)
        hook_commands_added = _command_delta(pre_hooks, snapshot_expected_hook_commands(after_notify))
        guard = apply_claude_permission_guard(CLAUDE_SETTINGS_PATH, apply=True, hook_commands_added=hook_commands_added)
        _clear_pending_hook_transaction()
        return {"backup": backup_path, "guard": guard, "hook_commands_added": hook_commands_added, "reconciled": reconciled}
    except Exception:
        atomic_restore_settings_text(CLAUDE_SETTINGS_PATH, original)
        raise


def enable_services() -> List[str]:
    messages: List[str] = []
    integration = ensure_claude_integration(dry_run=False)
    if integration["guard"]["changed"]:
        messages.append("claude-settings: reconciled managed delta")
    env = _default_env()
    python_exec = managed_python(required=True)

    host = api_host()
    port = api_port()
    chat_on = (os.environ.get("ORCH_CHAT_ENABLED", "").strip().lower() in ("1", "true", "yes"))
    api_record = read_pid_record(API_PID_PATH)
    if isinstance(api_record, dict) and _identity_matches(api_record, _proc_identity(int(api_record["pid"])), suffix="uvicorn"):
        messages.append("api: already managed")
    elif port_open("127.0.0.1", port):
        messages.append(f"api: external listener detected on port {port}")
    else:
        pid = _spawn_background(
            [python_exec, "-m", "uvicorn", "lib.tasks_api:app", "--host", host, "--port", str(port)],
            API_LOG_PATH,
            env=env,
        )
        write_pid_record(API_PID_PATH, pid)
        reach = "this machine only" if host == "127.0.0.1" else f"reachable on your network via {host}"
        messages.append(f"api: started pid={pid} on {host}:{port} ({reach})")
        messages.append(f"api: chat box {'ENABLED' if chat_on else 'off (set ORCH_CHAT_ENABLED=1 on a trusted network)'}")

    watch_record = read_pid_record(WATCH_PID_PATH)
    if isinstance(watch_record, dict) and _identity_matches(watch_record, _proc_identity(int(watch_record["pid"])), suffix="scripts/orch-watch"):
        messages.append("watch: already managed")
    else:
        pid = _spawn_background(
            [
                python_exec,
                str(REPO_ROOT / "scripts" / "orch-watch"),
                "--redis-host",
                os.environ.get("ORCH_REDIS_HOST", "127.0.0.1"),
                "--redis-port",
                os.environ.get("ORCH_REDIS_PORT", "6379"),
                "--readiness-checker",
                "lib.plan_readiness:check_readiness",
            ],
            WATCH_LOG_PATH,
            env=env,
        )
        write_pid_record(WATCH_PID_PATH, pid)
        messages.append(f"watch: started pid={pid}")
    return messages


def disable_services() -> List[str]:
    messages = [
        f"api: {'stopped' if stop_pidfile(API_PID_PATH, suffix='uvicorn') else 'not-managed'}",
        f"watch: {'stopped' if stop_pidfile(WATCH_PID_PATH, suffix='scripts/orch-watch') else 'not-managed'}",
    ]
    return messages


def compose_scope() -> Dict[str, Any]:
    return {
        "ports": ["127.0.0.1:6379", "127.0.0.1:7687", "127.0.0.1:7474"],
        "volumes": ["orch_redis_data", "orch_neo4j_data"],
        "files": [str(DOCKER_COMPOSE_FILE), str(CLAUDE_SETTINGS_PATH), str(SETUP_STATE_PATH)],
        "hooks": ["Stop", "PreToolUse", "PostToolUse", "UserPromptSubmit"],
    }


def _doctor_env_validation() -> CheckResult:
    OrchConfig, _, _ = _load_config_module()
    try:
        cfg = OrchConfig()
    except Exception as exc:
        return CheckResult("env", False, f"config invalid: {exc}", "set required ORCH_* variables in .env")
    problems = []
    if not cfg.neo4j_uri.startswith("bolt://"):
        problems.append("ORCH_NEO4J_URI must start with bolt://")
    if not cfg.dashboard_url.startswith(("http://", "https://")):
        problems.append("ORCH_DASHBOARD_URL must be http(s)")
    if cfg.redis_port <= 0:
        problems.append("ORCH_REDIS_PORT must be positive")
    if problems:
        return CheckResult("env", False, "; ".join(problems), "fix .env values")
    return CheckResult("env", True, "config values parse")


def _redis_ping(cfg: Any) -> None:
    _, _, get_redis_sync = _load_config_module()
    client = get_redis_sync(cfg)
    if not client.ping():
        raise RuntimeError("Redis PING returned false")


def _neo4j_probe(cfg: Any) -> None:
    _, get_neo4j_driver, _ = _load_config_module()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        session.run("RETURN 1 AS ok").single()


def _doctor_docker() -> CheckResult:
    if not compose_managed():
        return CheckResult("docker", True, "skipped: BYO infra mode")
    try:
        require_command("docker")
    except Exception as exc:
        return CheckResult("docker", False, str(exc), "install Docker with compose support")
    if not docker_running():
        return CheckResult("docker", False, "docker daemon is not running", "start Docker")
    return CheckResult("docker", True, "docker present and daemon reachable")


def _doctor_infra() -> CheckResult:
    OrchConfig, _, _ = _load_config_module()
    try:
        cfg = OrchConfig()
        _redis_ping(cfg)
        _neo4j_probe(cfg)
        return CheckResult("infra", True, f"redis={cfg.redis_host}:{cfg.redis_port} neo4j={cfg.neo4j_uri}")
    except Exception as exc:
        return CheckResult("infra", False, str(exc), "ensure configured Redis and Neo4j endpoints are reachable")


def _doctor_health() -> CheckResult:
    url = f"{api_base().rstrip('/')}/health"
    # Doctor runs seconds after install spawns the API; a freshly started uvicorn
    # needs a few seconds to bind, so a single instant probe loses the startup race
    # (stranger-install gate, first red run 2026-06-11: 'Connection refused' on an
    # API that came up fine moments later). Retry within a bounded window before
    # declaring failure; a genuinely-down API still fails loudly after ~20s.
    deadline = time.monotonic() + 20
    while True:
        try:
            payload = http_json(url)
            break
        except Exception as exc:
            if time.monotonic() >= deadline:
                return CheckResult("health", False, f"{url} unreachable after 20s: {exc}", "run `orch enable` or start uvicorn on :5002")
            time.sleep(1)
    expected = package_version()
    actual = payload.get("version")
    if not payload.get("ok"):
        return CheckResult("health", False, f"/health returned not-ok: {payload}", "fix API startup failure and retry")
    if actual != expected:
        return CheckResult("health", False, f"version mismatch health={actual} package={expected}", "reinstall the current checkout and restart the API")
    return CheckResult("health", True, f"service={payload.get('service')} version={actual}")


def _doctor_claude_settings() -> CheckResult:
    try:
        settings, _ = load_claude_settings(CLAUDE_SETTINGS_PATH)
    except Exception as exc:
        return CheckResult("claude-settings", False, f"invalid JSON: {exc}", "repair ~/.claude/settings.json")
    deny = settings.get("permissions", {}).get("deny", [])
    counts = {entry: deny.count(entry) for entry in MANAGED_DENIES}
    if any(count != 1 for count in counts.values()):
        return CheckResult("claude-settings", False, f"deny entries not exactly-once: {counts}", "run `orch enable` to reconcile managed settings")
    return CheckResult("claude-settings", True, f"deny entries present exactly-once: {counts}")


def _doctor_claude_hooks() -> CheckResult:
    try:
        settings, _ = load_claude_settings(CLAUDE_SETTINGS_PATH)
        notify_root = resolve_notify_root()
    except Exception as exc:
        return CheckResult("claude-hooks", False, f"settings unreadable: {exc}", "repair ~/.claude/settings.json or ORCH_NOTIFY_LIB_ROOT")
    expected = _expected_hook_scripts(notify_root)
    hooks = settings.get("hooks", {})
    failures = []
    for event, expected_path in expected.items():
        groups = hooks.get(event, [])
        count = 0
        for group in groups if isinstance(groups, list) else []:
            if not isinstance(group, dict):
                continue
            for hook in group.get("hooks", []):
                if not isinstance(hook, dict):
                    continue
                command_path = _extract_command_path(str(hook.get("command", "")))
                if command_path == expected_path:
                    count += 1
        if count != 1:
            failures.append(f"{event}={count}")
    if failures:
        return CheckResult("claude-hooks", False, ", ".join(failures), "run `orch enable` to reconcile managed hooks")
    return CheckResult("claude-hooks", True, "hook commands present exactly-once at expected paths")


def _run_fail_open_hook(hook_name: str) -> tuple[int, str, str]:
    payload = json.dumps({"stop_hook_active": True})
    env = os.environ.copy()
    env["ORCH_API_BASE"] = "http://127.0.0.1:65530"
    env.setdefault("TAEY_NODE_ID", "doctor-codex")
    proc = subprocess.run(
        [sys.executable, str(resolve_notify_root() / "hooks" / hook_name)],
        input=payload,
        text=True,
        capture_output=True,
        cwd=str(resolve_notify_root()),
        env=env,
    )
    return proc.returncode, proc.stdout.strip(), proc.stderr.strip()


def _doctor_notify_hook_fail_open() -> CheckResult:
    rc, out, err = _run_fail_open_hook("codex_stop.py")
    if rc == 0 and out == "{}" and err == "":
        return CheckResult("notify-hook-fail-open", True, "codex_stop.py returns {} and exit 0 when API is down")
    return CheckResult("notify-hook-fail-open", False, f"rc={rc} stdout={out} stderr={err}", "fix notify codex stop hook fail-open behavior")


def _doctor_orch_hook_fail_open() -> CheckResult:
    rc, out, err = _run_fail_open_hook("stop_idle.py")
    if rc == 0 and out == "{}" and err == "":
        return CheckResult("orch-hook-fail-open", True, "stop_idle.py returns {} and exit 0 when API is down")
    return CheckResult("orch-hook-fail-open", False, f"rc={rc} stdout={out} stderr={err}", "fix notify claude stop hook fail-open behavior")


def _doctor_stop_round_trip() -> CheckResult:
    OrchConfig, get_neo4j_driver, _ = _load_config_module()
    cfg = OrchConfig()
    project_id = f"doctor-{uuid.uuid4().hex[:8]}"
    phase_id = f"{project_id}-phase"
    task_id = f"{project_id}-task"
    driver = get_neo4j_driver(cfg)
    try:
        with driver.session(database=cfg.neo4j_db) as session:
            session.run(
                """
                CREATE (p:OrchProject {id: $project_id, name: 'Doctor Project', supervisor: 'doctor', status: 'active'})
                CREATE (ph:OrchPhase {id: $phase_id, project_id: $project_id, name: 'Main', order: 1, status: 'active'})
                CREATE (t:OrchTask {id: $task_id, phase_id: $phase_id, owner: 'doctor-codex', description: 'doctor task', status: 'pending', priority: 1})
                CREATE (p)-[:HAS_PHASE]->(ph)
                CREATE (ph)-[:HAS_TASK]->(t)
                """,
                project_id=project_id,
                phase_id=phase_id,
                task_id=task_id,
            )
        decision = http_json(
            f"{api_base().rstrip('/')}/api/sessions/doctor-codex/stop-decision?stop_hook_active=true",
            timeout=8,
        )
        next_task_id = None
        next_task = decision.get("next")
        if isinstance(next_task, dict):
            next_task_id = next_task.get("task_id")
        if next_task_id is None:
            next_task_id = decision.get("task_id")
        if bool(decision.get("block")) and next_task_id == task_id:
            return CheckResult("stop-round-trip", True, f"decision=block task={task_id}")
        return CheckResult("stop-round-trip", False, f"unexpected payload: {decision}", "inspect stop-decision API and live data wiring")
    except Exception as exc:
        return CheckResult("stop-round-trip", False, str(exc), "ensure API and Neo4j are reachable")
    finally:
        with driver.session(database=cfg.neo4j_db) as session:
            session.run("MATCH (t:OrchTask {id: $task_id}) DETACH DELETE t", task_id=task_id)
            session.run("MATCH (ph:OrchPhase {id: $phase_id}) DETACH DELETE ph", phase_id=phase_id)
            session.run("MATCH (p:OrchProject {id: $project_id}) DETACH DELETE p", project_id=project_id)


def _doctor_notify_daemon() -> CheckResult:
    pid = None
    if DEFAULT_NOTIFY_DAEMON_PIDFILE.exists():
        try:
            pid = int(DEFAULT_NOTIFY_DAEMON_PIDFILE.read_text(encoding="utf-8").strip())
        except Exception:
            pid = None
    if pid_alive(pid):
        return CheckResult("notify-daemon", True, f"pid={pid}")
    return CheckResult("notify-daemon", False, "notify daemon not running", "run notify start_notify_daemons.sh start")


def _doctor_orch_watch() -> CheckResult:
    record = read_pid_record(WATCH_PID_PATH)
    if isinstance(record, dict) and _identity_matches(record, _proc_identity(int(record["pid"])), suffix="scripts/orch-watch"):
        return CheckResult("orch-watch", True, f"pid={record['pid']} managed")
    return CheckResult("orch-watch", False, "orch-watch not running under managed pidfile", "run `orch enable`")


def run_doctor() -> List[CheckResult]:
    checks = [
        ("docker", _doctor_docker),
        ("infra", _doctor_infra),
        ("env", _doctor_env_validation),
        ("health", _doctor_health),
        ("claude-settings", _doctor_claude_settings),
        ("claude-hooks", _doctor_claude_hooks),
        ("stop-round-trip", _doctor_stop_round_trip),
        ("notify-daemon", _doctor_notify_daemon),
        ("orch-watch", _doctor_orch_watch),
        ("notify-hook-fail-open", _doctor_notify_hook_fail_open),
        ("orch-hook-fail-open", _doctor_orch_hook_fail_open),
    ]
    results: List[CheckResult] = []
    for label, fn in checks:
        try:
            results.append(fn())
        except Exception as exc:
            results.append(CheckResult(label, False, f"unexpected exception: {exc}", "inspect the doctor implementation"))
    return results


def print_doctor_results(results: Iterable[CheckResult], *, explain_scope: bool) -> int:
    if explain_scope:
        print(json.dumps(compose_scope(), indent=2, sort_keys=True))
        print("")
    failures = 0
    for result in results:
        icon = "✅" if result.ok else "❌"
        print(f"{icon} {result.label}: {result.detail}")
        if not result.ok:
            failures += 1
            if result.remediation:
                print(f"   remediation: {result.remediation}")
    return 1 if failures else 0
