#!/usr/bin/env python3
from __future__ import annotations

import argparse
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fleet_orchestrator.easy_setup import (  # noqa: E402
    compose_managed,
    disable_services,
    docker_compose_down,
    enable_services,
    notify_script,
    preflight_restore_diff,
    print_doctor_results,
    reconcile_pending_hook_transaction,
    remove_claude_permission_guard,
    restore_claude_settings_backup,
    run_doctor,
    set_compose_managed,
)


def _probe_bind_error(host: str, port: int) -> OSError | None:
    family = socket.AF_INET6 if ":" in host and host != "0.0.0.0" else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as probe:
        try:
            probe.bind((host, port))
        except OSError as exc:
            return exc
    return None


def cmd_serve(_: argparse.Namespace) -> int:
    from fleet_orchestrator.easy_setup import api_host, api_port
    from fleet_orchestrator.feature_flags import chat_enabled

    host = api_host()
    port = api_port()
    bind_error = _probe_bind_error(host, port)
    if bind_error is not None:
        print(
            f"ERROR: cannot start orch serve - {host}:{port} is already in use. "
            "Stop the conflicting process or set ORCH_PORT to a free port, then re-run 'orch serve'.",
            file=sys.stderr,
        )
        return 1

    chat_on = chat_enabled()
    reach = "this machine only" if host == "127.0.0.1" else f"your network — open http://{host}:{port}/ui/ from any device on it"
    print(f"Orchestrator UI -> http://{host}:{port}/ui/  ({reach})")
    print(f"Chat box: {'enabled' if chat_on else 'off (ORCH_CHAT_ENABLED=0)'}")
    print("Foreground server; Ctrl-C to stop. For a persistent background service use `orch enable`.")
    return subprocess.run(
        [sys.executable, "-m", "uvicorn", "fleet_orchestrator.tasks_api:app", "--host", host, "--port", str(port)],
        cwd=str(ROOT),
    ).returncode


def cmd_doctor(args: argparse.Namespace) -> int:
    return print_doctor_results(run_doctor(), explain_scope=args.explain_scope)


def cmd_enable(_: argparse.Namespace) -> int:
    for line in enable_services():
        print(line)
    return 0


def cmd_disable(_: argparse.Namespace) -> int:
    for line in disable_services():
        print(line)
    return 0


def cmd_uninstall(args: argparse.Namespace) -> int:
    reconcile_pending_hook_transaction()
    for line in disable_services():
        print(line)
    try:
        subprocess.run([str(notify_script("start_notify_daemons.sh")), "stop"], check=False)
    except Exception as exc:
        print(f"notify-daemon-stop: {exc}")
    if compose_managed():
        try:
            docker_compose_down()
            set_compose_managed(False)
            print("compose: down")
        except Exception as exc:
            print(f"compose: {exc}")
    else:
        print("compose: skipped (BYO mode)")

    if args.restore_original_settings:
        diff = preflight_restore_diff()
        if diff:
            print(diff, end="")
        try:
            restored = restore_claude_settings_backup(allow_path_drift=args.allow_path_drift_restore)
        except Exception as exc:
            print(f"claude-settings: {exc}")
            return 1
        if restored is not None:
            print(f"claude-settings: restored pristine backup {restored}")
        else:
            print("claude-settings: no pristine backup recorded")
        return 0

    result = remove_claude_permission_guard(apply=True)
    if result["diff"]:
        print(result["diff"], end="")
    print("claude-settings: removed managed deny and hook deltas")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="orch")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="Run the dashboard/API in the foreground on ORCH_HOST:ORCH_PORT")
    serve.set_defaults(func=cmd_serve)

    doctor = sub.add_parser("doctor")
    doctor.add_argument("--explain-scope", action="store_true")
    doctor.set_defaults(func=cmd_doctor)

    enable = sub.add_parser("enable")
    enable.set_defaults(func=cmd_enable)

    disable = sub.add_parser("disable")
    disable.set_defaults(func=cmd_disable)

    uninstall = sub.add_parser("uninstall")
    uninstall.add_argument("--restore-original-settings", action="store_true")
    uninstall.add_argument("--allow-path-drift-restore", action="store_true")
    uninstall.set_defaults(func=cmd_uninstall)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
