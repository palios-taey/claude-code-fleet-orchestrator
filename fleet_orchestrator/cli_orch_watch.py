#!/usr/bin/env python3
"""orch-watch — event-driven supervisor wake daemon.

Replaces the 3-min-poll watchloop that spammed supervisors with
"CONTINUE" messages even when there was no work. Subscribes to Redis
keyspace notifications and fires wakes ONLY when a state transition
actually warrants supervisor attention.

TWO SIGNALS the daemon pages on (both are silent stalls no awake actor
would notice):

1. ``Stuck current_task while worker idle`` — a worker is idle with
   current_task still set (outcome != done) longer than
   ``--stuck-threshold-sec``. The Stop hook already fired its single
   peer_idle when the worker stopped; this catches the case where the
   supervisor missed or didn't act on it.

2. ``Done-DEL that unblocks supervisor's OrchTask while supervisor is
   idle`` — symmetric twin of the stuck signal. When a worker finishes
   cleanly (outcome=done), the Stop hook deletes current_task. If that
   completion unblocked a previously-blocked OrchTask whose owner is
   currently idle, the supervisor won't poll themselves — actionable
   work sits forever. The DEL handler invokes a pluggable readiness
   checker (``--readiness-checker``); if the checker says "yes,
   supervisor X has newly-ready work", page X.

NEITHER signal is a pull state. Pulls (inbox piling up, no-progress-
since-last-event) are not paged — supervisors drain them on their next
loop, or the daemon's safety-net sweep catches them.

HYBRID INVOCATION: primary always-on Redis
PSUBSCRIBE for low-latency event-driven wakes + independent low-cadence
safety-net poll (``--sweep-interval-sec``, default 1800s = 30 min) that
runs the SAME investigate handler over all known nodes. Keyspace
notifications are best-effort / at-most-once / fire-and-forget; pure
always-on has non-zero silent missed-event probability on subscriber
death, network partition, or Redis restart. The 30-min poll covers
that gap with linear complexity delta and exponential reliability gain.

Both invocation paths route through the same ``investigate(node_id)``
function — canonical source for "stuck/unblocked" semantics and
the wake message shape.

Requires Redis to have ``notify-keyspace-events`` configured to include
at minimum ``Kgl$`` (keyspace + generic + list + string events). The
installer (Phase D) will set this; for manual install run::

    redis-cli CONFIG SET notify-keyspace-events 'Kgl$'
    redis-cli CONFIG REWRITE   # persist to redis.conf

Run from peer-respawn DAEMONS list or systemd::

    orch-watch --redis-host 127.0.0.1 --stuck-threshold-sec 300 \\
               --readiness-checker /path/to/plan_readiness.py:check_readiness

Output: structured logging to stderr; one peer_idle-style wake message
per detected condition pushed to the supervisor's inbox via the released
``taey-notify`` CLI. Dedup TTL ``--dedup-ttl-sec`` (default 3600)
prevents the same stuck-task wake from re-firing every event.

Readiness-checker interface
---------------------------

The DEL-side handler invokes an external function the operator wires up::

    def check_readiness(supervisor: str, completed_task: dict) -> Optional[str]:
        \"\"\"Return a wake message body if this completion unblocked work
        for an idle supervisor, else None.\"\"\"

``completed_task`` is the dict that was in ``taey:<worker>:current_task``
immediately before deletion: {task_id, description, supervisor,
started_at}. Implementations typically check the supervisor's plan
tracker for ready tasks now owned by them, or check Neo4j OrchTask
dependencies. The fleet-orchestrator plan tracker (Phase D, v0.4.0)
will ship a default implementation; for v0.2.x the operator wires their
own.

If no ``--readiness-checker`` is configured, DEL events are logged and
skipped (preserves backward compat with Phase B v0.2.0).
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from datetime import datetime
from typing import Dict, Optional

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from fleet_orchestrator.config import OrchConfig
from fleet_orchestrator.evidence_contract import TERMINAL_STATUSES
from fleet_orchestrator.handoff_validation import handoff_index_key, process_expired_handoffs
from fleet_orchestrator.notify_state import (
    key as notify_key,
    key_prefix as notify_key_prefix,
    state_key as notify_state_key,
)

import redis as redis_lib


def _configure_realtime_stderr(stream=sys.stderr) -> bool:
    reconfigure = getattr(stream, "reconfigure", None)
    if not callable(reconfigure):
        return False
    try:
        reconfigure(line_buffering=True)
    except (TypeError, ValueError, OSError):
        return False
    return True


_configure_realtime_stderr()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [orch-watch] %(levelname)s %(message)s",
)
log = logging.getLogger(__name__)

# Reuse fleet-notify's supervisor resolution rule so this daemon and the
# Stop hook never disagree about who to address.
SUFFIX_SUPERVISOR_RULES = ("-codex", "-gemini", "-grok")
NOTIFY_KEY_PREFIX = notify_key_prefix()
NOTIFY_DAEMON_HEARTBEAT_NODE = "_notify_daemon"
NOTIFY_DAEMON_HEARTBEAT_SUFFIX = "heartbeat"
DEFAULT_NOTIFY_DAEMON_WATCH_INTERVAL_SEC = 30
DEFAULT_NOTIFY_DAEMON_HEARTBEAT_MAX_AGE_SEC = 15
DEFAULT_NOTIFY_DAEMON_ALERT_DEDUP_TTL_SEC = 300
DEFAULT_STUCK_INBOX_MAX_AGE_SEC = 600
DEFAULT_COMPOSER_OCCUPANCY_MAX_AGE_SEC = 300
DEFAULT_WEDGED_COMPOSER_STABILITY_WINDOW_SEC = 120
DEFAULT_WEDGED_COMPOSER_REARM_SEC = 1800
DEFAULT_NOTIFY_ROUTER_SERVICE = "conductor-notify-router"
DEFAULT_NOTIFY_DAEMON_ALERT_TARGET = "conductor"
WEDGED_COMPOSER_TERMINAL_TASK_STATUSES = TERMINAL_STATUSES | frozenset({
    "cancelled",
    "done",
    "killed",
    "superseded",
})
COMPOSER_IGNORED_PROMPT_PREFIXES = (
    "use /skills to list available skills",
    "how is claude doing this session?",
)
USAGE_LIMIT_IDLE_MARKERS = (
    "you've hit your session limit",
    "you have hit your session limit",
    "you've reached your session limit",
    "you have reached your session limit",
    "you've hit your weekly limit",
    "you have hit your weekly limit",
    "you've reached your weekly limit",
    "you have reached your weekly limit",
    "you've hit your usage limit",
    "you have hit your usage limit",
    "you've reached your usage limit",
    "you have reached your usage limit",
)
USAGE_LIMIT_TRANSIENT_EXCLUSIONS = (
    "not your usage limit",
)
USAGE_LIMIT_RESTING_REGION_NONBLANK_LINES = 3
PANE_RESTING_REGION_NONBLANK_LINES = 8
PANE_WORKING_INDICATOR_MARKERS = (
    "esc to interrupt",
    "escape to interrupt",
    "ctrl c to interrupt",
    "ctrl+c to interrupt",
)
PANE_RESTING_PROMPT_LINES = {"$", ">", "\u276f", "\u203a"}


def state_key(node_id: str, suffix: str) -> str:
    return notify_state_key(node_id, suffix, prefix=NOTIFY_KEY_PREFIX)


def orch_key(namespace: str, *parts: str) -> str:
    return notify_key(":".join([namespace, *[str(part) for part in parts]]), prefix=NOTIFY_KEY_PREFIX)


def current_task_scan_pattern() -> str:
    return notify_key("*:current_task", prefix=NOTIFY_KEY_PREFIX)


def inbox_scan_pattern() -> str:
    return notify_key("*:inbox", prefix=NOTIFY_KEY_PREFIX)


def node_from_current_task_key(key: str) -> Optional[str]:
    prefix = f"{NOTIFY_KEY_PREFIX}:"
    suffix = ":current_task"
    if not key.startswith(prefix) or not key.endswith(suffix):
        return None
    return key[len(prefix):-len(suffix)]


def node_from_inbox_key(key: str) -> Optional[str]:
    prefix = f"{NOTIFY_KEY_PREFIX}:"
    suffix = ":inbox"
    if not key.startswith(prefix) or not key.endswith(suffix):
        return None
    return key[len(prefix):-len(suffix)]

# Redis key patterns the daemon subscribes to. KEYSPACE notifications
# arrive on channels named __keyspace@<db>__:<key>. We subscribe via
# PSUBSCRIBE with glob patterns.
SUBSCRIBE_PATTERNS = (
    f"__keyspace@0__:{NOTIFY_KEY_PREFIX}:*:current_task",
    f"__keyspace@0__:{NOTIFY_KEY_PREFIX}:*:idle",
    f"__keyspace@0__:{NOTIFY_KEY_PREFIX}:*:last_activity",
)

# Extract <node> from "__keyspace@0__:<prefix>:<node>:<suffix>"
_KEY_RE = re.compile(rf"^__keyspace@\d+__:{re.escape(NOTIFY_KEY_PREFIX)}:(.+):([a-z_]+)$")


def _local_hostname() -> str:
    try:
        return socket.gethostname().strip()
    except OSError:
        return ""


def _has_notify_session_state(r, session_id: str) -> bool:
    try:
        if r.exists(state_key(session_id, "current_task")):
            return True
        if r.exists(state_key(session_id, "idle")):
            return True
        if r.exists(state_key(session_id, "last_activity")):
            return True
    except Exception:
        return False
    return False


def _supervisor_candidate(r, value: object) -> Optional[str]:
    candidate = str(value or "").strip()
    if not candidate:
        return None
    host = _local_hostname()
    if not host or candidate != host:
        return candidate
    try:
        if candidate in set(OrchConfig().session_ids or []):
            return candidate
    except Exception:
        pass
    if _has_notify_session_state(r, candidate):
        return candidate
    if candidate in _local_tmux_sessions():
        return candidate
    log.debug("Ignoring non-session supervisor candidate %s (matches local host name)", candidate)
    return None


def resolve_supervisor(r, node_id: str, task: Optional[dict] = None) -> Optional[str]:
    """Resolve the deliverable supervisor for a worker alert.

    Current-task payloads are the dispatch contract, so their supervisor or
    dispatcher wins over legacy parent/suffix fallback. A local hostname fallback
    is ignored unless it is also configured or visible as a real local session.
    """
    candidates: list[object] = []
    if isinstance(task, dict):
        candidates.extend([task.get("supervisor"), task.get("dispatcher")])
    try:
        explicit = r.get(state_key(node_id, "parent"))
        if explicit:
            candidates.append(explicit)
    except Exception:
        pass
    for suffix in SUFFIX_SUPERVISOR_RULES:
        if node_id.endswith(suffix):
            candidates.append(node_id[: -len(suffix)])
            break
    for candidate in candidates:
        resolved = _supervisor_candidate(r, candidate)
        if resolved:
            return resolved
    return None


def parse_node_from_channel(channel: str):
    """Return (node_id, suffix) or (None, None) if the channel doesn't match
    one of our subscription patterns."""
    m = _KEY_RE.match(channel)
    if not m:
        return None, None
    return m.group(1), m.group(2)


def get_current_task(r, node_id: str):
    """Read taey:<node>:current_task as dict or None."""
    raw = r.get(state_key(node_id, "current_task"))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return {"raw": raw}


def get_last_outcome(r, node_id: str):
    """Read taey:<node>:last_outcome as dict or None."""
    raw = r.get(state_key(node_id, "last_outcome"))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return {"outcome": "unknown", "details": raw}


def _load_task_state(task_id: str):
    from fleet_orchestrator.config import OrchConfig
    from fleet_orchestrator.orch_schema import get_task

    return get_task(task_id, config=OrchConfig())


def _task_project_context(task_id: str) -> Optional[Dict[str, object]]:
    from fleet_orchestrator.config import OrchConfig
    from fleet_orchestrator.orch_schema import get_task_project

    return get_task_project(task_id, config=OrchConfig())


def _send_wake(r, target: str, body: str, priority: str, msg_id: str) -> bool:
    del r, msg_id
    cli = OrchConfig().notify_cli_path
    cli_path = shutil.which(cli) or (cli if os.path.isfile(cli) and os.access(cli, os.X_OK) else None)
    if cli_path is None:
        raise RuntimeError(f"notify CLI not found: {cli}")
    result = subprocess.run(
        [cli_path, target, body, "--from", "orch-watch", "--type", "wake", "--priority", priority],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        log.error("taey-notify failed for %s: %s", target, result.stderr.strip())
        return False
    return True


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off", ""}


def _int_env(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name, str(default)).strip()
    try:
        return max(minimum, int(raw))
    except (TypeError, ValueError):
        return default


def notify_daemon_heartbeat_key() -> str:
    return state_key(NOTIFY_DAEMON_HEARTBEAT_NODE, NOTIFY_DAEMON_HEARTBEAT_SUFFIX)


def _parse_notify_daemon_heartbeat(raw: object) -> tuple[Optional[float], Optional[str]]:
    if raw is None:
        return None, None
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    text = str(raw).strip()
    if not text:
        return None, None
    if text.startswith("{"):
        try:
            payload = json.loads(text)
            ts = payload.get("ts") or payload.get("timestamp") or payload.get("time")
            host = payload.get("host") or payload.get("machine")
            return float(ts), str(host) if host else None
        except (TypeError, ValueError, json.JSONDecodeError):
            return None, None
    ts_text, sep, host = text.partition("+")
    try:
        return float(ts_text), host if sep and host else None
    except (TypeError, ValueError):
        return None, None


def _notify_router_service_status(service_name: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["systemctl", "--user", "is-active", service_name],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return False, f"systemctl exception: {exc}"
    detail = (result.stdout or result.stderr or f"exit={result.returncode}").strip()
    return result.returncode == 0 and detail == "active", detail or f"exit={result.returncode}"


def _send_notify_daemon_tmux_alert(target_session: str, body: str) -> bool:
    steps = (
        ("clear input", ["C-u"]),
        ("write alert body", ["-l", body]),
        ("submit alert", ["Enter"]),
    )
    for label, keys in steps:
        if label == "submit alert":
            time.sleep(0.3)
        try:
            result = subprocess.run(
                ["tmux", "send-keys", "-t", target_session, *keys],
                capture_output=True,
                text=True,
                timeout=10,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.critical("notify-daemon watchdog tmux alert failed for %s during %s: %s",
                         target_session, label, exc)
            return False
        if result.returncode != 0:
            log.critical(
                "notify-daemon watchdog tmux alert failed for %s during %s: %s",
                target_session,
                label,
                (result.stderr or result.stdout or f"exit={result.returncode}").strip(),
            )
            return False
    return True


def _send_notify_daemon_desktop_alert(
    reason: str,
    title: str = "CRITICAL: notify daemon liveness failed",
) -> None:
    notify_send = shutil.which("notify-send")
    if not notify_send:
        return
    try:
        subprocess.run(
            [
                notify_send,
                "-u",
                "critical",
                title,
                reason[:500],
            ],
            capture_output=True,
            text=True,
            timeout=3,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.error("notify-send failed for notify-daemon watchdog alert: %s", exc)


def _local_tmux_sessions() -> set[str]:
    try:
        result = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("tmux session inventory failed during stuck-inbox remediation: %s", exc)
        return set()
    if result.returncode != 0:
        return set()
    return {line.strip() for line in result.stdout.splitlines() if line.strip()}


def _tmux_pane_tail(session_name: str, *, lines: int = 80) -> str:
    try:
        result = subprocess.run(
            ["tmux", "capture-pane", "-p", "-t", session_name, "-S", f"-{lines}"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.debug("tmux pane capture failed during stuck-inbox remediation for %s: %s",
                  session_name, exc)
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout or ""


def _recent_nonblank_pane_lines(pane_text: str, *, limit: int) -> list[str]:
    nonblank_lines = [line.strip() for line in (pane_text or "").splitlines() if line.strip()]
    return nonblank_lines[-limit:]


def _normalize_pane_region(pane_text: str) -> str:
    lowered = (pane_text or "").lower().replace("-", " ")
    return " ".join(lowered.split())


def _usage_limit_resting_region(pane_text: str) -> str:
    return "\n".join(_recent_nonblank_pane_lines(
        pane_text,
        limit=USAGE_LIMIT_RESTING_REGION_NONBLANK_LINES,
    ))


def _pane_shows_usage_limit_resting_state(pane_text: str) -> bool:
    normalized = _normalize_pane_region(_usage_limit_resting_region(pane_text))
    if not normalized:
        return False
    if any(marker in normalized for marker in USAGE_LIMIT_TRANSIENT_EXCLUSIONS):
        return False
    return any(marker in normalized for marker in USAGE_LIMIT_IDLE_MARKERS)


def _pane_shows_working_indicator(pane_text: str) -> bool:
    normalized = _normalize_pane_region(pane_text)
    return any(marker in normalized for marker in PANE_WORKING_INDICATOR_MARKERS)


def _pane_shows_resting_input_prompt(pane_text: str) -> bool:
    recent_lines = _recent_nonblank_pane_lines(
        pane_text,
        limit=PANE_RESTING_REGION_NONBLANK_LINES,
    )
    if not recent_lines:
        return False
    recent_region = "\n".join(recent_lines)
    if _pane_shows_working_indicator(recent_region):
        return False
    if _pane_shows_usage_limit_resting_state(recent_region):
        return True
    return any(line.strip() in PANE_RESTING_PROMPT_LINES for line in recent_lines)


def _reconcile_stranded_idle_for_stuck_inbox(r, node_id: str) -> bool:
    if r.get(state_key(node_id, "idle")):
        return False
    if r.exists(state_key(node_id, "tool_running")):
        return False
    if node_id not in _local_tmux_sessions():
        return False
    if not _pane_shows_resting_input_prompt(_tmux_pane_tail(node_id)):
        return False
    r.set(state_key(node_id, "idle"), "1")
    log.warning("Reconciled idle=1 for %s before stuck-inbox alert", node_id)
    return True


def _stuck_inbox_dedup_key(node_id: str, msg_id: object) -> str:
    return orch_key("notify-daemon-watchdog-stuck-inbox", node_id, str(msg_id or "unknown"))


def _stuck_inbox_dedup_pattern(node_id: str = "*") -> str:
    return orch_key("notify-daemon-watchdog-stuck-inbox", node_id, "*")


def _clear_drained_stuck_inbox_dedup_keys(r) -> int:
    cleared = 0
    for key_name in list(r.scan_iter(match=_stuck_inbox_dedup_pattern())):
        key = str(key_name)
        parts = key.split(":")
        if len(parts) < 4:
            continue
        node_id = parts[-2]
        try:
            if r.lrange(notify_key(f"{node_id}:inbox", prefix=NOTIFY_KEY_PREFIX), 0, -1):
                continue
            cleared += r.delete(key)
        except redis_lib.RedisError as exc:
            log.error("stuck inbox watchdog dedup cleanup failed for %s: %s", key, exc)
    return cleared


def _send_stuck_inbox_conductor_alert(r, body: str, *, oldest: dict[str, object],
                                      now: float) -> bool:
    msg_id = f"orch-watch-stuck-inbox-{oldest['node_id']}-{oldest['msg_id']}"
    payload = {
        "from": "orch-watch",
        "type": "MESSAGE",
        "priority": "high",
        "timestamp": now,
        "msg_id": msg_id,
        "body": body,
    }
    try:
        r.lpush(notify_key(f"{DEFAULT_NOTIFY_DAEMON_ALERT_TARGET}:inbox", prefix=NOTIFY_KEY_PREFIX),
                json.dumps(payload))
    except redis_lib.RedisError as exc:
        log.error("stuck inbox conductor alert enqueue failed: %s", exc)
        return False
    return True


def check_notify_daemon_liveness(
    r,
    *,
    now: Optional[float] = None,
    heartbeat_max_age_sec: int = DEFAULT_NOTIFY_DAEMON_HEARTBEAT_MAX_AGE_SEC,
    service_name: str = DEFAULT_NOTIFY_ROUTER_SERVICE,
    alert_target: str = DEFAULT_NOTIFY_DAEMON_ALERT_TARGET,
    dedup_ttl_sec: int = DEFAULT_NOTIFY_DAEMON_ALERT_DEDUP_TTL_SEC,
) -> dict[str, object]:
    current_time = _redis_now(r) if now is None else float(now)
    service_active, service_detail = _notify_router_service_status(service_name)
    raw_heartbeat = r.get(notify_daemon_heartbeat_key())
    heartbeat_ts, heartbeat_host = _parse_notify_daemon_heartbeat(raw_heartbeat)
    heartbeat_age = None if heartbeat_ts is None else current_time - heartbeat_ts
    heartbeat_fresh = heartbeat_ts is not None and heartbeat_age <= heartbeat_max_age_sec

    if service_active and heartbeat_fresh:
        try:
            r.delete(orch_key("notify-daemon-watchdog", "alert"))
        except redis_lib.RedisError:
            pass
        return {
            "ok": True,
            "alerted": False,
            "service_status": service_detail,
            "heartbeat_age_sec": heartbeat_age,
            "heartbeat_host": heartbeat_host,
        }

    reasons: list[str] = []
    if not service_active:
        reasons.append(f"{service_name} systemd status={service_detail!r}")
    if heartbeat_ts is None:
        reasons.append(f"{notify_daemon_heartbeat_key()} missing or malformed")
    elif not heartbeat_fresh:
        reasons.append(
            f"{notify_daemon_heartbeat_key()} stale age={heartbeat_age:.1f}s "
            f"host={heartbeat_host or 'unknown'}"
        )
    reason = "; ".join(reasons)
    banner = (
        "CRITICAL NOTIFY DAEMON LIVENESS FAILURE: "
        f"{reason}. Notification delivery may be collapsed. "
        f"Inspect `systemctl --user status {service_name}` and Redis key "
        f"`{notify_daemon_heartbeat_key()}`."
    )
    log.critical("%s", banner)

    dedup_key = orch_key("notify-daemon-watchdog", "alert")
    if dedup_ttl_sec > 0 and r.exists(dedup_key):
        return {
            "ok": False,
            "alerted": False,
            "deduped": True,
            "reason": reason,
            "service_status": service_detail,
            "heartbeat_age_sec": heartbeat_age,
            "heartbeat_host": heartbeat_host,
        }

    tmux_alerted = _send_notify_daemon_tmux_alert(alert_target, banner)
    _send_notify_daemon_desktop_alert(reason)
    try:
        r.set(dedup_key, "1", ex=dedup_ttl_sec)
    except redis_lib.RedisError as exc:
        log.error("notify-daemon watchdog dedup write failed: %s", exc)
    return {
        "ok": False,
        "alerted": tmux_alerted,
        "reason": reason,
        "service_status": service_detail,
        "heartbeat_age_sec": heartbeat_age,
        "heartbeat_host": heartbeat_host,
    }


def _coerce_message_timestamp(value: object) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except (TypeError, ValueError):
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def _decode_queued_message(raw: object) -> dict[str, object]:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        payload = json.loads(raw)
        return payload if isinstance(payload, dict) else {"raw": raw}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {"raw": str(raw)}


def _message_created_at(payload: dict[str, object]) -> Optional[float]:
    for key in ("timestamp", "created_at", "ts", "time", "sent_at"):
        ts = _coerce_message_timestamp(payload.get(key))
        if ts is not None:
            return ts
    return None


def _oldest_queued_inbox_message(r, *, now: float) -> Optional[dict[str, object]]:
    oldest: Optional[dict[str, object]] = None
    for inbox_key_name in r.scan_iter(match=inbox_scan_pattern()):
        key = str(inbox_key_name)
        node_id = node_from_inbox_key(key) or "unknown"
        try:
            raw_items = r.lrange(key, 0, -1)
        except redis_lib.RedisError as exc:
            log.error("stuck inbox scan failed for %s: %s", key, exc)
            continue
        for raw in raw_items:
            payload = _decode_queued_message(raw)
            created_at = _message_created_at(payload)
            if created_at is None:
                continue
            age = now - created_at
            if oldest is None or age > float(oldest["age_sec"]):
                oldest = {
                    "key": key,
                    "node_id": node_id,
                    "age_sec": age,
                    "created_at": created_at,
                    "from": payload.get("from") or payload.get("platform") or "unknown",
                    "type": payload.get("type") or payload.get("status") or "message",
                    "msg_id": payload.get("msg_id") or payload.get("id") or "unknown",
                }
    return oldest


def check_stuck_inbox_delivery(
    r,
    *,
    now: Optional[float] = None,
    max_age_sec: int = DEFAULT_STUCK_INBOX_MAX_AGE_SEC,
    alert_target: str = DEFAULT_NOTIFY_DAEMON_ALERT_TARGET,
    dedup_ttl_sec: int = DEFAULT_NOTIFY_DAEMON_ALERT_DEDUP_TTL_SEC,
) -> dict[str, object]:
    current_time = _redis_now(r) if now is None else float(now)
    _clear_drained_stuck_inbox_dedup_keys(r)
    oldest = _oldest_queued_inbox_message(r, now=current_time)
    if not oldest or float(oldest["age_sec"]) <= max_age_sec:
        return {"ok": True, "alerted": False, "oldest": oldest}

    if _reconcile_stranded_idle_for_stuck_inbox(r, str(oldest["node_id"])):
        return {
            "ok": True,
            "alerted": False,
            "remediated": True,
            "oldest": _oldest_queued_inbox_message(r, now=current_time),
        }

    dedup_key = _stuck_inbox_dedup_key(str(oldest["node_id"]), oldest["msg_id"])
    reason = (
        f"{oldest['key']} has undelivered message age={float(oldest['age_sec']):.1f}s "
        f"from={oldest['from']} type={oldest['type']} msg_id={oldest['msg_id']}"
    )
    banner = (
        "CRITICAL NOTIFY DELIVERY SLO FAILURE: "
        f"{reason}. The notify daemon may be alive while delivery is stuck; "
        "investigate the recipient idle flag, hooks, and inbox drain path."
    )

    del dedup_ttl_sec
    if r.exists(dedup_key):
        return {"ok": False, "alerted": False, "deduped": True, "reason": reason, "oldest": oldest}

    del alert_target
    log.critical("%s", banner)
    alerted = _send_stuck_inbox_conductor_alert(r, banner, oldest=oldest, now=current_time)
    try:
        r.set(dedup_key, "1")
    except redis_lib.RedisError as exc:
        log.error("stuck inbox watchdog dedup write failed: %s", exc)
    return {"ok": False, "alerted": alerted, "reason": reason, "oldest": oldest}


def _target_stop_decision_allows_stop(target: str, wake_reason: str,
                                      task_id: Optional[str] = None) -> bool:
    from fleet_orchestrator.orch_schema import WAKE_ALLOW_STOP, get_session_stop_decision

    try:
        decision = get_session_stop_decision(target)
    except Exception as exc:
        log.error("Stop-decision consult failed before %s wake: target=%s task=%s error=%s",
                  wake_reason, target, task_id, exc)
        return False

    if decision.get("block") is False and decision.get("wake_type") == WAKE_ALLOW_STOP:
        log.info("Suppressed %s wake: target=%s task=%s stop_decision=ALLOW_STOP reason=%s",
                 wake_reason, target, task_id, decision.get("reason"))
        return True
    return False


def _process_handoff_timeouts(r) -> None:
    prefix = os.environ.get("NOTIFY_KEY_PREFIX", "taey")
    seen: set[str] = set()
    for key in r.scan_iter(match=f"{prefix}:handoff:*"):
        parts = str(key).split(":")
        if len(parts) < 4:
            continue
        dispatcher = parts[2]
        if dispatcher in seen:
            continue
        seen.add(dispatcher)

        def _load_task(task_id: str):
            return _load_task_state(task_id) if task_id else None

        def _send(event: dict[str, object]) -> None:
            body = json.dumps(event, separators=(",", ":"))
            _send_wake(
                r,
                dispatcher,
                body,
                priority="high",
                msg_id=f"handoff-wake-{dispatcher}-{event.get('dispatcher_task_id')}",
            )

        try:
            process_expired_handoffs(
                r,
                session_id=dispatcher,
                load_task=_load_task,
                send_wake=_send,
                prefix=prefix,
            )
        except Exception as exc:
            log.error("handoff timeout processing failed for %s: %s", dispatcher, exc)


def _float_or_none(value: object) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _fresh_composer_occupancy(
    r,
    node_id: str,
    *,
    now: float,
    max_age_sec: int,
) -> Optional[dict[str, object]]:
    payload = _decode_queued_message(r.get(state_key(node_id, "composer_occupancy")))
    if payload.get("occupied") is not True:
        return None
    observed_at = _float_or_none(payload.get("observed_at"))
    if observed_at is None:
        return None
    age_sec = max(0.0, now - observed_at)
    if age_sec > max_age_sec:
        return None
    payload["age_sec"] = age_sec
    return payload


def _composer_prompt_text(payload: dict[str, object]) -> str:
    for key in ("composer_text", "text", "content", "excerpt"):
        text = " ".join(str(payload.get(key) or "").split())
        if text:
            return text
    return ""


def _composer_occupancy_fingerprint(payload: dict[str, object]) -> Optional[str]:
    prompt_text = _composer_prompt_text(payload)
    lowered = prompt_text.lower()
    if any(lowered.startswith(prefix) for prefix in COMPOSER_IGNORED_PROMPT_PREFIXES):
        return None
    for key in (
        "content_fingerprint",
        "fingerprint",
        "content_hash",
        "text_hash",
        "excerpt_hash",
    ):
        fingerprint = str(payload.get(key) or "").strip()
        if fingerprint:
            return fingerprint
    if not prompt_text:
        return None
    return hashlib.sha256(prompt_text.encode("utf-8")).hexdigest()


def _failed_activation_record_task_is_terminal(record: dict[str, object]) -> bool:
    task_id = str(record.get("dispatcher_task_id") or "").strip()
    if not task_id:
        return False
    try:
        task = _load_task_state(task_id)
    except Exception as exc:
        log.warning("wedged composer task lookup failed for %s: %s", task_id, exc)
        return False
    if not isinstance(task, dict):
        return False
    return str(task.get("status") or "").strip().lower() in WEDGED_COMPOSER_TERMINAL_TASK_STATUSES


def _delete_failed_activation_handoff_record(r, record: dict[str, object], *, prefix: str) -> None:
    key = str(record.get("_key") or "").strip()
    dispatcher = str(record.get("dispatcher_session_id") or "").strip()
    msg_id = str(record.get("msg_id") or "").strip()
    try:
        if key:
            r.delete(key)
        if dispatcher and msg_id:
            r.srem(handoff_index_key(prefix, dispatcher), msg_id, key)
    except Exception as exc:
        log.warning("failed activation handoff cleanup failed for task=%s msg=%s: %s",
                    record.get("dispatcher_task_id"), msg_id, exc)


def _failed_activation_handoffs(r, *, prefix: str) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for key in r.scan_iter(match=f"{prefix}:handoff:*", count=1000):
        record = _decode_queued_message(r.get(key))
        if record.get("kind") != "explicit_handoff":
            continue
        if str(record.get("activation_state") or "") != "failed":
            continue
        if not record.get("activation_failed_at"):
            continue
        record["_key"] = str(key)
        if _failed_activation_record_task_is_terminal(record):
            _delete_failed_activation_handoff_record(r, record, prefix=prefix)
            continue
        records.append(record)
    return records


def _wedged_composer_transition_key(target: str) -> str:
    return orch_key("wedged-composer-liveness", target or "unknown")


def _wedged_composer_candidate_key(target: str) -> str:
    return orch_key("wedged-composer-candidate", target or "unknown")


def _wedged_composer_candidate_ttl_sec(freshness_sec: int) -> int:
    stability_window = _int_env(
        "ORCH_WEDGED_COMPOSER_STABILITY_WINDOW_SEC",
        DEFAULT_WEDGED_COMPOSER_STABILITY_WINDOW_SEC,
    )
    stability_window = max(
        DEFAULT_WEDGED_COMPOSER_STABILITY_WINDOW_SEC,
        int(stability_window),
    )
    return max(1, int(freshness_sec), stability_window)


def _composer_candidate_matches(r, target: str, fingerprint: str, *, current_time: float,
                                ttl_sec: int) -> bool:
    key = _wedged_composer_candidate_key(target)
    existing = _decode_queued_message(r.get(key))
    matched = str(existing.get("fingerprint") or "") == fingerprint
    payload = {
        "fingerprint": fingerprint,
        "observed_at": current_time,
    }
    candidate_ttl_sec = _wedged_composer_candidate_ttl_sec(ttl_sec)
    try:
        r.set(key, json.dumps(payload, separators=(",", ":")), ex=candidate_ttl_sec)
    except TypeError:
        r.set(key, json.dumps(payload, separators=(",", ":")))
    return matched


def _clear_wedged_composer_candidate(r, target: str) -> None:
    try:
        r.delete(_wedged_composer_candidate_key(target))
    except Exception as exc:
        log.debug("wedged composer candidate clear failed for %s: %s", target, exc)


def _supervisor_for_failed_activation(r, target: str, record: dict[str, object]) -> Optional[str]:
    current = get_current_task(r, target)
    task = dict(current) if isinstance(current, dict) else {}
    dispatcher = str(record.get("dispatcher_session_id") or "").strip()
    if dispatcher:
        task.setdefault("dispatcher", dispatcher)
        task.setdefault("supervisor", dispatcher)
    return resolve_supervisor(r, target, task)


def _process_wedged_composer_liveness(
    r,
    dedup_ttl_sec: int,
    *,
    max_age_sec: Optional[int] = None,
    rearm_sec: Optional[int] = None,
) -> int:
    del dedup_ttl_sec
    prefix = os.environ.get("NOTIFY_KEY_PREFIX", "taey")
    current_time = _redis_now(r)
    freshness = max(1, int(
        max_age_sec
        if max_age_sec is not None
        else _int_env("ORCH_COMPOSER_OCCUPANCY_MAX_AGE_SEC",
                      DEFAULT_COMPOSER_OCCUPANCY_MAX_AGE_SEC)
    ))
    rearm = max(1, int(
        rearm_sec
        if rearm_sec is not None
        else _int_env("ORCH_WEDGED_COMPOSER_REARM_SEC",
                      DEFAULT_WEDGED_COMPOSER_REARM_SEC)
    ))
    sent = 0
    seen_targets: set[str] = set()
    for record in _failed_activation_handoffs(r, prefix=prefix):
        target = str(record.get("target_session_id") or "").strip()
        if not target or target in seen_targets:
            continue
        seen_targets.add(target)
        occupancy = _fresh_composer_occupancy(
            r,
            target,
            now=current_time,
            max_age_sec=freshness,
        )
        if not occupancy:
            _clear_wedged_composer_candidate(r, target)
            continue
        fingerprint = _composer_occupancy_fingerprint(occupancy)
        if not fingerprint:
            _clear_wedged_composer_candidate(r, target)
            continue
        transition_key = _wedged_composer_transition_key(target)
        if r.exists(transition_key):
            continue
        if not _composer_candidate_matches(r, target, fingerprint,
                                           current_time=current_time,
                                           ttl_sec=freshness):
            continue
        supervisor = _supervisor_for_failed_activation(r, target, record)
        if not supervisor:
            log.warning("wedged composer liveness could not resolve supervisor for %s", target)
            continue
        task_id = str(record.get("dispatcher_task_id") or "?")
        msg_id = str(record.get("msg_id") or "?")
        age = int(float(occupancy.get("age_sec") or 0))
        machine = str(occupancy.get("machine") or "unknown")
        body = (
            f"[WEDGED_COMPOSER] {target} has dispatch_activation_failed for task={task_id} "
            f"while its composer is still non-empty (observed {age}s ago on {machine}). "
            "Treat this peer as WEDGED, not idle; inspect the tmux pane before redispatching "
            "or clearing any possible outward action."
        )
        body += f" handoff_msg_id={msg_id}."
        if _send_wake(
            r,
            supervisor,
            body,
            priority="high",
            msg_id=f"orch-watch-wedged-composer-{target}-{msg_id}-{int(current_time)}",
        ):
            payload = {
                "target_session_id": target,
                "dispatcher_task_id": task_id,
                "msg_id": msg_id,
                "activation_failed_at": record.get("activation_failed_at"),
                "occupancy_observed_at": occupancy.get("observed_at"),
                "alerted_at": current_time,
            }
            r.set(transition_key, json.dumps(payload, separators=(",", ":")), ex=rearm)
            sent += 1
            log.info("Sent WEDGED_COMPOSER wake: supervisor=%s worker=%s task=%s",
                     supervisor, target, task_id)
    return sent


def _stop_gate_dedup(r, node_id: str, current_task_id: str, decision_key: str,
                     ttl_sec: int = 600) -> bool:
    try:
        return bool(r.set(
            orch_key("orch-stop-gate", node_id, current_task_id, decision_key),
            "1",
            nx=True,
            ex=ttl_sec,
        ))
    except Exception as exc:
        log.error("stop-gate dedup failed for %s/%s/%s: %s",
                  node_id, current_task_id, decision_key, exc)
        return True


def _resolve_affected_product(node_id: str, project_id: Optional[str]) -> Optional[str]:
    from fleet_orchestrator.dispatch import _resolve_product_id

    product_id = _resolve_product_id(node_id)
    if product_id:
        return product_id
    if project_id and project_id != "default":
        return project_id
    return None


def _supervisor_peer_work_requires_action(node_id: str,
                                          project_context: Optional[Dict[str, object]]) -> bool:
    project_id = (project_context or {}).get("project_id")
    if not isinstance(project_id, str) or not project_id:
        return False
    from fleet_orchestrator.config import OrchConfig
    from fleet_orchestrator.orch_schema import (
        get_supervisor_dispatchable_peer_task,
        get_supervisor_inflight_peer_task,
    )

    cfg = OrchConfig()
    return bool(
        get_supervisor_dispatchable_peer_task(node_id, project_id, config=cfg)
        or get_supervisor_inflight_peer_task(node_id, project_id, config=cfg)
    )


def _evaluate_user_stop_conditions(r, node_id: str, task_state: dict,
                                   project_context: Optional[Dict[str, object]]) -> tuple[Optional[str], Optional[dict]]:
    from fleet_orchestrator.plan_readiness import next_ready_for_session

    task_id = task_state.get("id") or task_state.get("task_id")
    project_id = (project_context or {}).get("project_id")
    stop_conditions = list((project_context or {}).get("user_stop_conditions") or [])
    next_ready = next_ready_for_session(
        node_id,
        exclude_task_id=task_id,
        project_id=project_id,
    )

    for condition in stop_conditions:
        if condition == "stop_when_all_ready_tasks_dispatched":
            if next_ready is None and not _supervisor_peer_work_requires_action(node_id, project_context):
                return condition, next_ready
        elif condition == "stop_when_user_explicit_decision_required_and_pending":
            if r.get(state_key(node_id, "awaiting_user_decision")):
                return condition, next_ready
        elif condition == "stop_when_blocked_on_external_signal_and_named":
            if ((task_state.get("blocked_on") or "").strip()):
                return condition, next_ready
        elif condition == "stop_when_production_stop_active_on_affected_product":
            product_id = _resolve_affected_product(node_id, project_id if isinstance(project_id, str) else None)
            if product_id and r.get(f"support:product:{product_id}:bug_lock") == "true":
                return condition, next_ready

    return None, next_ready


def _handle_user_stop_gate(r, node_id: str, task: dict) -> bool:
    task_id = task.get("task_id")
    if not task_id:
        return False

    task_state = _load_task_state(task_id)
    if not task_state:
        log.warning("Stop gate skipped for %s: task %s not found in OrchTask graph",
                    node_id, task_id)
        return False
    from fleet_orchestrator.orch_schema import (
        _blocked_on_has_live_resolver,
        is_awaiting_human_review_gate,
        is_declared_await_signal,
    )
    if is_awaiting_human_review_gate(task_state):
        log.info("Suppressed stop-gate wake: session=%s task=%s awaiting human review",
                 node_id, task_id)
        return True
    if task_state.get("status") != "in_progress":
        return False
    blocked_on = ((task_state.get("blocked_on") or "").strip())
    if blocked_on:
        if is_declared_await_signal(blocked_on):
            log.info("Suppressed stop-gate wake: session=%s task=%s awaiting signal=%s",
                     node_id, task_id, blocked_on)
            return True
        try:
            if _blocked_on_has_live_resolver(blocked_on, current_task_id=task_id):
                log.info("Suppressed stop-gate wake: session=%s task=%s live resolver=%s",
                         node_id, task_id, blocked_on)
                return True
        except Exception as exc:
            log.warning("Could not validate blocked_on for stop-gate wake: task=%s blocked_on=%s error=%s",
                        task_id, blocked_on, exc)
        return False

    project_context = _task_project_context(task_id)
    matched_condition, next_ready = _evaluate_user_stop_conditions(
        r, node_id, task_state, project_context
    )

    if matched_condition:
        # User stop conditions are project state. task.blocked_on is reserved for resolvable
        # task ids or structured AWAIT markers; storing the condition label there deadlocks.
        log.info("Suppressed stop-gate wake: session=%s task=%s user_stop_condition=%s",
                 node_id, task_id, matched_condition)
        return True

    if next_ready:
        next_task_id = next_ready.get("task_id", "?")
        if not _stop_gate_dedup(r, node_id, task_id, f"continue:{next_task_id}"):
            return True
        body = (
            f"[AUTO_CONTINUE] You stopped while task={task_id} remains in_progress with no matching "
            f"user stop condition. The next ready task for you is: {next_task_id} — "
            f"{(next_ready.get('description') or '')[:120]}. Continue execution instead of stopping."
        )
        if _send_wake(
            r,
            node_id,
            body,
            priority="high",
            msg_id=f"orch-watch-stop-gate-continue-{node_id}-{task_id}-{next_task_id}",
        ):
            log.info("Sent AUTO_CONTINUE wake: session=%s task=%s next_task=%s",
                     node_id, task_id, next_task_id)
        return True

    if not _stop_gate_dedup(r, node_id, task_id, "clarify"):
        return True
    body = (
        f"[CLARIFY_INTENT] You stopped while task={task_id} remains in_progress, no user stop "
        f"condition matched, and no other ready work exists. Please clarify intent or set blocked_on "
        f"before stopping again."
    )
    if _send_wake(
        r,
        node_id,
        body,
        priority="high",
        msg_id=f"orch-watch-stop-gate-clarify-{node_id}-{task_id}",
    ):
        log.info("Sent CLARIFY_INTENT wake: session=%s task=%s",
                 node_id, task_id)
    return True


def _build_peer_idle_body(r, supervisor: str, node_id: str,
                          task: dict, outcome: str,
                          details: str, duration_sec: int) -> str:
    from fleet_orchestrator.plan_readiness import next_ready_for_session

    task_id = task.get("task_id", "?")
    desc = (task.get("description") or "")[:120]
    body = (
        f"[PEER_IDLE] {node_id} stopped — outcome={outcome}; "
        f"task={task_id}; \"{desc}\"; duration={duration_sec}s."
    )
    if details:
        body += f" details={details[:200]}."

    next_task = next_ready_for_session(supervisor, exclude_task_id=task_id)
    if next_task:
        next_desc = (next_task.get("description") or "")[:120]
        return (
            f"{body} While waiting on {task_id}, the next ready task for you is: "
            f"{next_task['task_id']} — {next_desc}. Pick this up to make progress."
        )

    return (
        f"{body} No other ready work is available right now. "
        f"Confirm you're correctly waiting on this task's unblock signal before you stop again."
    )


def notify_supervisor_of_stuck(r, supervisor: str, node_id: str,
                                task: dict, stuck_for_sec: int,
                                dedup_ttl_sec: int) -> bool:
    """Push a single 'stuck task' alert to the supervisor. Returns True
    if the message was sent, False if deduped or unable.

    Audit fix: dedup is keyed per ``(node_id, task_id)`` not just
    node_id. Otherwise a worker that alerted stuck on task-A, got
    re-dispatched, then stalled on task-B is silent for the dedup TTL.
    The unblock path already keys correctly; this makes stuck consistent.
    """
    task_id = task.get("task_id", "?")
    dedup_key = orch_key("orch-watch-stuck", node_id, task_id)
    if r.exists(dedup_key):
        return False

    last_outcome = get_last_outcome(r, node_id) or {"outcome": "unknown"}
    outcome = last_outcome.get("outcome", "unknown")
    details = last_outcome.get("details", "")
    task_id = task.get("task_id", "?")
    task_state = _load_task_state(task_id) if task_id != "?" else None
    blocked_on = ((task_state or {}).get("blocked_on") or "").strip()
    if blocked_on:
        from fleet_orchestrator.orch_schema import (
            _blocked_on_has_live_resolver,
            is_awaiting_human_review_gate,
            is_declared_await_signal,
        )
        if is_awaiting_human_review_gate(task_state) or is_declared_await_signal(blocked_on):
            log.info("Suppressed PEER_IDLE wake: supervisor=%s worker=%s task=%s blocked_on=%s",
                     supervisor, node_id, task_id, blocked_on)
            return False
        try:
            if _blocked_on_has_live_resolver(blocked_on, current_task_id=task_id):
                log.info("Suppressed PEER_IDLE wake: supervisor=%s worker=%s task=%s resolver=%s",
                         supervisor, node_id, task_id, blocked_on)
                return False
        except Exception as exc:
            log.warning("Could not validate blocked_on for PEER_IDLE wake: task=%s blocked_on=%s error=%s",
                        task_id, blocked_on, exc)

    try:
        started_at = float(task.get("started_at", 0) or 0)
    except Exception:
        started_at = 0.0
    duration_sec = int(max(0.0, _redis_now(r) - started_at)) if started_at > 0 else stuck_for_sec
    if _target_stop_decision_allows_stop(supervisor, "PEER_IDLE", task_id):
        return False
    body = _build_peer_idle_body(r, supervisor, node_id, task, outcome, details, duration_sec)

    if not _send_wake(
        r,
        supervisor,
        body,
        priority="high",
        msg_id=f"orch-watch-peer-idle-{node_id}-{int(time.time())}",
    ):
        return False

    r.set(dedup_key, "1", ex=dedup_ttl_sec)
    log.info("Sent PEER_IDLE wake: supervisor=%s worker=%s task=%s stuck_for=%ss",
             supervisor, node_id, task_id, stuck_for_sec)
    return True


def notify_supervisor_of_unblock(r, supervisor: str, completed_task: dict,
                                  message: str, dedup_ttl_sec: int) -> bool:
    """Push a single unblock-wake to the supervisor when a worker's
    done-DEL unblocked their owned OrchTask. ``message`` is the body the
    readiness checker returned. Returns True if sent, False if deduped."""
    task_id = completed_task.get("task_id", "?")
    dedup_key = orch_key("orch-watch-unblock", supervisor, task_id)
    if r.exists(dedup_key):
        return False

    if _target_stop_decision_allows_stop(supervisor, "UNBLOCK", task_id):
        return False

    if not _send_wake(
        r,
        supervisor,
        message,
        priority="normal",
        msg_id=f"orch-watch-unblock-{supervisor}-{task_id}-{int(time.time())}",
    ):
        return False

    r.set(dedup_key, "1", ex=dedup_ttl_sec)
    log.info("Sent UNBLOCK wake: supervisor=%s completed_task=%s",
             supervisor, task_id)
    return True


def _process_worker_liveness_expirations(
    r,
    dedup_ttl_sec: int,
    *,
    task_id_prefix: Optional[str] = None,
    project_id_prefix: Optional[str] = None,
) -> int:
    from fleet_orchestrator.worker_liveness import (
        escalate_stale_worker_tasks,
        worker_task_liveness_dedup_key,
    )

    escalated = escalate_stale_worker_tasks(
        task_id_prefix=task_id_prefix,
        project_id_prefix=project_id_prefix,
    )
    sent = 0
    for task in escalated:
        task_id = task.get("task_id")
        supervisor = task.get("supervisor") or task.get("project_supervisor")
        worker = task.get("worker")
        if not task_id or not supervisor:
            continue
        dedup_key = worker_task_liveness_dedup_key(str(task_id))
        if r.exists(dedup_key):
            continue
        if _target_stop_decision_allows_stop(str(supervisor), "WORKER_LIVENESS_EXPIRED", str(task_id)):
            continue
        expired_blocked_on = str(task.get("expired_blocked_on") or "").strip()
        body = (
            f"[WORKER_LIVENESS_EXPIRED] task={task_id} assigned to {worker} "
            f"had no task-keyed heartbeat for {task.get('stale_for_sec', '?')}s. "
            "It has been returned to pending with needs_attention=true so it can be "
            "redispatched or investigated; it is no longer an indefinite in_progress stall."
        )
        if expired_blocked_on:
            body += (
                f" The cleared blocked_on value was free text: {expired_blocked_on!r}. "
                "Free-text blocked_on is informational only and does NOT exempt worker-liveness. "
                "Model an intentional cross-session or external wait with "
                "`--blocked-on AWAIT:external-signal:<detail>`; that structured marker is "
                "machine-resolvable and the executor/supervisor clears it when the external work lands."
            )
        if _send_wake(
            r,
            str(supervisor),
            body,
            priority="high",
            msg_id=f"orch-watch-worker-liveness-{supervisor}-{task_id}-{int(time.time())}",
        ):
            r.set(dedup_key, "1", ex=dedup_ttl_sec)
            sent += 1
            log.info("Sent WORKER_LIVENESS_EXPIRED wake: supervisor=%s worker=%s task=%s",
                     supervisor, worker, task_id)
    return sent


def _process_task_reconciliations(
    r,
    dedup_ttl_sec: int,
    *,
    task_id_prefix: Optional[str] = None,
    project_id_prefix: Optional[str] = None,
) -> int:
    from fleet_orchestrator.task_reconciliation import reconcile_stale_tasks

    result = reconcile_stale_tasks(
        task_id_prefix=task_id_prefix,
        project_id_prefix=project_id_prefix,
    )
    sent = 0
    for task in result.get("reconciled", []):
        task_id = str(task.get("task_id") or "").strip()
        supervisor = str(task.get("supervisor") or "").strip()
        if not task_id or not supervisor:
            continue
        dedup_key = orch_key("task-reconciled", supervisor, task_id)
        if r.exists(dedup_key):
            continue
        kind = str(task.get("reconciliation_kind") or "stale_task")
        if _target_stop_decision_allows_stop(supervisor, "TASK_RECONCILED", task_id):
            log.info("Reconciled stale task without wake: supervisor=%s task=%s kind=%s",
                     supervisor, task_id, kind)
            r.set(dedup_key, "1", ex=dedup_ttl_sec)
            continue
        body = (
            f"[TASK_RECONCILED] task={task_id} kind={kind} status={task.get('status')} "
            f"reason={task.get('reason')}. Re-check current/next work; this stale task "
            "no longer counts as live work."
        )
        if _send_wake(
            r,
            supervisor,
            body,
            priority="normal",
            msg_id=f"orch-watch-task-reconciled-{supervisor}-{task_id}-{int(time.time())}",
        ):
            r.set(dedup_key, "1", ex=dedup_ttl_sec)
            sent += 1
            log.info("Sent TASK_RECONCILED wake: supervisor=%s task=%s kind=%s",
                     supervisor, task_id, kind)
    return sent


def _process_idle_owner_graph_work(
    r,
    dedup_ttl_sec: int,
    *,
    task_id_prefix: Optional[str] = None,
) -> int:
    from fleet_orchestrator.orch_schema import (
        WAKE_ALLOW_STOP,
        get_session_stop_decision,
        list_self_owned_in_progress_sessions,
    )

    sent = 0
    for candidate in list_self_owned_in_progress_sessions(task_id_prefix=task_id_prefix):
        owner = str(candidate.get("session_id") or "").strip()
        if not owner:
            continue
        if not r.get(state_key(owner, "idle")):
            continue
        if r.get(state_key(owner, "current_task")):
            continue
        try:
            decision = get_session_stop_decision(owner)
        except Exception as exc:
            log.error("owner graph-work stop-decision failed: owner=%s task=%s error=%s",
                      owner, candidate.get("task_id"), exc)
            continue
        if decision.get("block") is False and decision.get("wake_type") == WAKE_ALLOW_STOP:
            continue
        if not decision.get("block"):
            continue

        task_id = str(decision.get("task_id") or candidate.get("task_id") or "unknown-task")
        dedup_key = orch_key("orch-watch-owner-graph-work", owner, task_id)
        if r.exists(dedup_key):
            continue
        reason = str(decision.get("reason") or "").strip()
        if not reason:
            reason = (
                f"WAKE: you own in-progress task={candidate.get('task_id')} in the "
                "tracker and are idle with no Redis current_task binding. Re-check "
                "`taey-plan current` / `taey-plan next` and continue."
            )
        if _send_wake(
            r,
            owner,
            reason,
            priority="high",
            msg_id=f"orch-watch-owner-graph-work-{owner}-{task_id}-{int(time.time())}",
        ):
            r.set(dedup_key, "1", ex=dedup_ttl_sec)
            sent += 1
            log.info("Sent OWNER_GRAPH_WORK wake: owner=%s task=%s", owner, task_id)
    return sent


def handle_done_del(r, node_id: str, snapshot: dict,
                     readiness_checker, dedup_ttl_sec: int) -> None:
    """A worker's current_task was DEL'd. Distinguish done-clear (Stop
    hook on outcome=done) from force-clear (supervisor's clear_current_
    task or any other administrative wipe), and run the readiness checker
    ONLY for done-clear.

    Audit fix: the Stop hook
    writes ``taey:<node>:last_clear_was_done`` (30s TTL marker) right
    when its compare-and-swap clear succeeds. Read that marker here; if
    absent, the DEL was NOT a done-clear (could be force-clear, expiry,
    administrative wipe) and we skip the readiness check. Without this
    check, force-clearing an errored task would spuriously fire
    unblock-wakes as if the task had completed.
    """
    if not readiness_checker:
        log.debug("done-DEL on %s but no --readiness-checker configured; skipping.",
                  node_id)
        return

    done_marker = r.get(state_key(node_id, "last_clear_was_done"))
    if not done_marker:
        log.debug("DEL on %s but no done-marker — treating as force-clear, "
                  "skipping readiness check.", node_id)
        return
    # Consume the marker so we don't double-fire if the same DEL event
    # arrives twice (rare but possible under PSUBSCRIBE+sweep overlap).
    r.delete(state_key(node_id, "last_clear_was_done"))

    completed_task = snapshot.get("task")
    supervisor = snapshot.get("supervisor") or resolve_supervisor(r, node_id)
    if not supervisor or not completed_task:
        return

    # Only wake if the supervisor is currently idle. If they're working,
    # they'll see the unblocked task on their next pull (taey-plan next).
    if not r.get(state_key(supervisor, "idle")):
        log.debug("Supervisor %s not idle; skip unblock wake.", supervisor)
        return

    try:
        message = readiness_checker(supervisor, completed_task)
    except Exception as exc:
        log.error("readiness_checker raised for supervisor=%s task=%s: %s",
                  supervisor, completed_task.get("task_id"), exc)
        return

    if not message:
        return

    notify_supervisor_of_unblock(r, supervisor, completed_task,
                                  message, dedup_ttl_sec)


# Memory of in-flight tasks per node so we can react to current_task DEL
# events with the task content (which is gone from Redis by the time we
# see the event). Refreshed on every SET we observe + by the periodic
# sweep. Stale entries do no harm — they're only consulted on DEL.
_TASK_SNAPSHOTS: dict = {}


def _redis_now(r) -> float:
    """Return Redis-server's current unix time as a float. Use this in
    place of ``time.time()`` for any duration calculation that compares
    timestamps written by other nodes / processes. Client-side clock skew across multi-host
    fleets can fire stuck-alerts seconds early or late depending on which
    node wrote the timestamp).
    """
    try:
        sec, usec = r.time()
        return float(sec) + float(usec) / 1_000_000.0
    except Exception:
        return time.time()


def investigate(r, node_id: str, event_type: str,
                stuck_threshold_sec: int, dedup_ttl_sec: int,
                readiness_checker) -> None:
    """Canonical source for what 'stuck' and 'unblocked' mean.
    Both the PSUBSCRIBE event path and the periodic sweep call this.

    ``event_type`` is one of:
      - ``current_task_set`` / ``current_task_del``
      - ``idle_set`` / ``idle_del``
      - ``last_activity_set``
      - ``sweep`` (called from periodic poll, treats node as a generic check)

    Audit fix: skip snapshot
    refresh on ``current_task_del`` events. Otherwise a SET-then-DEL race
    where a fresh dispatch and a Stop-hook-clear arrive close in time
    causes the refresh to overwrite the snapshot with the NEW task right
    before the DEL handler reads it — spurious unblock-wake on the in-
    flight task, and the real new-task completion gets missed.
    """
    if event_type != "current_task_del":
        task = get_current_task(r, node_id)
        if task:
            _TASK_SNAPSHOTS[node_id] = {
                "task": task,
                "supervisor": resolve_supervisor(r, node_id, task),
            }
    else:
        task = None  # The key is gone; DEL handler uses the snapshot.

    # Done-DEL handler: a current_task just disappeared. The handle_done_del
    # function will check the done-marker key to distinguish Stop-hook
    # done-clear from supervisor force-clear.
    if event_type == "current_task_del" and node_id in _TASK_SNAPSHOTS:
        snapshot = _TASK_SNAPSHOTS.pop(node_id, None)
        if snapshot:
            handle_done_del(r, node_id, snapshot, readiness_checker, dedup_ttl_sec)
        return

    # Stuck-task check: worker idle + current_task present + idle-time
    # over threshold.
    if not task:
        return

    idle = r.get(state_key(node_id, "idle"))
    if not idle:
        return

    last_outcome = get_last_outcome(r, node_id)
    if last_outcome and last_outcome.get("outcome") == "done":
        # Grace window: the Stop hook's CAS done-clear is async with
        # respect to orch-watch's keyspace event arrival. The hook fires
        # action_stop() which (a) sets idle=1 -> emits a keyspace event,
        # (b) reads current_task + last_outcome, (c) runs the Lua
        # compare-and-swap clear. orch-watch can see (a)'s event and
        # call investigate() BEFORE (c) lands — meaning current_task is
        # still set, last_outcome is already done, but the clear is
        # genuinely in flight. Warning at that point is noise.
        # Skip the warning if last_activity is within the last 10s
        # (the CAS clear should have landed by then).
        try:
            la = float(r.get(state_key(node_id, "last_activity")) or 0)
        except Exception:
            la = 0
        if _redis_now(r) - la > 10:
            log.warning("State drift on %s: current_task set but outcome=done "
                        "(activity %ds ago — past grace window). Stop hook should "
                        "have cleared.", node_id, int(_redis_now(r) - la))
        return

    if event_type in {"idle_set", "sweep"} and _handle_user_stop_gate(r, node_id, task):
        return

    # Audit fix #3 + fix-of-fix: a task cannot be "stuck" longer
    # than it has existed. The original v0.2.0 used time-since-dispatch
    # (task.started_at), which over-fires when a worker is busy 4min then
    # idle 30s. v0.2.1 switched to time-since-last-activity, which under-
    # fires the opposite way: a fresh dispatch on a long-idle worker is
    # flagged stuck immediately even though the task is brand new.
    #
    # The right invariant: stuck_for = max-of-{time-since-dispatch,
    # time-since-last-activity}. The task is stuck only when BOTH
    # (a) it has existed long enough AND (b) the worker hasn't done
    # anything since. Using max means a fresh dispatch on a long-idle
    # worker gets its own threshold window before being flagged; a long-
    # standing dispatch on a recently-active worker waits for the
    # activity-since-dispatch gap to grow past threshold.
    #
    # Equivalent expression: stuck_for = now - max(started_at, last_activity).
    # Compared to Redis-server time so cross-host clock skew is bounded.
    last_activity_raw = r.get(state_key(node_id, "last_activity"))
    try:
        last_activity = float(last_activity_raw) if last_activity_raw else 0.0
    except Exception:
        last_activity = 0.0
    try:
        started_at = float(task.get("started_at", 0))
    except Exception:
        started_at = 0.0

    boundary = max(started_at, last_activity)
    if boundary <= 0:
        return  # No reliable boundary timestamp — skip rather than false-fire.

    stuck_for = int(_redis_now(r) - boundary)
    if stuck_for < stuck_threshold_sec:
        return

    supervisor = resolve_supervisor(r, node_id, task)
    if not supervisor:
        log.debug("No supervisor for %s; skip stuck alert.", node_id)
        return

    notify_supervisor_of_stuck(r, supervisor, node_id, task,
                                stuck_for, dedup_ttl_sec)


def load_readiness_checker(spec):
    """Resolve --readiness-checker spec ``path/to/file.py:function_name``
    or ``module.path:function_name`` to a callable."""
    if not spec:
        return None
    if ":" not in spec:
        raise ValueError(f"--readiness-checker must be 'path:function', got {spec!r}")
    module_spec, func_name = spec.rsplit(":", 1)
    if os.path.isfile(module_spec):
        import importlib.util
        mod_name = os.path.splitext(os.path.basename(module_spec))[0]
        spec_obj = importlib.util.spec_from_file_location(mod_name, module_spec)
        module = importlib.util.module_from_spec(spec_obj)
        spec_obj.loader.exec_module(module)
    else:
        import importlib
        module = importlib.import_module(module_spec)
    return getattr(module, func_name)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--redis-host", default=os.environ.get("REDIS_HOST", "127.0.0.1"))
    parser.add_argument("--redis-port", type=int,
                        default=int(os.environ.get("REDIS_PORT", "6379")))
    parser.add_argument("--stuck-threshold-sec", type=int, default=300,
                        help="Idle + unresolved current_task for this long → stuck alert.")
    parser.add_argument("--dedup-ttl-sec", type=int, default=3600,
                        help="Don't re-fire the same alert (stuck or unblock) within this window.")
    parser.add_argument("--sweep-interval-sec", type=int, default=1800,
                        help="Periodic safety-net sweep across all current_task keys "
                             "(default 30 min: keyspace "
                             "notifications are best-effort/at-most-once; the poll is "
                             "the reliability backstop).")
    parser.add_argument("--readiness-checker", default=None,
                        help="Module:function for the done-DEL readiness checker. "
                             "Spec format: '/path/to/file.py:check_readiness' or "
                             "'package.module:check_readiness'. If unset, done-DEL "
                             "events are logged and skipped.")
    parser.add_argument("--notify-daemon-watchdog", dest="notify_daemon_watchdog",
                        action=argparse.BooleanOptionalAction,
                        default=_bool_env("ORCH_NOTIFY_DAEMON_WATCHDOG", True),
                        help="Watch conductor-notify-router plus fleet-notify heartbeat and delivery SLO.")
    parser.add_argument("--notify-daemon-watch-interval-sec", type=int,
                        default=_int_env("ORCH_NOTIFY_DAEMON_WATCH_INTERVAL_SEC",
                                         DEFAULT_NOTIFY_DAEMON_WATCH_INTERVAL_SEC),
                        help="Notify-daemon watchdog cadence.")
    parser.add_argument("--notify-daemon-heartbeat-max-age-sec", type=int,
                        default=_int_env("ORCH_NOTIFY_DAEMON_HEARTBEAT_MAX_AGE_SEC",
                                         DEFAULT_NOTIFY_DAEMON_HEARTBEAT_MAX_AGE_SEC),
                        help="Maximum acceptable age for the fleet-notify daemon heartbeat.")
    parser.add_argument("--notify-daemon-stuck-inbox-max-age-sec", type=int,
                        default=_int_env("ORCH_NOTIFY_DAEMON_STUCK_INBOX_MAX_AGE_SEC",
                                         DEFAULT_STUCK_INBOX_MAX_AGE_SEC),
                        help="Maximum acceptable age for queued notify inbox messages.")
    parser.add_argument("--notify-router-service",
                        default=os.environ.get("ORCH_NOTIFY_ROUTER_SERVICE", DEFAULT_NOTIFY_ROUTER_SERVICE),
                        help="systemd --user service name for the notify router.")
    parser.add_argument("--notify-daemon-alert-target",
                        default=os.environ.get("ORCH_NOTIFY_DAEMON_ALERT_TARGET",
                                               DEFAULT_NOTIFY_DAEMON_ALERT_TARGET),
                        help="tmux session that receives direct OOB critical alerts.")
    args = parser.parse_args()

    r = redis_lib.Redis(host=args.redis_host, port=args.redis_port,
                        decode_responses=True, socket_timeout=10)
    r.ping()

    # Verify keyspace notifications are enabled. If not, set them ourselves
    # (the installer should make this permanent via CONFIG REWRITE).
    cfg = r.config_get("notify-keyspace-events").get("notify-keyspace-events", "")
    needed = set("Kgl$")
    if not needed.issubset(set(cfg)):
        # F17: UNION our required flags with whatever is already configured -- never
        # clobber. notify-keyspace-events is a single shared Redis-wide setting; other
        # consumers may rely on flags we don't ('A', 'E', 'x', ...). Writing a bare 'Kgl$'
        # would silently break them. Preserve the existing flags and add only the missing.
        merged = "".join(sorted(set(cfg) | needed))
        log.warning("notify-keyspace-events=%r is missing required flags %r; "
                    "unioning to %r (run CONFIG REWRITE to persist).", cfg, "".join(sorted(needed)), merged)
        r.config_set("notify-keyspace-events", merged)

    readiness_checker = load_readiness_checker(args.readiness_checker)
    if readiness_checker:
        log.info("Loaded readiness checker: %s", args.readiness_checker)

    # Bootstrap _TASK_SNAPSHOTS so done-DEL events for already-existing
    # current_task keys have content to react to (the daemon may start
    # mid-dispatch).
    for k in r.scan_iter(match=current_task_scan_pattern()):
        node = node_from_current_task_key(k)
        if node:
            task = get_current_task(r, node)
            if task:
                _TASK_SNAPSHOTS[node] = {
                    "task": task,
                    "supervisor": resolve_supervisor(r, node, task),
                }

    pubsub = r.pubsub()
    pubsub.psubscribe(*SUBSCRIBE_PATTERNS)
    log.info("Started: subscribed to %s; stuck_threshold=%ss dedup_ttl=%ss sweep=%ss",
             SUBSCRIBE_PATTERNS, args.stuck_threshold_sec, args.dedup_ttl_sec,
             args.sweep_interval_sec)

    # Audit fix for the sweep reliability gap:
    # the sweep MUST be able to fire even when no keyspace events arrive.
    # The previous implementation embedded the sweep check inside
    # ``for message in pubsub.listen():`` — a blocking generator that
    # yields nothing in a quiet fleet, so the sweep block never ran after
    # bootstrap. That defeats the explicit "best-effort PSUBSCRIBE +
    # poll backstop" design. The fix is to
    # use ``get_message(timeout=N)`` in a manual loop so we get unblocked
    # every N seconds and can run the sweep regardless of event volume.
    last_sweep = time.time()
    # Poll interval bounded by min(60s, sweep/4) so even rapid sweep
    # configs see the loop tick fast enough.
    poll_timeout = min(60.0, max(5.0, args.sweep_interval_sec / 4))
    if args.notify_daemon_watchdog:
        poll_timeout = min(
            poll_timeout,
            max(1.0, args.notify_daemon_watch_interval_sec / 4),
        )
    last_notify_daemon_watch = 0.0
    last_wedged_composer_liveness = 0.0
    wedged_composer_liveness_interval = min(60.0, max(1.0, poll_timeout))

    while True:
        try:
            message = pubsub.get_message(
                ignore_subscribe_messages=True, timeout=poll_timeout
            )
        except Exception as exc:
            log.error("pubsub.get_message error: %s; retry in 1s", exc)
            time.sleep(1.0)
            continue

        now = time.time()
        if now - last_wedged_composer_liveness >= wedged_composer_liveness_interval:
            last_wedged_composer_liveness = now
            try:
                _process_wedged_composer_liveness(r, args.dedup_ttl_sec)
            except Exception as exc:
                log.error("wedged composer liveness check failed: %s", exc)

        if (args.notify_daemon_watchdog
                and now - last_notify_daemon_watch >= args.notify_daemon_watch_interval_sec):
            last_notify_daemon_watch = now
            try:
                check_notify_daemon_liveness(
                    r,
                    heartbeat_max_age_sec=args.notify_daemon_heartbeat_max_age_sec,
                    service_name=args.notify_router_service,
                    alert_target=args.notify_daemon_alert_target,
                )
                check_stuck_inbox_delivery(
                    r,
                    max_age_sec=args.notify_daemon_stuck_inbox_max_age_sec,
                    alert_target=args.notify_daemon_alert_target,
                )
            except (redis_lib.RedisError, OSError, subprocess.SubprocessError, TypeError, ValueError) as exc:
                log.error("notify-daemon watchdog check failed: %s", exc)

        if now - last_sweep > args.sweep_interval_sec:
            last_sweep = now
            sweep_count = 0
            try:
                _process_handoff_timeouts(r)
                _process_wedged_composer_liveness(r, args.dedup_ttl_sec)
                _process_task_reconciliations(r, args.dedup_ttl_sec)
                _process_worker_liveness_expirations(r, args.dedup_ttl_sec)
                _process_idle_owner_graph_work(r, args.dedup_ttl_sec)
                for k in r.scan_iter(match=current_task_scan_pattern()):
                    node = node_from_current_task_key(k)
                    if node:
                        investigate(r, node, "sweep",
                                    args.stuck_threshold_sec,
                                    args.dedup_ttl_sec,
                                    readiness_checker)
                        sweep_count += 1
                log.debug("Sweep checked %d current_task keys.", sweep_count)
            except Exception as exc:
                log.error("Sweep failed: %s", exc)

        if not message or message.get("type") not in ("pmessage", "message"):
            try:
                _process_handoff_timeouts(r)
                _process_wedged_composer_liveness(r, args.dedup_ttl_sec)
                _process_task_reconciliations(r, args.dedup_ttl_sec)
                _process_worker_liveness_expirations(r, args.dedup_ttl_sec)
                _process_idle_owner_graph_work(r, args.dedup_ttl_sec)
            except Exception as exc:
                log.error("background liveness pass failed: %s", exc)
            continue

        channel = message.get("channel", "")
        event_data = message.get("data", "")
        node_id, suffix = parse_node_from_channel(channel)
        if not node_id:
            continue

        event_type = f"{suffix}_{event_data}" if event_data else suffix

        try:
            investigate(r, node_id, event_type,
                        args.stuck_threshold_sec,
                        args.dedup_ttl_sec,
                        readiness_checker)
        except Exception as exc:
            log.error("investigate failed for %s (%s): %s", node_id, event_type, exc)


if __name__ == "__main__":
    main()
