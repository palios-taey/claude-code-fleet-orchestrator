#!/usr/bin/env python3
"""Acceptance: dispatch and doctor surface hookless fleet sessions."""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fleet_orchestrator.test_isolation import assert_acceptance_redis_isolated  # noqa: E402

assert_acceptance_redis_isolated()
os.environ.setdefault("ORCH_NEO4J_URI", "bolt://127.0.0.1:7687")
os.environ.setdefault("ORCH_NEO4J_DB", "neo4j")
os.environ.setdefault("ORCH_DASHBOARD_URL", "http://127.0.0.1:5002")

import fleet_orchestrator.dispatch as dispatch_module  # noqa: E402
from fleet_orchestrator import easy_setup  # noqa: E402


FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _write_codex_hooks(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    hooks = {
        "SessionStart": "codex_session_start.py",
        "PreToolUse": "codex_pre_tool.py",
        "PostToolUse": "codex_post_tool.py",
        "Stop": "codex_stop.py",
        "UserPromptSubmit": "codex_user_prompt.py",
    }
    path.write_text(json.dumps({
        "hooks": {
            event: [{"hooks": [{"type": "command", "command": f"python3 /notify/hooks/{script}"}]}]
            for event, script in hooks.items()
        }
    }), encoding="utf-8")


def _with_env(values: dict[str, str | None]):
    original = {key: os.environ.get(key) for key in values}

    class EnvGuard:
        def __enter__(self):
            for key, value in values.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            return self

        def __exit__(self, *_exc):
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    return EnvGuard()


def _dispatch_refuses_hookless(tmp: Path) -> None:
    missing_settings = tmp / "missing-codex-hooks.json"
    with _with_env({"CODEX_HOOKS_PATH": str(missing_settings)}):
        with mock.patch.object(dispatch_module, "_claim_ready_orch_task") as claim, \
             mock.patch.object(dispatch_module, "bind_current_task") as bind, \
             mock.patch.object(dispatch_module, "subprocess") as subprocess_mod:
            refused = None
            try:
                dispatch_module.dispatch("hookless-codex", "task-1", "hookless dispatch")
            except dispatch_module.HooksNotInstalled as exc:
                refused = str(exc)
    _check("hookless dispatch raises HooksNotInstalled", refused is not None and "hookless-codex" in refused, refused)
    _check("hookless dispatch does not claim task", not claim.called, claim.call_args_list)
    _check("hookless dispatch does not bind current_task", not bind.called, bind.call_args_list)
    _check("hookless dispatch does not notify", not subprocess_mod.run.called, subprocess_mod.run.call_args_list)


def _doctor_warns_hookless(tmp: Path) -> None:
    missing_settings = tmp / "missing-doctor-codex-hooks.json"
    with _with_env({
        "ORCH_SESSION_IDS": "doctor-hookless-codex",
        "CODEX_HOOKS_PATH": str(missing_settings),
    }):
        result = easy_setup._doctor_session_hooks()
    _check("doctor returns WARN for hookless configured session", result.ok and result.detail.startswith("WARN:"), result)
    _check("doctor warning names hookless session", "doctor-hookless-codex" in result.detail, result.detail)


def _hooked_dispatch_allowed(tmp: Path) -> None:
    settings = tmp / "codex-hooks.json"
    _write_codex_hooks(settings)
    ok = SimpleNamespace(returncode=0, stdout="OK", stderr="")
    with _with_env({"CODEX_HOOKS_PATH": str(settings)}):
        with mock.patch.object(dispatch_module, "_redis_connect", return_value=SimpleNamespace(get=lambda _key: None)), \
             mock.patch.object(dispatch_module, "_resolve_product_id", return_value=None), \
             mock.patch.object(dispatch_module, "_claim_ready_orch_task") as claim, \
             mock.patch.object(dispatch_module, "bind_current_task", return_value=123.0) as bind, \
             mock.patch.object(dispatch_module, "mark_superseded_for_task"), \
             mock.patch.object(dispatch_module, "_assemble_dispatch_prompt", return_value=("PACKET", {"refs": {}, "rules": []})), \
             mock.patch.object(dispatch_module, "maybe_emit_decision_receipt"), \
             mock.patch.object(dispatch_module.subprocess, "run", return_value=ok) as notify_run, \
             mock.patch.object(dispatch_module, "OrchConfig", return_value=SimpleNamespace(notify_cli_path="taey-notify")):
            dispatch_module.dispatch("hooked-codex", "task-2", "hooked dispatch", supervisor="sup")
    _check("hooked dispatch claims task", claim.called, claim.call_args_list)
    _check("hooked dispatch binds current_task", bind.called, bind.call_args_list)
    _check("hooked dispatch notifies once", notify_run.call_count == 1, notify_run.call_args_list)


def main() -> int:
    with tempfile.TemporaryDirectory(prefix="hook-installation-") as raw:
        tmp = Path(raw)
        _dispatch_refuses_hookless(tmp)
        _doctor_warns_hookless(tmp)
        _hooked_dispatch_allowed(tmp)
    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)}: {FAILURES}")
        return 1
    print("\nPASS - hookless sessions are surfaced before silent dispatch.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
