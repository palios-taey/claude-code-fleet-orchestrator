"""Standalone configuration for claude-code-fleet-orchestrator."""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional

import redis
import redis.asyncio as aioredis


class OrchConfigError(ValueError):
    """Raised when required orchestrator configuration is missing or invalid."""


def _load_dotenv_candidates() -> None:
    candidates = []
    explicit = os.environ.get("ORCH_DOTENV")
    if explicit:
        candidates.append(Path(explicit))
    candidates.append(Path.cwd() / ".env")
    candidates.append(Path(__file__).resolve().parent.parent / ".env")

    for env_path in candidates:
        if not env_path.is_file():
            continue
        with env_path.open(encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.replace("export ", "").strip()
                os.environ.setdefault(key, value.strip())
        break


_load_dotenv_candidates()

KEY_PREFIX = "orch:"


def _require_env(name: str) -> str:
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise OrchConfigError(f"{name} must be set")
    return value.strip()


def _optional_env(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _int_env(name: str, default: Optional[int] = None) -> int:
    raw = _optional_env(name)
    if raw is None:
        if default is None:
            raise OrchConfigError(f"{name} must be set")
        return default
    try:
        return int(raw)
    except ValueError as exc:
        raise OrchConfigError(f"{name} must be an integer") from exc


# ---------------------------------------------------------------------------
# THE ENV CONTRACT (no-fallback discipline, made auditable).
#
# The no-fallback rule is: config whose ABSENCE would silently produce WRONG
# behavior must fail LOUD; config that is genuinely OPTIONAL may carry a
# documented default, because an absent value then selects a *supported mode*,
# not a masked-missing-config fallback. These two categories are declared here
# so an auditor can verify the contract by reading one place instead of tracing
# every _optional_env call. (ws1-grok-audit Check-3 resolution, 2026-06-11: the
# distinction was correct in behavior but implicit; this makes it explicit.)
#
# REQUIRED — _require_env / _int_env(no default): absence raises OrchConfigError.
# These are connectivity-critical; a silent default would point the product at
# the wrong store and corrupt state.
REQUIRED_ENV = (
    "ORCH_REDIS_HOST",      # _require via redis_host
    "ORCH_REDIS_PORT",      # _int_env (no default) via redis_port
    "ORCH_NEO4J_URI",       # _require via neo4j_uri
    "ORCH_NEO4J_DB",        # _require via neo4j_db
)
# OPTIONAL — _optional_env with a documented default. Absent => the named mode,
# which is a fully supported operating state, NOT a silent fallback for missing
# required config. Each entry: (var, default, why-the-default-is-a-real-mode).
# Note: when one of these IS present but MALFORMED, its parser fails loud
# (e.g. _parse_product_owner_map raises on bad JSON/format) — only true ABSENCE
# selects the default. A typo'd variable *name* is indistinguishable from unset
# for any program and is out of scope for the product to detect.
OPTIONAL_ENV = (
    # var, default, supported-mode rationale
    ("ORCH_PRODUCT_OWNER_MAP", "{} (no remap)",
     "no product remap -> dispatch routes session->worker directly; the common single-fleet case"),
    ("ORCH_SESSION_IDS", "[] (deny-all notify allowlist)",
     "empty allowlist fail-CLOSES: an unlisted notify target raises 400, never silently delivers"),
    ("NOTIFY_KEY_PREFIX", "taey",
     "canonical redis namespace; override only to run a second isolated fleet on one redis"),
    ("ORCH_NOTIFY_CLI", "taey-notify",
     "the released CLI name on PATH; override for a vendored/renamed install"),
    ("ORCH_NOTIFY_LIB_ROOT", "None (use importable fleet-notify)",
     "required only if fleet-notify is not already importable on sys.path; ensure_notify_importable raises loud if missing then"),
    ("ORCH_REF_ALLOWED_ROOT", "unset (refs disabled)",
     "plan/source refs are disabled fail-safe when unset; any ref use fails loud at validation time"),
    ("ORCH_DASHBOARD_URL", "http://127.0.0.1:5002",
     "local API/UI base URL used by setup/UI helpers; core engine state does not depend on it"),
    ("ORCH_DATA_DIR", "$XDG_DATA_HOME or ~/.local/share (XDG Base Dir spec)",
     "standard local-tool data location; zero-config by design for a single-user local product"),
)


def _parse_sentinels(raw: str) -> list[tuple[str, int]]:
    if not raw.strip():
        return []
    pairs: list[tuple[str, int]] = []
    for entry in raw.split(","):
        item = entry.strip()
        if not item:
            continue
        if ":" in item:
            host, port = item.rsplit(":", 1)
            try:
                pairs.append((host, int(port)))
            except ValueError as exc:
                raise OrchConfigError("ORCH_REDIS_SENTINELS must use host:port pairs") from exc
        else:
            pairs.append((item, 26379))
    return pairs


def _parse_product_owner_map() -> Dict[str, str]:
    raw = _optional_env("ORCH_PRODUCT_OWNER_MAP")
    if raw is None:
        raw = _optional_env("PRODUCT_OWNER_MAP", "")
    if not raw:
        return {}

    try:
        import json

        parsed = json.loads(raw)
    except Exception:
        parsed = None

    if isinstance(parsed, dict):
        result = {}
        for key, value in parsed.items():
            key_s = str(key).strip()
            value_s = str(value).strip()
            if key_s and value_s:
                result[key_s] = value_s
        if result:
            return result

    result: Dict[str, str] = {}
    for item in raw.replace(";", ",").split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise OrchConfigError("ORCH_PRODUCT_OWNER_MAP must be JSON or key=value pairs")
        key, value = item.split("=", 1)
        key = key.strip()
        value = value.strip()
        if not key or not value:
            raise OrchConfigError("ORCH_PRODUCT_OWNER_MAP contains an empty key or value")
        result[key] = value
    return result


def _parse_session_ids() -> list[str]:
    raw = _optional_env("ORCH_SESSION_IDS", "")
    if not raw:
        return []
    items = [item.strip() for item in raw.replace(";", ",").split(",")]
    return [item for item in items if item]


def ensure_notify_importable() -> None:
    try:
        import identity  # noqa: F401
        return
    except ImportError as first_error:
        notify_root = _optional_env("ORCH_NOTIFY_LIB_ROOT")
        if notify_root is None:
            raise OrchConfigError(
                "ORCH_NOTIFY_LIB_ROOT must be set when fleet-notify is not already importable"
            ) from first_error
        notify_path = Path(notify_root)
        if not notify_path.is_dir():
            raise OrchConfigError("ORCH_NOTIFY_LIB_ROOT must point to a directory")
        if str(notify_path) not in sys.path:
            sys.path.insert(0, str(notify_path))
        try:
            import identity  # noqa: F401
        except ImportError as second_error:
            raise OrchConfigError(
                "ORCH_NOTIFY_LIB_ROOT does not contain an importable fleet-notify installation"
            ) from second_error


def notify_cli() -> str:
    return _optional_env("ORCH_NOTIFY_CLI", "taey-notify") or "taey-notify"


@dataclass
class OrchConfig:
    """Configuration loaded from environment variables."""

    redis_host: str = field(default_factory=lambda: _require_env("ORCH_REDIS_HOST"))
    redis_port: int = field(default_factory=lambda: _int_env("ORCH_REDIS_PORT"))
    neo4j_uri: str = field(default_factory=lambda: _require_env("ORCH_NEO4J_URI"))
    neo4j_user: Optional[str] = field(default_factory=lambda: _optional_env("ORCH_NEO4J_USER"))
    neo4j_pass: Optional[str] = field(default_factory=lambda: _optional_env("ORCH_NEO4J_PASS"))
    neo4j_db: str = field(default_factory=lambda: _require_env("ORCH_NEO4J_DB"))
    dashboard_url: str = field(default_factory=lambda: _optional_env("ORCH_DASHBOARD_URL", "http://127.0.0.1:5002") or "http://127.0.0.1:5002")
    redis_sentinels: str = field(default_factory=lambda: _optional_env("ORCH_REDIS_SENTINELS", "") or "")
    redis_sentinel_master: str = field(default_factory=lambda: _optional_env("ORCH_REDIS_SENTINEL_MASTER", "orch-master") or "orch-master")
    notify_lib_root: Optional[str] = field(default_factory=lambda: _optional_env("ORCH_NOTIFY_LIB_ROOT"))
    notify_cli_path: str = field(default_factory=notify_cli)
    product_owner_map: Dict[str, str] = field(default_factory=_parse_product_owner_map)
    session_ids: list[str] = field(default_factory=_parse_session_ids)

    heartbeat_interval_s: float = 12.0
    heartbeat_ttl_s: int = 36
    task_stream: str = f"{KEY_PREFIX}streams:tasks"
    event_stream: str = f"{KEY_PREFIX}streams:events"
    consumer_group: str = "conductors"
    stream_maxlen: int = 100_000
    file_lock_ttl_s: int = 1800
    file_lock_prefix: str = f"{KEY_PREFIX}lock:file:"
    agent_prefix: str = f"{KEY_PREFIX}agent:"
    heartbeat_prefix: str = f"{KEY_PREFIX}heartbeat:"
    activity_prefix: str = f"{KEY_PREFIX}activity:"
    notify_prefix: str = f"{KEY_PREFIX}notify:"
    alert_channel: str = f"{KEY_PREFIX}notify:alerts"
    suspected_dead_key: str = f"{KEY_PREFIX}suspected_dead"


def key(suffix: str) -> str:
    return f"{KEY_PREFIX}{suffix}"


_sync_pool: Optional[redis.ConnectionPool] = None
_async_pool: Optional[aioredis.ConnectionPool] = None
_sentinel_sync = None
_sentinel_async = None
_sync_redis_config: Optional[tuple[str, int, str, str]] = None
_async_redis_config: Optional[tuple[str, int, str, str]] = None
_neo4j_driver = None
_neo4j_driver_config: Optional[tuple[str, Optional[str], Optional[str], str]] = None


def _redis_config_tuple(cfg: OrchConfig) -> tuple[str, int, str, str]:
    return (cfg.redis_host, cfg.redis_port, cfg.redis_sentinels, cfg.redis_sentinel_master)


def get_redis_sync(config: Optional[OrchConfig] = None) -> redis.Redis:
    global _sync_pool, _sentinel_sync, _sync_redis_config
    cfg = config or OrchConfig()
    config_tuple = _redis_config_tuple(cfg)
    if (_sync_pool is not None or _sentinel_sync is not None) and _sync_redis_config != config_tuple:
        raise OrchConfigError(
            "Redis sync client already initialized with a different configuration; restart the process to change ORCH_REDIS_*"
        )
    sentinels = _parse_sentinels(cfg.redis_sentinels)

    if sentinels:
        if _sentinel_sync is None:
            from redis.sentinel import Sentinel

            _sentinel_sync = Sentinel(sentinels, socket_timeout=3, decode_responses=True)
            _sync_redis_config = config_tuple
        return _sentinel_sync.master_for(cfg.redis_sentinel_master, socket_timeout=3)

    if _sync_pool is None:
        _sync_pool = redis.ConnectionPool(
            host=cfg.redis_host,
            port=cfg.redis_port,
            decode_responses=True,
            max_connections=20,
        )
        _sync_redis_config = config_tuple
    return redis.Redis(connection_pool=_sync_pool)


def get_redis_async(config: Optional[OrchConfig] = None) -> aioredis.Redis:
    global _async_pool, _sentinel_async, _async_redis_config
    cfg = config or OrchConfig()
    config_tuple = _redis_config_tuple(cfg)
    if (_async_pool is not None or _sentinel_async is not None) and _async_redis_config != config_tuple:
        raise OrchConfigError(
            "Redis async client already initialized with a different configuration; restart the process to change ORCH_REDIS_*"
        )
    sentinels = _parse_sentinels(cfg.redis_sentinels)

    if sentinels:
        if _sentinel_async is None:
            from redis.asyncio.sentinel import Sentinel as AsyncSentinel

            _sentinel_async = AsyncSentinel(sentinels, socket_timeout=3, decode_responses=True)
            _async_redis_config = config_tuple
        return _sentinel_async.master_for(cfg.redis_sentinel_master, socket_timeout=3)

    if _async_pool is None:
        _async_pool = aioredis.ConnectionPool(
            host=cfg.redis_host,
            port=cfg.redis_port,
            decode_responses=True,
            max_connections=20,
        )
        _async_redis_config = config_tuple
    return aioredis.Redis(connection_pool=_async_pool)


def get_neo4j_driver(config: Optional[OrchConfig] = None):
    """Return the process-wide Neo4j driver singleton.

    Callers own short-lived sessions via ``with driver.session(...)``; the shared
    driver is intentionally not closed by schema helpers.
    """
    global _neo4j_driver, _neo4j_driver_config
    from neo4j import GraphDatabase

    cfg = config or OrchConfig()
    config_tuple = (cfg.neo4j_uri, cfg.neo4j_user, cfg.neo4j_pass, cfg.neo4j_db)
    if _neo4j_driver is not None and _neo4j_driver_config != config_tuple:
        raise OrchConfigError(
            "Neo4j driver already initialized with a different configuration; restart the process to change ORCH_NEO4J_*"
        )
    if _neo4j_driver is None:
        if cfg.neo4j_user and cfg.neo4j_pass:
            _neo4j_driver = GraphDatabase.driver(cfg.neo4j_uri, auth=(cfg.neo4j_user, cfg.neo4j_pass))
        else:
            _neo4j_driver = GraphDatabase.driver(cfg.neo4j_uri, auth=None)
        _neo4j_driver_config = config_tuple
    return _neo4j_driver


def get_neo4j_session(config: Optional[OrchConfig] = None):
    cfg = config or OrchConfig()
    return get_neo4j_driver(cfg).session(database=cfg.neo4j_db)
