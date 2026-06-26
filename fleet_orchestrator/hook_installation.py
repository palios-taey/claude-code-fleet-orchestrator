"""Hook-installation checks shared by doctor and dispatch."""

from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


@dataclass(frozen=True)
class HookInstallationStatus:
    session_id: str
    cli: str
    settings_path: Path
    ok: bool
    detail: str
    missing: tuple[str, ...] = ()
    remediation: str = "install claude-code-fleet-notify hooks for this CLI"


CLI_HOOK_SPECS: dict[str, tuple[str, Path, dict[str, tuple[str, ...]]]] = {
    "claude": (
        "CLAUDE_SETTINGS_PATH",
        Path.home() / ".claude" / "settings.json",
        {
            "SessionStart": ("session_start.py",),
            "PreToolUse": ("pre_tool_activity.py",),
            "PostToolUse": ("check_notifications.py",),
            "Stop": ("stop_idle.py",),
            "UserPromptSubmit": ("prompt_activity.py",),
        },
    ),
    "codex": (
        "CODEX_HOOKS_PATH",
        Path.home() / ".codex" / "hooks.json",
        {
            "SessionStart": ("codex_session_start.py",),
            "PreToolUse": ("codex_pre_tool.py",),
            "PostToolUse": ("codex_post_tool.py",),
            "Stop": ("codex_stop.py",),
            "UserPromptSubmit": ("codex_user_prompt.py",),
        },
    ),
    "gemini": (
        "GEMINI_SETTINGS_PATH",
        Path.home() / ".gemini" / "settings.json",
        {
            "BeforeTool": ("gemini_before_tool.py",),
            "AfterTool": ("gemini_after_tool.py",),
            "BeforeAgent": ("gemini_before_agent.py",),
            "AfterAgent": ("gemini_after_agent.py",),
        },
    ),
    "grok": (
        "GROK_HOOKS_PATH",
        Path.home() / ".grok" / "hooks" / "cf-notify.json",
        {
            "SessionStart": ("grok_session_start.py",),
            "Stop": ("grok_stop.py",),
            "UserPromptSubmit": ("grok_user_prompt.py",),
        },
    ),
}


def cli_for_session(session_id: str) -> str:
    for suffix, cli in (
        ("-codex", "codex"),
        ("-gemini", "gemini"),
        ("-grok", "grok"),
        ("-claude", "claude"),
    ):
        if session_id.endswith(suffix):
            return cli
    return "claude"


def settings_path_for_cli(cli: str) -> Path:
    env_name, default_path, _events = CLI_HOOK_SPECS[cli]
    raw = os.environ.get(env_name)
    return Path(raw).expanduser() if raw else default_path


def _load_settings(path: Path) -> tuple[dict[str, Any] | None, str | None]:
    if not path.is_file():
        return None, f"settings file not found: {path}"
    try:
        parsed = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        return None, f"settings file is invalid JSON: {path}: {exc}"
    except OSError as exc:
        return None, f"settings file is unreadable: {path}: {exc}"
    if not isinstance(parsed, dict):
        return None, f"settings file root is not an object: {path}"
    return parsed, None


def _event_commands(settings: dict[str, Any], event: str) -> list[str]:
    hooks = settings.get("hooks")
    if not isinstance(hooks, dict):
        return []
    groups = hooks.get(event)
    if not isinstance(groups, list):
        return []
    commands: list[str] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        hook_rows = group.get("hooks")
        if not isinstance(hook_rows, list):
            continue
        for hook in hook_rows:
            if isinstance(hook, dict) and isinstance(hook.get("command"), str):
                commands.append(str(hook["command"]))
    return commands


def _command_script_names(command: str) -> set[str]:
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()
    names: set[str] = set()
    for token in tokens:
        if ".py" not in token:
            continue
        name = Path(token).name
        if name.endswith(".py"):
            names.add(name)
    return names


def _has_script(commands: Iterable[str], script_name: str) -> bool:
    return any(script_name in _command_script_names(command) for command in commands)


def hook_installation_status(session_id: str, *, cli: str | None = None) -> HookInstallationStatus:
    resolved_cli = cli or cli_for_session(session_id)
    if resolved_cli not in CLI_HOOK_SPECS:
        return HookInstallationStatus(
            session_id=session_id,
            cli=resolved_cli,
            settings_path=Path(""),
            ok=False,
            detail=f"unsupported CLI for hook installation check: {resolved_cli}",
            missing=("unsupported-cli",),
        )

    _env_name, _default_path, required = CLI_HOOK_SPECS[resolved_cli]
    settings_path = settings_path_for_cli(resolved_cli)
    settings, error = _load_settings(settings_path)
    if error:
        missing = tuple(f"{event}:{script}" for event, scripts in required.items() for script in scripts)
        return HookInstallationStatus(
            session_id=session_id,
            cli=resolved_cli,
            settings_path=settings_path,
            ok=False,
            detail=f"{session_id} ({resolved_cli}) has no installed hooks: {error}",
            missing=missing,
        )

    missing = []
    for event, scripts in required.items():
        commands = _event_commands(settings or {}, event)
        for script in scripts:
            if not _has_script(commands, script):
                missing.append(f"{event}:{script}")
    if missing:
        return HookInstallationStatus(
            session_id=session_id,
            cli=resolved_cli,
            settings_path=settings_path,
            ok=False,
            detail=(
                f"{session_id} ({resolved_cli}) is missing managed notify hooks "
                f"in {settings_path}: {', '.join(missing)}"
            ),
            missing=tuple(missing),
        )
    return HookInstallationStatus(
        session_id=session_id,
        cli=resolved_cli,
        settings_path=settings_path,
        ok=True,
        detail=f"{session_id} ({resolved_cli}) hook config present in {settings_path}",
    )
