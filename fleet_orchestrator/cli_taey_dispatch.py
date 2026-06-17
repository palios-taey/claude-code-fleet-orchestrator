#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fleet_orchestrator.out_of_band import (  # noqa: E402
    clear_out_of_band_task,
    heartbeat_out_of_band_task,
    notify_supervisor,
    patch_task_status,
    register_out_of_band_task,
)


def detect_node_id() -> str:
    explicit = os.environ.get("TAEY_NODE_ID")
    if explicit:
        return explicit
    try:
        result = subprocess.run(["tmux", "display-message", "-p", "#S"], capture_output=True, text=True, timeout=2)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    return socket.gethostname()


def parse_json_object(raw: str) -> dict:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"ERROR: JSON expected: {exc}")
    if not isinstance(value, dict):
        raise SystemExit("ERROR: JSON object expected")
    return value


def cmd_register(args: argparse.Namespace) -> int:
    supervisor = args.supervisor or detect_node_id()
    runner = args.runner or detect_node_id()
    owner = args.owner or runner
    payload = register_out_of_band_task(
        args.task_id,
        supervisor=supervisor,
        owner=owner,
        runner=runner,
        heartbeat_ttl_secs=args.ttl,
        details=args.details,
    )
    print(f"OK: registered out-of-band task {args.task_id} supervisor={supervisor} owner={owner} ttl={payload['heartbeat_ttl_secs']}s")
    return 0


def cmd_heartbeat(args: argparse.Namespace) -> int:
    payload = heartbeat_out_of_band_task(args.task_id)
    print(f"OK: heartbeat out-of-band task {args.task_id} heartbeat_at={payload['heartbeat_at']}")
    return 0


def cmd_complete(args: argparse.Namespace) -> int:
    sender = args.runner or detect_node_id()
    evidence = parse_json_object(args.evidence) if args.evidence else {"production_observation": args.summary}
    patch_task_status(args.task_id, args.status, evidence=evidence, sender=sender)
    clear_out_of_band_task(args.task_id)
    if args.supervisor:
        notify_supervisor(
            args.supervisor,
            f"OUT-OF-BAND COMPLETE [{args.task_id}]: status={args.status}; {args.summary}",
            sender=sender,
        )
    print(f"OK: completed out-of-band task {args.task_id} status={args.status}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="taey-dispatch")
    sub = parser.add_subparsers(dest="command", required=True)

    oob = sub.add_parser("out-of-band", help="Register and drive harness/subprocess task liveness")
    oob_sub = oob.add_subparsers(dest="out_of_band_command", required=True)

    register = oob_sub.add_parser("register", help="Register a task driven outside a peer session")
    register.add_argument("task_id")
    register.add_argument("--supervisor", help="Supervisor to wake on completion; defaults to this session")
    register.add_argument("--owner", help="Task owner/dispatched peer; defaults to runner")
    register.add_argument("--runner", help="Harness/session driving the subprocess; defaults to this session")
    register.add_argument("--ttl", type=int, default=300, help="Fresh heartbeat window in seconds")
    register.add_argument("--details", help="Short harness/process description")
    register.set_defaults(func=cmd_register)

    heartbeat = oob_sub.add_parser("heartbeat", help="Refresh an out-of-band task heartbeat")
    heartbeat.add_argument("task_id")
    heartbeat.set_defaults(func=cmd_heartbeat)

    complete = oob_sub.add_parser("complete", help="Finish an out-of-band task and wake the supervisor")
    complete.add_argument("task_id")
    complete.add_argument("--status", choices=["completed", "failed", "interrupted"], default="completed")
    complete.add_argument("--evidence", help="Terminal evidence JSON object")
    complete.add_argument("--summary", default="out-of-band harness completed")
    complete.add_argument("--supervisor", help="Supervisor to notify")
    complete.add_argument("--runner", help="Sender identity; defaults to this session")
    complete.set_defaults(func=cmd_complete)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
