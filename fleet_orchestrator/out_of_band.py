from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any, Optional

from .config import OrchConfig, get_redis_sync


DEFAULT_HEARTBEAT_TTL_SECS = 300


def out_of_band_task_key(task_id: str) -> str:
    prefix = os.environ.get("NOTIFY_KEY_PREFIX", "taey")
    return f"{prefix}:out-of-band-task:{task_id}"


def register_out_of_band_task(
    task_id: str,
    *,
    supervisor: str,
    owner: str,
    runner: str,
    heartbeat_ttl_secs: int = DEFAULT_HEARTBEAT_TTL_SECS,
    details: Optional[str] = None,
    config: Optional[OrchConfig] = None,
) -> dict[str, Any]:
    if not task_id.strip():
        raise ValueError("task_id is required")
    if not supervisor.strip():
        raise ValueError("supervisor is required")
    if not owner.strip():
        raise ValueError("owner is required")
    now = time.time()
    payload: dict[str, Any] = {
        "task_id": task_id,
        "supervisor": supervisor,
        "owner": owner,
        "runner": runner or supervisor,
        "registered_at": now,
        "heartbeat_at": now,
        "heartbeat_ttl_secs": max(1, int(heartbeat_ttl_secs)),
    }
    if details:
        payload["details"] = details[:500]
    r = get_redis_sync(config)
    r.set(out_of_band_task_key(task_id), json.dumps(payload, separators=(",", ":")), ex=payload["heartbeat_ttl_secs"] * 2)
    return payload


def heartbeat_out_of_band_task(task_id: str, *, config: Optional[OrchConfig] = None) -> dict[str, Any]:
    r = get_redis_sync(config)
    key = out_of_band_task_key(task_id)
    raw = r.get(key)
    if not raw:
        raise RuntimeError(f"out-of-band task is not registered or heartbeat expired: {task_id}")
    try:
        payload = json.loads(raw)
    except Exception as exc:
        raise RuntimeError(f"out-of-band task registration is malformed: {task_id}") from exc
    if not isinstance(payload, dict) or payload.get("task_id") != task_id:
        raise RuntimeError(f"out-of-band task registration does not match task: {task_id}")
    payload["heartbeat_at"] = time.time()
    ttl = max(1, int(payload.get("heartbeat_ttl_secs") or DEFAULT_HEARTBEAT_TTL_SECS))
    r.set(key, json.dumps(payload, separators=(",", ":")), ex=ttl * 2)
    return payload


def clear_out_of_band_task(task_id: str, *, config: Optional[OrchConfig] = None) -> None:
    get_redis_sync(config).delete(out_of_band_task_key(task_id))


def out_of_band_task_active(task_id: str, *, now: Optional[float] = None,
                            config: Optional[OrchConfig] = None) -> bool:
    r = get_redis_sync(config)
    raw = r.get(out_of_band_task_key(task_id))
    if not raw:
        return False
    try:
        payload = json.loads(raw)
    except Exception:
        return False
    if not isinstance(payload, dict) or payload.get("task_id") != task_id:
        return False
    try:
        heartbeat_at = float(payload.get("heartbeat_at"))
        ttl = max(1, int(payload.get("heartbeat_ttl_secs") or DEFAULT_HEARTBEAT_TTL_SECS))
    except (TypeError, ValueError):
        return False
    return ((time.time() if now is None else now) - heartbeat_at) < ttl


def patch_task_status(task_id: str, status: str, *, evidence: Optional[dict[str, Any]] = None,
                      sender: str = "", dashboard_url: Optional[str] = None) -> dict[str, Any]:
    base = (dashboard_url or os.environ.get("ORCH_DASHBOARD_URL") or "http://127.0.0.1:5002").rstrip("/")
    payload: dict[str, Any] = {"status": status}
    if sender:
        payload["from"] = sender
    if evidence is not None:
        payload["evidence"] = evidence
    request = urllib.request.Request(
        f"{base}/api/task/{task_id}",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="PATCH",
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"task status patch failed HTTP {exc.code}: {exc.read().decode()[:500]}") from exc
    if not isinstance(result, dict) or not result.get("ok"):
        raise RuntimeError(f"task status patch failed: {result}")
    return result


def notify_supervisor(supervisor: str, body: str, *, sender: str, priority: str = "high",
                      config: Optional[OrchConfig] = None) -> None:
    cfg = config or OrchConfig()
    result = subprocess.run(
        [cfg.notify_cli_path, supervisor, body, "--from", sender, "--type", "response_ready", "--priority", priority],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"{cfg.notify_cli_path} failed")
