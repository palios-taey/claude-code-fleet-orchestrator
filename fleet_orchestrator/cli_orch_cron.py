#!/usr/bin/env python3
"""orch-cron — recurring task runner with file-tracked state.

Reads an operator-controlled JSON registry of recurring triggers. A trigger
can wake a session with a prompt file, dispatch an OrchTask by id, dispatch the
next ready task in a project, or run an operator-authored command directly on
the cadence.
The optional ``state_file`` turns each recurring fire into a first-class
"process tracked through files" — the supervisor can grep the state_file to
see the task's full history without context-switching into the worker pane.

JSON registry (path passed via ``--registry``)::

    {
      "_doc": "Recurring task registry — orch-cron fires due entries.",
      "triggers": [
        {
          "id": "x-claude-cycle",            // unique identifier
          "session": "x-claude",             // target tmux session
          "tz": "America/New_York",          // schedule timezone
          "minute": 9,                       // wall-clock minute
          "hours": [8, 10, 12, 14, 16, 18, 20, 22],
          "prompt_file": "/path/to/wake-prompt.txt", // prompt wake mode
          "state_file": "/path/to/x.jsonl",  // OPTIONAL — append fire records here
          "enabled": true,
          "note": "freeform"
        },
        {
          "id": "upwork-proposal-cycle",
          "session": "treasurer",
          "supervisor": "treasurer",
          "task_id": "recurring-context::upwork-proposal-cycle", // OrchTask mode
          "description": "Run the Upwork proposal recurring cycle",
          "tz": "America/New_York",
          "minute": 39,
          "hours": [8, 10, 12, 14, 16, 18, 20, 22],
          "state_file": "/path/to/upwork-cycle.jsonl",
          "enabled": true
        },
        {
          "id": "linkedin-plan-cycle",
          "session": "treasurer",
          "supervisor": "treasurer",
          "project": "linkedin-plan",        // Project next-ready dispatch mode; no reset
          "description": "Run the LinkedIn recurring plan",
          "tz": "America/New_York",
          "minute": 39,
          "hours": [8, 10, 12, 14, 16, 18, 20, 22],
          "state_file": "/path/to/linkedin-cycle.jsonl",
          "enabled": true
        },
        {
          "id": "careers-act-advance",
          "command": "cd /path/to/repo && .venv/bin/python scripts/loop/cycle.py",
          "cwd": "/path/to/repo",            // optional subprocess cwd
          "timeout_sec": 600,                // optional; default 600, always enforced
          "tz": "America/New_York",
          "minute": 59,
          "hours": [8, 10, 12, 14, 16, 18, 20, 22],
          "state_file": "/path/to/careers-act.jsonl",
          "enabled": true
        }
      ]
    }

Trigger modes are mutually exclusive. A trigger with more than one of
``command``, ``task_id``, ``project``, or ``prompt_file`` is skipped as ambiguous
instead of using silent precedence. Commands are trusted operator registry input
and may use shell syntax; never interpolate non-registry data into command
strings.

State file format (jsonl, one record per fire)::

    {"ts": 1779800000, "fire_id": "x-claude-cycle-20260526-2009",
     "session": "x-claude", "tz_hour_minute": "20:09",
     "prompt_hash": "sha256:..."  /* of prompt_file at fire time */,
     "result": "dispatched" | "skipped:already_fired" | "skipped:disabled" }

    {"ts": 1779800000, "fire_id": "careers-act-20260526-2059",
     "trigger_mode": "command", "command": "cd /repo && ./run.sh",
     "cwd": "/repo", "timeout_sec": 600, "exit_code": 0,
     "stdout": "...", "stderr": "...", "duration_sec": 1.234,
     "result": "command:exit_0" | "command:timeout" | "command:error" }

    {"ts": 1779800000, "fire_id": "linkedin-plan-20260526-2039",
     "trigger_mode": "project", "project": "linkedin-plan",
     "task_id": "linkedin-plan::step-0", "session": "treasurer",
     "result": "dispatched" }

Operators (e.g., the WAKE_PROMPT.txt-driven posting loop) can grep the
state_file for fires of a given type, count fires per day, replay a
specific fire's prompt by hash lookup, etc.

Run from system cron (every minute, exact-minute match) OR from a
peer-respawn DAEMONS entry (long-running with internal 1-min loop)::

    # crontab -e
    * * * * * /usr/local/bin/orch-cron --registry /etc/orch/recurring.json

    # or peer-respawn (using --watch)
    orch-cron --registry /etc/orch/recurring.json --watch

Fires via the released ``taey-notify`` CLI so the message routes through
the canonical fleet-notify inbox + daemon path.

DST: schedules are wall-clock in each trigger's ``tz``. Hours inside
the fall-back-ambiguous window (01:00-02:59 in US zones on DST end)
WILL fire twice. Recommend keeping schedules outside that window.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime
from typing import Any, Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # py<3.9
    from backports.zoneinfo import ZoneInfo  # type: ignore

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from fleet_orchestrator.config import OrchConfig, get_neo4j_driver

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [orch-cron] %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# Dedup-via-Redis so two cron ticks within the same minute don't double-fire.
# 120s TTL guards only the same-minute window; an hour later the slot is fresh.
DEDUP_TTL_SEC = 120
NOTIFY_KEY_PREFIX = os.environ.get("NOTIFY_KEY_PREFIX", "taey")
COMMAND_TIMEOUT_DEFAULT_SEC = 600.0
COMMAND_OUTPUT_LIMIT = 4000
DEFAULT_WEEKDAYS = (1, 2, 3, 4, 5, 6, 7)
_BAD_WEEKDAY_WARNING_KEYS: set[str] = set()


def orch_key(namespace: str, *parts: str) -> str:
    return ":".join([NOTIFY_KEY_PREFIX, namespace, *[str(part) for part in parts]])


def load_registry(path: str) -> dict:
    if not os.path.isfile(path):
        raise SystemExit(f"registry not found: {path}")
    with open(path) as f:
        return json.load(f)


def _trigger_id(trig: dict) -> str:
    return str(
        trig.get("id")
        or trig.get("session")
        or trig.get("task_id")
        or trig.get("project")
        or "?"
    )


def _trigger_weekdays(trig: dict) -> tuple[int, ...]:
    if "weekdays" not in trig:
        return DEFAULT_WEEKDAYS

    raw = trig.get("weekdays")
    if (
        isinstance(raw, list)
        and raw
        and all(type(day) is int and 1 <= day <= 7 for day in raw)
    ):
        return tuple(raw)

    trig_id = _trigger_id(trig)
    warning_key = f"{trig_id}:{repr(raw)}"
    if warning_key not in _BAD_WEEKDAY_WARNING_KEYS:
        log.warning(
            "bad weekdays in %s: expected non-empty list of ISO weekdays 1..7; treating as absent",
            trig_id,
        )
        _BAD_WEEKDAY_WARNING_KEYS.add(warning_key)
    return DEFAULT_WEEKDAYS


def should_fire(trig: dict, now_local: datetime) -> bool:
    """Exact-minute match within a listed hour and optional ISO weekday."""
    return (
        now_local.hour in trig.get("hours", [])
        and now_local.minute == trig.get("minute")
        and now_local.isoweekday() in _trigger_weekdays(trig)
    )


def _dedup_fire(redis_client: Any, fire_id: str) -> bool:
    dedup_key = orch_key("orch-cron-fired", fire_id)
    if redis_client is None:
        return True
    if redis_client.exists(dedup_key):
        return False
    redis_client.set(dedup_key, "1", ex=DEDUP_TTL_SEC)
    return True


def _clear_dedup(redis_client: Any, fire_id: str) -> None:
    if redis_client is None:
        return
    try:
        redis_client.delete(orch_key("orch-cron-fired", fire_id))
    except Exception:
        pass


def _trigger_route_fields(trig: dict) -> list[str]:
    return [field for field in ("command", "task_id", "project", "prompt_file") if field in trig]


def _truncate_output(value: Any, limit: int = COMMAND_OUTPUT_LIMIT) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        text = value.decode(errors="replace")
    else:
        text = str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + f"...[truncated {len(text) - limit} chars]"


def _command_timeout_sec(trig: dict) -> float:
    raw = trig.get("timeout_sec", COMMAND_TIMEOUT_DEFAULT_SEC)
    timeout = float(raw)
    if timeout <= 0:
        raise ValueError("timeout_sec must be > 0")
    return timeout


def _command_args(command: Any) -> tuple[Any, bool]:
    if isinstance(command, list):
        return [str(part) for part in command], False
    return str(command), True


def _task_prompt_body(trig: dict, task_id: str, description: str) -> str:
    body = str(trig.get("prompt_body") or "").strip()
    if body:
        return body
    return (
        f"DISPATCH {task_id} — recurring cadence fire from orch-cron. "
        f"{description}"
    )


def _project_prompt_body(trig: dict, project_id: str, task_id: str, description: str) -> str:
    body = str(trig.get("prompt_body") or "").strip()
    if body:
        return body
    return (
        f"DISPATCH {task_id} — recurring project cadence fire from orch-cron "
        f"for project {project_id}. {description}"
    )


def _completed_recurring_project_next_ready(session_id: str, project_id: str) -> Optional[dict]:
    cfg = OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        row = session.run(
            """
            MATCH (proj:OrchProject {id: $project_id})-[:HAS_PHASE]->(ph:OrchPhase)-[:HAS_TASK]->(t:OrchTask)
            WHERE t.status = 'completed'
              AND coalesce(t.recurring, false) = true
              AND coalesce(t.owner, '') = $session_id
              AND coalesce(t.blocked_on, '') = ''
              AND coalesce(toLower(trim(proj.status)), '') IN ['active', 'in_progress', 'completed']
              AND NOT EXISTS {
                  MATCH (t)-[:DEPENDS_ON]->(dep:OrchTask)
                  WHERE dep.status <> 'completed'
              }
            RETURN t.id AS task_id,
                   t.description AS description,
                   t.priority AS priority,
                   t.owner AS owner,
                   ph.id AS phase_id,
                   ph.name AS phase_name,
                   proj.id AS project_id,
                   proj.name AS project_name
            ORDER BY toInteger(coalesce(t.reclaim_count, 0)) ASC,
                     toInteger(coalesce(t.priority, 999999999)) ASC,
                     t.created_at ASC,
                     t.id ASC
            LIMIT 1
            """,
            project_id=project_id,
            session_id=session_id,
        ).single()
    return dict(row) if row else None


def _fire_task_trigger(r, trig: dict, now_local: datetime, dry_run: bool = False) -> str:
    trig_id = trig.get("id") or trig.get("task_id") or trig.get("session", "?")
    session = str(trig.get("session") or "").strip()
    task_id = str(trig.get("task_id") or "").strip()
    if not session:
        return "skipped:no_session"
    if not task_id:
        return "skipped:no_task_id"

    description = str(trig.get("description") or f"Recurring task cadence fire: {task_id}")
    fire_id = f"{trig_id}-{now_local:%Y%m%d-%H%M}"
    if dry_run:
        log.info("[DRY] would dispatch task trigger %s @ %s task=%s session=%s",
                 trig_id, fire_id, task_id, session)
        return "dry_run"
    if not _dedup_fire(r, fire_id):
        return "skipped:already_fired"

    try:
        from fleet_orchestrator.dispatch import OrchTaskNotReady, WorkerBusy, dispatch

        dispatch(
            session,
            task_id,
            description,
            supervisor=str(trig.get("supervisor") or session),
            prompt_body=_task_prompt_body(trig, task_id, description),
            priority=str(trig.get("priority") or "normal"),
            is_bugfix=bool(trig.get("is_bugfix", False)),
            force=bool(trig.get("force", False)),
        )
    except (OrchTaskNotReady, WorkerBusy) as exc:
        _clear_dedup(r, fire_id)
        log.info("SKIP task trigger %s task=%s session=%s: %s",
                 trig_id, task_id, session, exc)
        return "skipped:task_not_ready"

    log.info("FIRE %s session=%s task=%s", trig_id, session, task_id)
    return "dispatched"


def _fire_project_trigger(r, trig: dict, now_local: datetime, dry_run: bool = False) -> str:
    trig_id = trig.get("id") or trig.get("project") or trig.get("session", "?")
    session = str(trig.get("session") or "").strip()
    project_id = str(trig.get("project") or "").strip()
    if not session:
        return "skipped:no_session"
    if not project_id:
        return "skipped:no_project"

    fire_id = f"{trig_id}-{now_local:%Y%m%d-%H%M}"
    if dry_run:
        log.info("[DRY] would dispatch project next-ready trigger %s @ %s project=%s session=%s",
                 trig_id, fire_id, project_id, session)
        return "dry_run"
    if not _dedup_fire(r, fire_id):
        return "skipped:already_fired"

    from fleet_orchestrator.dispatch import OrchTaskNotReady, WorkerBusy, dispatch
    from fleet_orchestrator.orch_schema import get_session_next_ready, project_cycle_in_flight

    cycle_state = project_cycle_in_flight(project_id)
    if int(cycle_state.get("active_count") or 0) > 0:
        log.info("SKIP project trigger %s project=%s session=%s: skipped:cycle_in_flight %s",
                 trig_id, project_id, session, cycle_state)
        return "skipped:cycle_in_flight"
    next_ready = (
        get_session_next_ready(session, project_id=project_id)
        or _completed_recurring_project_next_ready(session, project_id)
    )
    task_id = str((next_ready or {}).get("task_id") or (next_ready or {}).get("id") or "").strip()
    if not task_id:
        log.info("SKIP project trigger %s project=%s session=%s: no ready task",
                 trig_id, project_id, session)
        return "skipped:no_ready_task"

    description = str(
        trig.get("description")
        or (next_ready or {}).get("description")
        or f"Recurring project cadence fire: {project_id}"
    )
    try:
        dispatch(
            session,
            task_id,
            description,
            supervisor=str(trig.get("supervisor") or session),
            prompt_body=_project_prompt_body(trig, project_id, task_id, description),
            priority=str(trig.get("priority") or "normal"),
            is_bugfix=bool(trig.get("is_bugfix", False)),
            force=bool(trig.get("force", False)),
        )
    except (OrchTaskNotReady, WorkerBusy) as exc:
        _clear_dedup(r, fire_id)
        log.info("SKIP project trigger %s project=%s session=%s task=%s: %s",
                 trig_id, project_id, session, task_id, exc)
        return "skipped:task_not_ready"

    trig["_orch_cron_project_task_id"] = task_id
    log.info("FIRE project trigger %s session=%s project=%s task=%s",
             trig_id, session, project_id, task_id)
    return "dispatched"


def _fire_command_trigger(r, trig: dict, now_local: datetime, dry_run: bool = False) -> str:
    trig_id = trig.get("id") or "command"
    command = trig.get("command")
    if isinstance(command, list):
        has_command = bool(command)
    else:
        has_command = bool(str(command or "").strip())
    if not has_command:
        log.warning("SKIP command trigger %s: no command configured", trig_id)
        return "skipped:no_command"

    try:
        timeout_sec = _command_timeout_sec(trig)
    except (TypeError, ValueError) as exc:
        log.warning("SKIP command trigger %s: bad timeout_sec: %s", trig_id, exc)
        return "skipped:bad_timeout"

    cwd = str(trig.get("cwd") or "").strip() or None
    fire_id = f"{trig_id}-{now_local:%Y%m%d-%H%M}"
    args, shell = _command_args(command)
    if dry_run:
        log.info("[DRY] would run command trigger %s @ %s command=%r cwd=%s timeout=%s",
                 trig_id, fire_id, command, cwd or "", timeout_sec)
        return "dry_run"
    if not _dedup_fire(r, fire_id):
        return "skipped:already_fired"

    started = time.monotonic()
    base_record = {
        "ts": int(time.time()),
        "fire_id": fire_id,
        "trigger_mode": "command",
        "command": command,
        "cwd": cwd or "",
        "timeout_sec": timeout_sec,
        "tz_hour_minute": f"{now_local:%H:%M}",
        "hostname": socket.gethostname(),
    }
    try:
        completed = subprocess.run(
            args,
            cwd=cwd,
            timeout=timeout_sec,
            capture_output=True,
            text=True,
            shell=shell,
        )
        duration = time.monotonic() - started
        result = f"command:exit_{completed.returncode}"
        record = {
            **base_record,
            "duration_sec": round(duration, 3),
            "exit_code": completed.returncode,
            "stdout": _truncate_output(completed.stdout),
            "stderr": _truncate_output(completed.stderr),
            "result": result,
        }
        append_state_file(trig.get("state_file"), record)
        log.info("FIRE command trigger %s exit=%s duration=%.3fs cwd=%s",
                 trig_id, completed.returncode, duration, cwd or "")
        return result
    except subprocess.TimeoutExpired as exc:
        duration = time.monotonic() - started
        record = {
            **base_record,
            "duration_sec": round(duration, 3),
            "exit_code": None,
            "stdout": _truncate_output(exc.stdout),
            "stderr": _truncate_output(exc.stderr),
            "result": "command:timeout",
        }
        append_state_file(trig.get("state_file"), record)
        log.warning("TIMEOUT command trigger %s after %.3fs (timeout=%ss)",
                    trig_id, duration, timeout_sec)
        return "command:timeout"
    except OSError as exc:
        duration = time.monotonic() - started
        record = {
            **base_record,
            "duration_sec": round(duration, 3),
            "exit_code": None,
            "stdout": "",
            "stderr": "",
            "error": str(exc),
            "result": "command:error",
        }
        append_state_file(trig.get("state_file"), record)
        log.error("ERROR command trigger %s: %s", trig_id, exc)
        return "command:error"


def fire_trigger(r, trig: dict, now_local: datetime, dry_run: bool = False) -> str:
    """Fire one due trigger. Returns the result label written to state_file."""
    trig_id = trig.get("id") or trig.get("session", "?")

    if not trig.get("enabled", True):
        return "skipped:disabled"

    route_fields = _trigger_route_fields(trig)
    if len(route_fields) > 1:
        log.warning("SKIP trigger %s: ambiguous trigger modes %s", trig_id, ",".join(route_fields))
        return "skipped:ambiguous_trigger"
    if route_fields == ["command"]:
        return _fire_command_trigger(r, trig, now_local, dry_run=dry_run)
    if route_fields == ["task_id"]:
        return _fire_task_trigger(r, trig, now_local, dry_run=dry_run)
    if route_fields == ["project"]:
        return _fire_project_trigger(r, trig, now_local, dry_run=dry_run)

    session = trig.get("session")
    if not session:
        return "skipped:no_session"

    prompt_file = trig.get("prompt_file")
    if not prompt_file or not os.path.isfile(prompt_file):
        return "skipped:no_prompt_file"

    with open(prompt_file) as f:
        prompt = f.read()
    prompt_hash = "sha256:" + hashlib.sha256(prompt.encode()).hexdigest()[:16]

    fire_id = f"{trig_id}-{now_local:%Y%m%d-%H%M}"

    if dry_run:
        log.info("[DRY] would fire %s @ %s — %d char prompt", trig_id,
                 fire_id, len(prompt))
        return "dry_run"

    # Redis dedup so two cron ticks in the same minute don't double-fire.
    # Only set the dedup key after we've passed the dry-run check, so a
    # dry-run doesn't block a subsequent real fire in the same minute.
    if not _dedup_fire(r, fire_id):
        return "skipped:already_fired"

    cli = OrchConfig().notify_cli_path
    cli_path = shutil.which(cli) or (cli if os.path.isfile(cli) and os.access(cli, os.X_OK) else None)
    if cli_path is None:
        raise SystemExit(f"notify CLI not found: {cli}")
    result = subprocess.run(
        [cli_path, session, prompt, "--from", "orch-cron", "--type", "wake"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        raise SystemExit(result.stderr.strip() or f"{cli_path} failed")

    log.info("FIRE %s session=%s (%d char prompt, hash=%s)",
             trig_id, session, len(prompt), prompt_hash)
    return "dispatched"


def trigger_payload_hash(trig: dict) -> str:
    if "command" in trig:
        payload = json.dumps(
            {"command": trig.get("command"), "cwd": trig.get("cwd") or ""},
            sort_keys=True,
        )
        return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()[:16]
    if trig.get("project"):
        payload = json.dumps(
            {
                "project": trig.get("project"),
                "session": trig.get("session") or "",
                "description": trig.get("description") or "",
            },
            sort_keys=True,
        )
        return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()[:16]
    if trig.get("prompt_file") and os.path.isfile(trig["prompt_file"]):
        with open(trig["prompt_file"], "rb") as f:
            return "sha256:" + hashlib.sha256(f.read()).hexdigest()[:16]
    if trig.get("task_id"):
        task_id = str(trig.get("task_id") or "")
        payload = _task_prompt_body(
            trig,
            task_id,
            str(trig.get("description") or f"Recurring task cadence fire: {task_id}"),
        )
        return "sha256:" + hashlib.sha256(payload.encode()).hexdigest()[:16]
    return ""


def append_state_file(state_file: Optional[str], record: dict) -> Optional[str]:
    """Append a fire record to state_file, then SHA-256 the full file and
    write a sidecar ``<state_file>.meta.json`` with {last_fire_log_hash,
    last_fire_ts, last_fire_id, last_fire_size_bytes}. Returns the hash
    (or None if no state_file was configured).

    Hash-on-fire is the BLACK_HOLE/CANNOT_LIE_PROVENANCE invariant for
    recurring-task state — graph queries (or any downstream auditor) can
    verify the state_file hasn't drifted from what we last wrote without
    storing the full log in the graph. <1ms IO cost per fire, no cardinality explosion at
    high-cadence loops (8/day per loop today).
    """
    if not state_file:
        return None
    try:
        os.makedirs(os.path.dirname(state_file), exist_ok=True)
        with open(state_file, "a") as f:
            f.write(json.dumps(record) + "\n")

        # Hash the full state_file post-append. The meta sidecar lives
        # alongside (not inside) the jsonl so tail-grep usability is
        # preserved.
        h = hashlib.sha256()
        size = 0
        with open(state_file, "rb") as f:
            for chunk in iter(lambda: f.read(65536), b""):
                h.update(chunk)
                size += len(chunk)
        digest = "sha256:" + h.hexdigest()
        meta = {
            "last_fire_log_hash": digest,
            "last_fire_ts": record.get("ts"),
            "last_fire_id": record.get("fire_id"),
            "last_fire_size_bytes": size,
        }
        with open(state_file + ".meta.json", "w") as f:
            json.dump(meta, f, indent=2)
        return digest
    except Exception as exc:
        log.error("state_file write/hash failed (%s): %s", state_file, exc)
        return None


def tick(registry_path: str, redis_client, dry_run: bool = False,
         now_override: Optional[datetime] = None) -> int:
    """Run one tick across all triggers. Returns number of fires."""
    reg = load_registry(registry_path)
    fires = 0
    for trig in reg.get("triggers", []):
        trig_id = "?"
        try:
            trig_id = _trigger_id(trig)
            tz = ZoneInfo(trig.get("tz", "America/New_York"))
            now_local = (now_override or datetime.now(tz)).astimezone(tz) \
                if now_override and now_override.tzinfo \
                else (now_override or datetime.now(tz))

            if not should_fire(trig, now_local):
                continue

            result = fire_trigger(redis_client, trig, now_local, dry_run=dry_run)
            prompt_hash = trigger_payload_hash(trig)
            # Only append a state_file record for ACTUAL dispatches. Skipped
            # ticks (already-fired, disabled, no-prompt-file) and dry-runs
            # are logged but not persisted, so the state_file stays a clean
            # audit trail of "what actually went out" + the matching hash
            # sidecar genuinely represents dispatch state.
            if result == "dispatched":
                trigger_mode = (
                    "project" if trig.get("project")
                    else ("task" if trig.get("task_id") else "prompt")
                )
                record = {
                    "ts": int(time.time()),
                    "fire_id": f"{trig_id}-{now_local:%Y%m%d-%H%M}",
                    "session": trig.get("session"),
                    "trigger_mode": trigger_mode,
                    "project": trig.get("project") or "",
                    "task_id": trig.get("task_id") or trig.get("_orch_cron_project_task_id") or "",
                    "tz_hour_minute": f"{now_local:%H:%M}",
                    "prompt_hash": prompt_hash,
                    "result": result,
                    "hostname": socket.gethostname(),
                }
                append_state_file(trig.get("state_file"), record)
                fires += 1
            elif "command" in trig and result.startswith("command:"):
                fires += 1
        except Exception as exc:
            log.error("bad trigger %s: %s", trig_id, exc)
            continue

    return fires


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--registry", required=True,
                        help="Path to recurring.json registry.")
    parser.add_argument("--redis-host", default=os.environ.get("REDIS_HOST", "127.0.0.1"))
    parser.add_argument("--redis-port", type=int,
                        default=int(os.environ.get("REDIS_PORT", "6379")))
    parser.add_argument("--watch", action="store_true",
                        help="Long-running mode: sleep 60s between ticks. "
                             "Default is single-tick (intended for system cron).")
    parser.add_argument("--dry-run", action="store_true",
                        help="Don't actually fire taey-notify; log what would happen.")
    parser.add_argument("--now",
                        help="Override 'now' for simulation, ISO 8601 with tz "
                             "(e.g. '2026-05-26 12:09:00-04:00').")
    args = parser.parse_args()

    redis_client = None
    try:
        import redis as redis_lib
        redis_client = redis_lib.Redis(host=args.redis_host, port=args.redis_port,
                                        decode_responses=True, socket_timeout=2)
        redis_client.ping()
    except Exception as exc:
        log.warning("Redis unreachable (%s); dedup disabled, falls back to "
                    "best-effort exact-minute behavior.", exc)
        redis_client = None

    now_override = None
    if args.now:
        try:
            now_override = datetime.fromisoformat(args.now)
        except Exception as exc:
            raise SystemExit(f"bad --now: {exc}")

    if not args.watch:
        n = tick(args.registry, redis_client, dry_run=args.dry_run,
                 now_override=now_override)
        log.info("tick complete: %d fires", n)
        return

    log.info("Started in --watch mode (60s loop). registry=%s", args.registry)
    while True:
        try:
            n = tick(args.registry, redis_client, dry_run=args.dry_run)
            if n:
                log.info("tick: %d fires", n)
        except Exception as exc:
            log.error("tick failed: %s", exc)
        time.sleep(60)


if __name__ == "__main__":
    main()
