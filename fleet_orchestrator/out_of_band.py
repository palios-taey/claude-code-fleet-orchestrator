from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from typing import Any, Iterable, Optional

from .config import OrchConfig, get_neo4j_driver, get_redis_sync


DEFAULT_HEARTBEAT_TTL_SECS = 300


def out_of_band_task_key(task_id: str) -> str:
    prefix = os.environ.get("NOTIFY_KEY_PREFIX", "taey")
    return f"{prefix}:out-of-band-task:{task_id}"


def _task_registration_scope(task_id: str, *, config: Optional[OrchConfig] = None) -> dict[str, Any]:
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    with driver.session(database=cfg.neo4j_db) as session:
        record = session.run(
            """
            MATCH (p:OrchProject)-[:HAS_PHASE]->(:OrchPhase)-[:HAS_TASK]->(t:OrchTask {id: $task_id})
            RETURN p.supervisor AS supervisor, t.owner AS owner, t.dispatched_to AS dispatched_to
            """,
            task_id=task_id,
        ).single()
    if not record:
        raise ValueError(f"out-of-band task does not exist: {task_id}")
    return {
        "supervisor": str(record["supervisor"] or ""),
        "owner": str(record["owner"] or ""),
        "dispatched_to": str(record["dispatched_to"] or ""),
    }


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
    scope = _task_registration_scope(task_id, config=config)
    real_workers = {worker for worker in (scope["owner"], scope["dispatched_to"]) if worker}
    if supervisor != scope["supervisor"]:
        raise ValueError(f"out-of-band supervisor mismatch for {task_id}: expected {scope['supervisor']!r}")
    if owner not in real_workers:
        raise ValueError(f"out-of-band owner mismatch for {task_id}: expected one of {sorted(real_workers)!r}")
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


def out_of_band_task_active(task_id: str, *, workers: Optional[Iterable[str]] = None,
                            now: Optional[float] = None,
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
    if workers is not None:
        real_workers = {str(worker) for worker in workers if worker}
        if not real_workers:
            return False
        claimed_workers = {str(payload.get("owner") or ""), str(payload.get("runner") or "")}
        if not (claimed_workers & real_workers):
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
