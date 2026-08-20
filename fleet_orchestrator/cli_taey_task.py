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
    taey-task unbind <peer>              # Clear a stale peer current_task binding
    taey-task remove-dependency <task-id> <depends-on-task-id>
    taey-task update <task-id> completed --evidence '{"commit_sha":"abc123","repo":"OWNER/REPO","production_observation":"verified live"}'
    taey-task update <task-id> completed --evidence-file /tmp/evidence.json
    taey-task update <task-id> completed --evidence-observation "verified live"
    taey-task update <task-id> failed --evidence '{"reason":"blocked by missing dependency"}'
    taey-task update <task-id> changes_requested --reason "audit rejection reason"
    taey-task hold <task-id> AWAIT:external-signal:<detail>
    taey-task outcome done --details "ready for supervisor review"
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
CHANGES_REQUESTED_STATUS = "changes_requested"
UPDATE_STATUSES = tuple(sorted(TERMINAL_STATUSES | {"in_progress", CHANGES_REQUESTED_STATUS}))
OUTCOME_STATUSES = ("done", "error", "interrupted")
AWAIT_MARKER_HINT = "AWAIT:<human-review|family-consent|external-signal>:<detail>"


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


EVIDENCE_PARSE_HINT = (
    "Retry with `taey-task update <task-id> completed --evidence-file <path>` "
    "for JSON evidence, or `taey-task update <task-id> completed "
    "--evidence-observation '<plain text>'` for prose production_observation "
    "evidence with quotes/newlines."
)


def parse_evidence_arg(raw, *, source="--evidence"):
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(
            f"ERROR: {source} must be valid JSON at line {exc.lineno}, "
            f"column {exc.colno} (char {exc.pos}): {exc.msg}. {EVIDENCE_PARSE_HINT}",
            file=sys.stderr,
        )
        sys.exit(1)
    if not isinstance(value, dict):
        print(
            f"ERROR: {source} must decode to a JSON object. {EVIDENCE_PARSE_HINT}",
            file=sys.stderr,
        )
        sys.exit(1)
    return value


def parse_evidence_file_arg(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return parse_evidence_arg(handle.read(), source=f"--evidence-file {path}")
    except OSError as exc:
        print(
            f"ERROR: could not read --evidence-file {path}: {exc}. "
            "Inspect the path or retry with `taey-task update <task-id> completed "
            "--evidence-observation '<plain text>'`.",
            file=sys.stderr,
        )
        sys.exit(1)


def evidence_from_update_args(args):
    if args.evidence is not None:
        return parse_evidence_arg(args.evidence)
    if args.evidence_file is not None:
        return parse_evidence_file_arg(args.evidence_file)
    if args.evidence_observation is not None:
        return {"production_observation": args.evidence_observation}
    return None


def has_evidence_input(args):
    return (
        args.evidence is not None
        or args.evidence_file is not None
        or args.evidence_observation is not None
    )


def _validate_await_marker_or_exit(raw: str) -> str:
    marker = str(raw or "").strip()
    from fleet_orchestrator.orch_schema import is_declared_await_signal

    if not is_declared_await_signal(marker):
        print(
            f"ERROR: hold marker must match {AWAIT_MARKER_HINT}. "
            "Retry: taey-task hold <task-id> AWAIT:external-signal:<detail>",
            file=sys.stderr,
        )
        sys.exit(1)
    return marker


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

    api_call(
        "POST",
        f"/api/sessions/{args.peer}/notify",
        data={
            "dispatch": True,
            "task_id": task_id,
            "from": sender,
            "priority": args.priority,
            "force": bool(args.force),
        },
    )

    print(f"OK: dispatched {task_id} -> {args.peer} (by {sender})")


def cmd_update(args):
    """Update a task's status via PATCH /api/task/{id}."""
    sender = detect_from_node()
    changes_requested = args.status == CHANGES_REQUESTED_STATUS
    evidence_input_provided = has_evidence_input(args)
    if args.status in TERMINAL_STATUSES and not evidence_input_provided:
        print(
            f"ERROR: {args.status} status requires --evidence JSON, "
            "--evidence-file <path>, or --evidence-observation '<plain text>'. "
            f"Retry: taey-task update {args.task_id} completed "
            "--evidence-observation '<what changed>'",
            file=sys.stderr,
        )
        sys.exit(1)
    if args.status not in TERMINAL_STATUSES and evidence_input_provided:
        print(
            "ERROR: evidence flags are only valid with terminal statuses. "
            "Retry without --evidence/--evidence-file/--evidence-observation, "
            f"or use `taey-task update {args.task_id} completed --evidence-observation '<what changed>'`.",
            file=sys.stderr,
        )
        sys.exit(1)
    evidence = evidence_from_update_args(args)
    if changes_requested and not str(args.reason or "").strip():
        print(
            f"ERROR: changes_requested requires --reason '<audit rejection reason>'. "
            f"Retry: taey-task update {args.task_id} changes_requested --reason '<audit rejection reason>'",
            file=sys.stderr,
        )
        sys.exit(1)
    if changes_requested and (args.blocked_on is not None or args.clear_blocked_on):
        print(
            f"ERROR: --blocked-on is not valid with changes_requested. "
            f"Retry without --blocked-on: taey-task update {args.task_id} changes_requested "
            "--reason '<audit rejection reason>'",
            file=sys.stderr,
        )
        sys.exit(1)
    data = {
        "status": args.status,
        "from": sender,
    }
    if args.clear_blocked_on:
        data["blocked_on"] = ""
    elif args.blocked_on is not None:
        data["blocked_on"] = args.blocked_on
    if evidence is not None:
        data["evidence"] = evidence
    if changes_requested:
        data["record_outcome"] = CHANGES_REQUESTED_STATUS
        data["reason"] = args.reason.strip()
        if args.peer:
            data["peer"] = args.peer
    result = api_call("PATCH", f"/api/task/{args.task_id}", data)
    if result.get("ok"):
        if changes_requested:
            target = result.get("dispatched_to") or (result.get("changes_requested") or {}).get("worker") or "peer"
            print(f"OK: {args.task_id} → changes_requested; re-dispatched to {target} (by {sender})")
            return
        blocked_on = result.get("blocked_on")
        suffix = f" blocked_on={blocked_on}" if blocked_on else ""
        print(f"OK: {args.task_id} → {args.status} (by {sender}){suffix}")
    else:
        print(
            f"ERROR: {result.get('error', 'unknown')}. "
            f"Inspect with `taey-task status {args.task_id}` or retry the update command.",
            file=sys.stderr,
        )
        sys.exit(1)


def cmd_hold(args):
    """Set a structured AWAIT marker on the caller's own bound in-progress task."""
    sender = detect_from_node()
    marker = _validate_await_marker_or_exit(" ".join(args.blocked_on))
    data = {
        "status": "in_progress",
        "from": sender,
        "blocked_on": marker,
    }
    result = api_call("PATCH", f"/api/task/{args.task_id}", data)
    if result.get("ok"):
        print(f"OK: {args.task_id} -> in_progress blocked_on={result.get('blocked_on') or marker} (by {sender})")
        return
    print(
        f"ERROR: {result.get('error', 'unknown')}. "
        f"Inspect with `taey-task status {args.task_id}` or retry from the bound peer session.",
        file=sys.stderr,
    )
    sys.exit(1)


def cmd_outcome(args):
    """Record the caller's current_task outcome through the installed CLI."""
    sender = detect_from_node()
    from fleet_orchestrator.dispatch import record_outcome

    record_outcome(sender, args.outcome, args.details)
    print(f"OK: outcome recorded for {sender}: {args.outcome}")


def cmd_remove_dependency(args):
    """Remove a manual DEPENDS_ON edge through the API."""
    result = api_call("DELETE", f"/api/tasks/{args.task_id}/dependencies/{args.depends_on_id}")
    if result.get("ok"):
        print(f"OK: removed dependency {result.get('task_id')} -> {result.get('depends_on_id')}")
    else:
        print(f"ERROR: {result.get('error', 'unknown')}", file=sys.stderr)
        sys.exit(1)


def cmd_unbind(args):
    """Clear one session's current_task binding through the API.

    Optional --task-id is an explicit repair when Redis is already empty but a
    stale dispatched_to / liveness claim remains for this peer.
    """
    endpoint = f"/api/sessions/{args.peer}/current-task"
    task_id = str(getattr(args, "task_id", "") or "").strip()
    if task_id:
        endpoint = f"{endpoint}?task_id={task_id}"
    result = api_call("DELETE", endpoint)
    if result.get("ok"):
        unbound_task = result.get("task_id") or result.get("previous_task_id") or "?"
        status = result.get("status") or "pending"
        repair = " (repair)" if result.get("repair") else ""
        print(
            f"OK: unbound{repair} {args.peer} task={unbound_task} "
            f"status={status} dispatched_to={result.get('dispatched_to')}"
        )
    else:
        detail = result.get("detail")
        if isinstance(detail, dict):
            print(f"ERROR: {detail.get('error') or detail}", file=sys.stderr)
            if detail.get("next_step"):
                print(f"NEXT: {detail['next_step']}", file=sys.stderr)
        else:
            print(f"ERROR: {result.get('error') or detail or 'unknown'}", file=sys.stderr)
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

    p_status = sub.add_parser("status", aliases=["show"], help="Check task status (alias: show)")
    p_status.add_argument("task_id", help="Task ID")
    p_status.set_defaults(command="status")

    p_dispatch = sub.add_parser("dispatch", help="Claim, bind, and wake a peer task")
    p_dispatch.add_argument("task_id", help="Task ID")
    p_dispatch.add_argument("peer", help="Peer session to dispatch to")
    p_dispatch.add_argument("--priority", default="normal",
                            help="Notify priority passed to taey-notify (default: normal)")
    p_dispatch.add_argument("--force", action="store_true",
                            help="Replace an existing live current_task binding for the peer")

    p_remove_dependency = sub.add_parser("remove-dependency", help="Remove a manual task dependency edge")
    p_remove_dependency.add_argument("task_id", help="Task ID")
    p_remove_dependency.add_argument("depends_on_id", help="Task ID currently depended on")

    p_unbind = sub.add_parser("unbind", help="Clear a peer session's current_task binding")
    p_unbind.add_argument("peer", help="Peer session to unbind")
    p_unbind.add_argument(
        "--task-id",
        dest="task_id",
        help="Explicit repair when Redis is empty but this peer still holds dispatched_to/liveness",
    )

    p_update = sub.add_parser("update", help="Update task status")
    p_update.add_argument("task_id", help="Task ID")
    p_update.add_argument("status", choices=UPDATE_STATUSES)
    p_update.add_argument("--blocked-on", help="Mark the in-progress task as waiting on an external signal")
    p_update.add_argument("--clear-blocked-on", action="store_true",
                          help="Clear the task's blocked_on marker")
    evidence_group = p_update.add_mutually_exclusive_group()
    evidence_group.add_argument("--evidence",
                                help="JSON object with completion evidence, e.g. '{\"commit_sha\":\"abc123\",\"repo\":\"OWNER/REPO\",\"production_observation\":\"verified live\"}'")
    evidence_group.add_argument("--evidence-file",
                                help="Path to a JSON object with completion evidence; avoids shell quoting for prose evidence")
    evidence_group.add_argument("--evidence-observation",
                                help="Plain production_observation text; wrapped as {'production_observation': value}")
    p_update.add_argument("--reason", help="Audit rejection reason; required with changes_requested")
    p_update.add_argument("--peer", help="Explicit peer to re-dispatch when the task no longer records one")

    p_hold = sub.add_parser(
        "hold",
        help="Set a structured AWAIT marker on the caller's own bound task",
    )
    p_hold.add_argument("task_id", help="Task ID")
    p_hold.add_argument(
        "blocked_on",
        nargs="+",
        help=f"Structured marker, {AWAIT_MARKER_HINT}",
    )

    p_outcome = sub.add_parser(
        "outcome",
        help="Record the caller's current_task outcome without a Python import snippet",
    )
    p_outcome.add_argument("outcome", choices=OUTCOME_STATUSES)
    p_outcome.add_argument("--details", default="", help="Short outcome summary")

    args = parser.parse_args()
    if args.command == "create":
        cmd_create(args)
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "dispatch":
        cmd_dispatch(args)
    elif args.command == "remove-dependency":
        cmd_remove_dependency(args)
    elif args.command == "unbind":
        cmd_unbind(args)
    elif args.command == "update":
        cmd_update(args)
    elif args.command == "hold":
        cmd_hold(args)
    elif args.command == "outcome":
        cmd_outcome(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
