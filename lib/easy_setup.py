"""Easy setup helpers for the standalone orchestrator install flow."""

from __future__ import annotations

import difflib
import json
import os
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from lib.config import OrchConfig, get_neo4j_driver

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


@dataclass
class CheckResult:
    label: str
    ok: bool
    detail: str
    remediation: Optional[str] = None


def ensure_runtime_dirs() -> None:
    for path in (STATE_DIR, RUNTIME_DIR, LOG_DIR):
        path.mkdir(parents=True, exist_ok=True)


def package_version() -> str:
    namespace: Dict[str, str] = {}
    version_path = REPO_ROOT / "fleet_orchestrator" / "version.py"
    exec(version_path.read_text(encoding="utf-8"), namespace)
    return str(namespace["__version__"])


def api_base() -> str:
    return os.environ.get("ORCH_API_BASE") or os.environ.get("ORCH_DASHBOARD_URL") or DEFAULT_API_BASE


def read_json_file(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def write_json_file(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_setup_state() -> Dict[str, Any]:
    state = read_json_file(SETUP_STATE_PATH, {})
    return state if isinstance(state, dict) else {}


def save_setup_state(state: Dict[str, Any]) -> None:
    write_json_file(SETUP_STATE_PATH, state)


def _timestamp() -> str:
    return time.strftime("%Y%m%d-%H%M%S")


def _managed_marker(settings: Dict[str, Any]) -> Dict[str, Any]:
    managed = settings.setdefault("_managedBy", {})
    if not isinstance(managed, dict):
        settings["_managedBy"] = {}
        managed = settings["_managedBy"]
    orchestrator = managed.setdefault(MANAGED_BY, {})
    if not isinstance(orchestrator, dict):
        managed[MANAGED_BY] = {}
        orchestrator = managed[MANAGED_BY]
    return orchestrator


def _normalize_settings(settings: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(settings, dict):
        raise ValueError("settings JSON root must be an object")
    permissions = settings.setdefault("permissions", {})
    if not isinstance(permissions, dict):
        raise ValueError("permissions must be an object")
    deny = permissions.setdefault("deny", [])
    if not isinstance(deny, list):
        raise ValueError("permissions.deny must be a list")
    return settings


def load_claude_settings(path: Path = CLAUDE_SETTINGS_PATH) -> tuple[Dict[str, Any], str]:
    if path.exists():
        original = path.read_text(encoding="utf-8")
        parsed = json.loads(original)
    else:
        original = "{\n}\n"
        parsed = {}
    return _normalize_settings(parsed), original


def _ensure_deny_entries(settings: Dict[str, Any]) -> bool:
    permissions = settings["permissions"]
    deny = permissions["deny"]
    changed = False
    for entry in MANAGED_DENIES:
        if entry not in deny:
            deny.append(entry)
            changed = True
    marker = _managed_marker(settings)
    existing = marker.get("permissions.deny")
    if existing != MANAGED_DENIES:
        marker["permissions.deny"] = list(MANAGED_DENIES)
        changed = True
    return changed


def _remove_deny_entries(settings: Dict[str, Any]) -> bool:
    changed = False
    permissions = settings.get("permissions")
    if isinstance(permissions, dict):
        deny = permissions.get("deny")
        if isinstance(deny, list):
            kept = [item for item in deny if item not in MANAGED_DENIES]
            if kept != deny:
                permissions["deny"] = kept
                changed = True
    managed = settings.get("_managedBy")
    if isinstance(managed, dict):
        marker = managed.get(MANAGED_BY)
        if isinstance(marker, dict) and "permissions.deny" in marker:
            del marker["permissions.deny"]
            changed = True
        if isinstance(marker, dict) and not marker:
            del managed[MANAGED_BY]
        if not managed:
            settings.pop("_managedBy", None)
    return changed


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


def snapshot_claude_settings(path: Path = CLAUDE_SETTINGS_PATH) -> Path:
    ensure_runtime_dirs()
    backup = STATE_DIR / f"{path.name}.pre-orch-install.{_timestamp()}.bak"
    if path.exists():
        shutil.copy2(path, backup)
    else:
        backup.write_text("{\n}\n", encoding="utf-8")
    state = load_setup_state()
    state["claude_settings_backup"] = str(backup)
    state["claude_settings_path"] = str(path)
    save_setup_state(state)
    return backup


def apply_claude_permission_guard(path: Path = CLAUDE_SETTINGS_PATH, *, apply: bool) -> Dict[str, Any]:
    settings, original = load_claude_settings(path)
    changed = _ensure_deny_entries(settings)
    updated = render_settings(settings)
    diff = unified_diff(original, updated, str(path))
    if apply and changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(updated, encoding="utf-8")
    return {"changed": changed, "diff": diff, "updated": updated}


def remove_claude_permission_guard(path: Path = CLAUDE_SETTINGS_PATH, *, apply: bool) -> Dict[str, Any]:
    settings, original = load_claude_settings(path)
    changed = _remove_deny_entries(settings)
    updated = render_settings(settings)
    diff = unified_diff(original, updated, str(path))
    if apply and changed:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(updated, encoding="utf-8")
    return {"changed": changed, "diff": diff, "updated": updated}


def restore_claude_settings_backup() -> Optional[Path]:
    state = load_setup_state()
    backup_raw = state.get("claude_settings_backup")
    path_raw = state.get("claude_settings_path")
    if not backup_raw or not path_raw:
        return None
    backup = Path(str(backup_raw))
    target = Path(str(path_raw))
    if not backup.exists():
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(backup, target)
    return backup


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


def docker_compose_up() -> None:
    subprocess.run(docker_compose_cmd() + ["-f", str(DOCKER_COMPOSE_FILE), "up", "-d"], check=True, cwd=str(REPO_ROOT))


def docker_compose_down() -> None:
    subprocess.run(docker_compose_cmd() + ["-f", str(DOCKER_COMPOSE_FILE), "down"], check=False, cwd=str(REPO_ROOT))


def write_pid(pid_path: Path, pid: int) -> None:
    ensure_runtime_dirs()
    pid_path.write_text(str(pid), encoding="utf-8")


def read_pid(pid_path: Path) -> Optional[int]:
    if not pid_path.exists():
        return None
    try:
        return int(pid_path.read_text(encoding="utf-8").strip())
    except Exception:
        return None


def pid_alive(pid: Optional[int]) -> bool:
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def stop_pidfile(pid_path: Path) -> bool:
    pid = read_pid(pid_path)
    if not pid_alive(pid):
        pid_path.unlink(missing_ok=True)
        return False
    os.kill(pid, 15)
    deadline = time.time() + 5
    while time.time() < deadline:
        if not pid_alive(pid):
            pid_path.unlink(missing_ok=True)
            return True
        time.sleep(0.1)
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


def _default_env() -> Dict[str, str]:
    env = os.environ.copy()
    env.setdefault("PYTHONPATH", str(REPO_ROOT))
    env.setdefault("ORCH_API_BASE", api_base())
    return env


def enable_services() -> List[str]:
    messages: List[str] = []
    env = _default_env()
    if pid_alive(read_pid(API_PID_PATH)):
        messages.append("api: already managed")
    elif port_open("127.0.0.1", 5002):
        messages.append("api: external listener detected on 127.0.0.1:5002")
    else:
        pid = _spawn_background(
            [sys.executable, "-m", "uvicorn", "lib.tasks_api:app", "--host", "127.0.0.1", "--port", "5002"],
            API_LOG_PATH,
            env=env,
        )
        write_pid(API_PID_PATH, pid)
        messages.append(f"api: started pid={pid}")
    if pid_alive(read_pid(WATCH_PID_PATH)):
        messages.append("watch: already managed")
    else:
        existing = subprocess.run(["pgrep", "-f", "scripts/orch-watch"], capture_output=True, text=True)
        if existing.returncode == 0 and existing.stdout.strip():
            messages.append("watch: external orch-watch detected")
        else:
            pid = _spawn_background(
                [
                    sys.executable,
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
            write_pid(WATCH_PID_PATH, pid)
            messages.append(f"watch: started pid={pid}")
    return messages


def disable_services() -> List[str]:
    messages = [
        f"api: {'stopped' if stop_pidfile(API_PID_PATH) else 'not-managed'}",
        f"watch: {'stopped' if stop_pidfile(WATCH_PID_PATH) else 'not-managed'}",
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
    try:
        cfg = OrchConfig()
    except Exception as exc:
        return CheckResult("env", False, f"config invalid: {exc}", "set required ORCH_* variables in .env")
    problems = []
    if not cfg.neo4j_uri.startswith("bolt://"):
        problems.append("ORCH_NEO4J_URI must start with bolt://")
    if not cfg.dashboard_url.startswith("http://") and not cfg.dashboard_url.startswith("https://"):
        problems.append("ORCH_DASHBOARD_URL must be http(s)")
    if cfg.redis_port <= 0:
        problems.append("ORCH_REDIS_PORT must be positive")
    if problems:
        return CheckResult("env", False, "; ".join(problems), "fix .env values")
    return CheckResult("env", True, "config values parse")


def _doctor_docker() -> CheckResult:
    try:
        require_command("docker")
    except Exception as exc:
        return CheckResult("docker", False, str(exc), "install Docker with compose support")
    if not docker_running():
        return CheckResult("docker", False, "docker daemon is not running", "start Docker or use external Redis/Neo4j")
    return CheckResult("docker", True, "docker present and daemon reachable")


def _doctor_infra() -> CheckResult:
    redis_ok = port_open("127.0.0.1", 6379)
    bolt_ok = port_open("127.0.0.1", 7687)
    if redis_ok and bolt_ok:
        return CheckResult("infra", True, "redis:6379 and neo4j:7687 reachable")
    missing = []
    if not redis_ok:
        missing.append("redis:6379")
    if not bolt_ok:
        missing.append("neo4j:7687")
    return CheckResult("infra", False, "missing " + ", ".join(missing), "run docker compose up or point .env at external infra")


def _doctor_health() -> CheckResult:
    url = f"{api_base().rstrip('/')}/health"
    try:
        payload = http_json(url)
    except Exception as exc:
        return CheckResult("health", False, f"{url} unreachable: {exc}", "run `orch enable` or start uvicorn on :5002")
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
        return CheckResult("claude-settings", False, f"deny entries not exactly-once: {counts}", "rerun scripts/install or `orch enable` guard step")
    return CheckResult("claude-settings", True, f"deny entries present exactly-once: {counts}")


def _doctor_claude_hooks() -> CheckResult:
    try:
        settings, _ = load_claude_settings(CLAUDE_SETTINGS_PATH)
    except Exception as exc:
        return CheckResult("claude-hooks", False, f"settings unreadable: {exc}", "repair ~/.claude/settings.json")
    expected = {
        "PreToolUse": "pre_tool_activity.py",
        "PostToolUse": "check_notifications.py",
        "Stop": "stop_idle.py",
        "UserPromptSubmit": "prompt_activity.py",
    }
    hooks = settings.get("hooks", {})
    failures = []
    for event, script_name in expected.items():
        groups = hooks.get(event, [])
        commands = []
        for group in groups if isinstance(groups, list) else []:
            if isinstance(group, dict):
                for hook in group.get("hooks", []):
                    if isinstance(hook, dict):
                        commands.append(str(hook.get("command", "")))
        count = sum(1 for command in commands if command.endswith(script_name))
        if count != 1:
            failures.append(f"{event}={count}")
    if failures:
        return CheckResult("claude-hooks", False, ", ".join(failures), "rerun notify install-hooks.sh --apply")
    return CheckResult("claude-hooks", True, "hook commands present exactly-once")


def _doctor_hook_fail_open() -> CheckResult:
    try:
        hook = resolve_notify_root() / "hooks" / "codex_stop.py"
    except Exception as exc:
        return CheckResult("hook-fail-open", False, str(exc), "set ORCH_NOTIFY_LIB_ROOT to the notify checkout")
    dead_port = 65530
    payload = json.dumps({"stop_hook_active": True})
    env = os.environ.copy()
    env["ORCH_API_BASE"] = f"http://127.0.0.1:{dead_port}"
    env.setdefault("TAEY_NODE_ID", "doctor-codex")
    proc = subprocess.run(
        [sys.executable, str(hook)],
        input=payload,
        text=True,
        capture_output=True,
        cwd=str(resolve_notify_root()),
        env=env,
    )
    if proc.returncode == 0 and proc.stdout.strip() == "{}" and proc.stderr.strip() == "":
        return CheckResult("hook-fail-open", True, "stop hook returns {} and exit 0 when API is down")
    return CheckResult(
        "hook-fail-open",
        False,
        f"rc={proc.returncode} stdout={proc.stdout.strip()} stderr={proc.stderr.strip()}",
        "fix notify hook fail-open behavior or ORCH_API_BASE wiring",
    )


def _doctor_stop_round_trip() -> CheckResult:
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
    managed_pid = read_pid(WATCH_PID_PATH)
    process = subprocess.run(["pgrep", "-f", "scripts/orch-watch"], capture_output=True, text=True)
    pids = [line.strip() for line in process.stdout.splitlines() if line.strip()]
    if len(pids) == 1:
        detail = f"pid={pids[0]}"
        if pid_alive(managed_pid):
            detail += " managed"
        return CheckResult("orch-watch", True, detail)
    if len(pids) == 0:
        return CheckResult("orch-watch", False, "orch-watch not running", "run `orch enable`")
    return CheckResult("orch-watch", False, f"multiple orch-watch processes: {', '.join(pids)}", "stop duplicates and keep one orch-watch instance")


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
        ("hook-fail-open", _doctor_hook_fail_open),
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


def create_temp_dotenv() -> Path:
    cfg = {}
    for key in (
        "ORCH_REDIS_HOST",
        "ORCH_REDIS_PORT",
        "ORCH_NEO4J_URI",
        "ORCH_NEO4J_USER",
        "ORCH_NEO4J_PASS",
        "ORCH_NEO4J_DB",
        "ORCH_DASHBOARD_URL",
        "ORCH_NOTIFY_LIB_ROOT",
        "ORCH_NOTIFY_CLI",
        "ORCH_API_BASE",
    ):
        value = os.environ.get(key)
        if value:
            cfg[key] = value
    handle = tempfile.NamedTemporaryFile("w", delete=False, suffix=".env")
    try:
        for key, value in cfg.items():
            handle.write(f"{key}={value}\n")
    finally:
        handle.flush()
        handle.close()
    return Path(handle.name)
