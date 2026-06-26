#!/usr/bin/env python3
"""
Create an OrchTask via the dashboard API.

Tasks persist in Neo4j and trigger a notification to the supervisor.
Use this instead of taey-notify when you have work for the supervisor.

Usage:
    taey-task create "Fix the broken save function in train_fsdp_v3.py"
    taey-task create "DEFECT: store_project_motifs missing vector write" --priority 80
    taey-task create "Enhancement: add retry logic to consultation pipeline" --from supervisor
    taey-task list                    # Show pending tasks
    taey-task status <task-id>        # Check a task's status
    taey-task dispatch <task-id> <peer>  # Claim/bind/wake peer work
    taey-task dispatch <task-id> <peer> --force  # Explicitly replace a peer's live current_task
    taey-task update <task-id> completed --evidence '{"commit_sha":"abc123","production_observation":"verified live"}'
    taey-task update <task-id> failed --evidence '{"reason":"blocked by missing dependency"}'
"""
import argparse
import json
import os
import subprocess
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet_orchestrator.cli_handoff_state import conflicting_binding, executor_bindings, format_binding
from fleet_orchestrator.cli_http import api_json_or_exit
from fleet_orchestrator.evidence_contract import TERMINAL_STATUSES

DASHBOARD_URL = os.environ.get("ORCH_DASHBOARD_URL", "http://localhost:5002")
UPDATE_STATUSES = tuple(sorted(TERMINAL_STATUSES | {"in_progress"}))


def detect_from_node():
    """Auto-detect sender identity from tmux session name."""
    explicit = os.environ.get("TAEY_NODE_ID")
    if explicit:
        return explicit
    try:
        result = subprocess.run(
            ["tmux", "display-message", "-p", "#S"],
            capture_output=True, text=True, timeout=2,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except Exception:
        pass
    import socket
    return socket.gethostname()


def api_call(method, endpoint, data=None):
    """Make an API call to the dashboard."""
    return api_json_or_exit(method, DASHBOARD_URL, endpoint, data=data, timeout=10)


def parse_evidence_arg(raw):
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"ERROR: --evidence must be valid JSON: {exc}", file=sys.stderr)
        sys.exit(1)
    if not isinstance(value, dict):
        print("ERROR: --evidence must decode to a JSON object", file=sys.stderr)
        sys.exit(1)
    return value


def cmd_create(args):
    """Create a new OrchTask."""
    sender = args.sender or detect_from_node()
    task_type = getattr(args, "type", "standard") or "standard"
    data = {
        "title": args.description,
        "description": args.description,
        "priority": args.priority,
        "from": sender,
        "task_type": task_type,
    }
    result = api_call("POST", "/api/task/create", data)
    task_id = result.get("task_id", "?")
    lane = "MICRO (fast-track)" if task_type == "micro" else "STANDARD (full DMAIC)"
    print(f"OK: task created [{task_id}] (pri:{args.priority}) [{lane}] from {sender}")
    print(f"    {args.description[:120]}")


def cmd_list(args):
    """List pending tasks."""
    result = api_call("GET", "/api/tasks/ranked")
    tasks = result.get("tasks", [])
    if not tasks:
        print("No pending tasks.")
        return
    print(f"{'ID':<25} {'Pri':>3} {'Description':<60}")
    print("-" * 90)
    for t in tasks[:20]:
        desc = t.get("description", "")[:58]
        print(f"{t['task_id']:<25} {t.get('priority',0):>3} {desc}")


def cmd_status(args):
    """Check a task's status."""
    result = api_call("GET", f"/api/tasks/{args.task_id}")
    if "error" in result:
        print(f"ERROR: {result['error']}", file=sys.stderr)
        sys.exit(1)
    t = result
    print(f"Task: {t.get('id', args.task_id)}")
    print(f"  Status: {t.get('status', '?')}")
    print(f"  Owner: {t.get('owner', 'unassigned')}")
    bindings = executor_bindings(t, api_call)
    if bindings:
        print("  Executor binding:")
        for binding in bindings:
            print(f"    - {format_binding(binding)}")
    else:
        print("  Executor binding: -")
    print(f"  Blocked on: {t.get('blocked_on') or '-'}")
    print(f"  Priority: {t.get('priority', '?')}")
    if t.get("completed_by"):
        print(f"  Completed by: {t.get('completed_by')}")
    if t.get("completed_at"):
        print(f"  Completed at: {t.get('completed_at')}")
    if t.get("completion_evidence"):
        print(f"  Completion evidence: {json.dumps(t.get('completion_evidence'), sort_keys=True)}")
    verification = t.get("completion_evidence_verification")
    if isinstance(verification, dict):
        status = verification.get("status") or ("VERIFIED" if verification.get("verified") else "UNVERIFIED")
        reason = verification.get("reason") or "-"
        print(f"  Completion evidence verification: {status} - {reason}")
    print(f"  Description: {t.get('description', '?')[:200]}")


def cmd_dispatch(args):
    """Dispatch an OrchTask to a peer through the canonical claim/bind/wake primitive."""
    sender = detect_from_node()
    task = api_call("GET", f"/api/tasks/{args.task_id}")
    if "error" in task:
        print(f"ERROR: {task['error']}", file=sys.stderr)
        sys.exit(1)

    task_id = str(task.get("id") or args.task_id)
    description = str(task.get("description") or task.get("title") or "").strip()
    if not description:
        print(f"ERROR: task {task_id} has no description to dispatch", file=sys.stderr)
        sys.exit(1)

    conflict = conflicting_binding(task, args.peer, api_call)
    if conflict and not args.force:
        print(
            f"ERROR: task {task_id} already has live executor binding "
            f"{format_binding(conflict)}; re-run `taey-task dispatch {task_id} {args.peer} --force` "
            "only if you intend to replace it.",
            file=sys.stderr,
        )
        sys.exit(1)
    if conflict and args.force:
        print(
            f"WARNING: replacing live executor binding {format_binding(conflict)}; "
            f"verify with `taey-task status {task_id}`.",
            file=sys.stderr,
        )

    from fleet_orchestrator.dispatch import (
        BugLockActive,
        HooksNotInstalled,
        OrchTaskNotReady,
        WorkerBusy,
        dispatch as dispatch_task,
    )

    try:
        dispatch_task(
            worker=args.peer,
            task_id=task_id,
            description=description,
            supervisor=sender,
            priority=args.priority,
            force=bool(args.force),
        )
    except (BugLockActive, HooksNotInstalled, OrchTaskNotReady, WorkerBusy, RuntimeError) as exc:
        print(
            f"ERROR: dispatch failed: {exc}. "
            f"Inspect with `taey-task status {task_id}` or retry `taey-task dispatch {task_id} {args.peer}`.",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"OK: dispatched {task_id} -> {args.peer} (by {sender})")


def cmd_update(args):
    """Update a task's status via PATCH /api/task/{id}."""
    sender = detect_from_node()
    if args.status in TERMINAL_STATUSES and args.evidence is None:
        print(f"ERROR: {args.status} status requires --evidence JSON", file=sys.stderr)
        sys.exit(1)
    if args.status not in TERMINAL_STATUSES and args.evidence is not None:
        print("ERROR: --evidence is only valid with terminal statuses", file=sys.stderr)
        sys.exit(1)
    data = {
        "status": args.status,
        "from": sender,
    }
    if args.clear_blocked_on:
        data["blocked_on"] = ""
    elif args.blocked_on is not None:
        data["blocked_on"] = args.blocked_on
    if args.evidence is not None:
        data["evidence"] = parse_evidence_arg(args.evidence)
    result = api_call("PATCH", f"/api/task/{args.task_id}", data)
    if result.get("ok"):
        blocked_on = result.get("blocked_on")
        suffix = f" blocked_on={blocked_on}" if blocked_on else ""
        print(f"OK: {args.task_id} → {args.status} (by {sender}){suffix}")
    else:
        print(f"ERROR: {result.get('error', 'unknown')}", file=sys.stderr)
        sys.exit(1)


def main():
    parser = argparse.ArgumentParser(
        description="Create and manage OrchTasks via the dashboard API"
    )
    sub = parser.add_subparsers(dest="command")

    p_create = sub.add_parser("create", help="Create a new task")
    p_create.add_argument("description", help="Task description")
    p_create.add_argument("--priority", type=int, default=50, help="Priority (0-100)")
    p_create.add_argument("--from", dest="sender", help="Sender identity")
    p_create.add_argument("--type", choices=["standard", "micro"], default="standard",
                          help="micro = trivial fix (<10 LOC), skips DMAIC MEASURE")

    p_list = sub.add_parser("list", help="List pending tasks")

    p_status = sub.add_parser("status", help="Check task status")
    p_status.add_argument("task_id", help="Task ID")

    p_dispatch = sub.add_parser("dispatch", help="Claim, bind, and wake a peer task")
    p_dispatch.add_argument("task_id", help="Task ID")
    p_dispatch.add_argument("peer", help="Peer session to dispatch to")
    p_dispatch.add_argument("--priority", default="normal",
                            help="Notify priority passed to taey-notify (default: normal)")
    p_dispatch.add_argument("--force", action="store_true",
                            help="Replace an existing live current_task binding for the peer")

    p_update = sub.add_parser("update", help="Update task status")
    p_update.add_argument("task_id", help="Task ID")
    p_update.add_argument("status", choices=UPDATE_STATUSES)
    p_update.add_argument("--blocked-on", help="Mark the in-progress task as waiting on an external signal")
    p_update.add_argument("--clear-blocked-on", action="store_true",
                          help="Clear the task's blocked_on marker")
    p_update.add_argument("--evidence",
                          help="JSON object with completion evidence, e.g. '{\"commit_sha\":\"abc123\",\"production_observation\":\"verified live\"}'")

    args = parser.parse_args()
    if args.command == "create":
        cmd_create(args)
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "dispatch":
        cmd_dispatch(args)
    elif args.command == "update":
        cmd_update(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
