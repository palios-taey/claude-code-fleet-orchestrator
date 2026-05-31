#!/usr/bin/env python3
"""
Create an OrchTask via the dashboard API.

Tasks persist in Neo4j and trigger a notification to the conductor.
Use this instead of taey-notify when you have work for the conductor.

Usage:
    taey-task create "Fix the broken save function in train_fsdp_v3.py"
    taey-task create "DEFECT: store_family_motifs missing Weaviate write" --priority 80
    taey-task create "Enhancement: add retry logic to consultation pipeline" --from weaver
    taey-task list                    # Show pending tasks
    taey-task status <task-id>        # Check a task's status
    taey-task update <task-id> completed --commit-sha <sha>   # Mark task done
"""
import argparse
import json
import os
import subprocess
import sys
import urllib.request
import urllib.error

DASHBOARD_URL = os.environ.get("ORCH_DASHBOARD_URL", "http://localhost:5002")


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
    url = f"{DASHBOARD_URL}{endpoint}"
    if data is not None:
        req = urllib.request.Request(
            url,
            data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json"},
            method=method,
        )
    else:
        req = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        body = e.read().decode()[:500]
        print(f"ERROR: HTTP {e.code}: {body}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)


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
    print(f"  Blocked on: {t.get('blocked_on') or '-'}")
    print(f"  Priority: {t.get('priority', '?')}")
    print(f"  Commit SHA: {t.get('closeout_commit_sha') or '-'}")
    print(f"  Production observation: {t.get('closeout_production_observation') or '-'}")
    print(f"  Evidence note: {t.get('closeout_evidence_note') or '-'}")
    print(f"  Description: {t.get('description', '?')[:200]}")


def cmd_update(args):
    """Update a task's status via PATCH /api/task/{id}."""
    sender = detect_from_node()
    data = {
        "status": args.status,
        "from": sender,
    }
    if args.clear_blocked_on:
        data["blocked_on"] = ""
    elif args.blocked_on is not None:
        data["blocked_on"] = args.blocked_on
    if args.result is not None:
        data["result"] = args.result
    if args.commit_sha is not None:
        data["commit_sha"] = args.commit_sha
    if args.production_observation is not None:
        data["production_observation"] = args.production_observation
    if args.note is not None:
        data["note"] = args.note
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

    p_update = sub.add_parser("update", help="Update task status")
    p_update.add_argument("task_id", help="Task ID")
    p_update.add_argument("status", choices=["completed", "failed", "in_progress", "interrupted"])
    p_update.add_argument("--result", help="One-line outcome summary")
    p_update.add_argument("--commit-sha", help="Git commit SHA for close-out evidence")
    p_update.add_argument("--production-observation", help="Observed production result for close-out evidence")
    p_update.add_argument("--note", help="Supplemental evidence note")
    p_update.add_argument("--blocked-on", help="Mark the in-progress task as waiting on an external signal")
    p_update.add_argument("--clear-blocked-on", action="store_true",
                          help="Clear the task's blocked_on marker")

    args = parser.parse_args()
    if args.command == "create":
        cmd_create(args)
    elif args.command == "list":
        cmd_list(args)
    elif args.command == "status":
        cmd_status(args)
    elif args.command == "update":
        cmd_update(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
