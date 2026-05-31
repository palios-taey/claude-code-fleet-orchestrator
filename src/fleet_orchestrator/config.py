"""
Orchestration Configuration

Orch-specific Redis/Neo4j connections with strict namespace isolation.
All Redis keys are prefixed with 'orch:'. Neo4j uses 'orchestration' database.

ISOLATION: Zero shared state with memory infrastructure (ISMA, HMM, Weaviate).
"""

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import redis
import redis.asyncio as aioredis

from neo4j import basic_auth

def _parse_env_file(env_path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    with env_path.open() as handle:
        for raw_line in handle:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.replace("export ", "", 1).strip()
            value = value.strip().strip("\"'")
            if key:
                values[key] = value
    return values


def _candidate_env_paths() -> list[Path]:
    repo_root = Path(__file__).resolve().parents[2]
    home_root = Path.home()
    explicit = Path(os.environ["ORCH_DOTENV"]) if os.environ.get("ORCH_DOTENV") else None
    candidates: list[Path] = [
        path for path in (
            explicit,
            Path.cwd() / ".env",
            repo_root / ".env",
            home_root / ".env",
            home_root / "the-conductor" / ".env",
            home_root / "claude-code-fleet-orchestrator" / ".env",
            home_root / "treasurer" / ".env",
        )
        if path is not None
    ]
    try:
        candidates.extend(sorted(home_root.glob("*/.env")))
    except OSError:
        pass
    return candidates


def _load_env_defaults() -> None:
    seen: set[Path] = set()
    for env_path in _candidate_env_paths():
        try:
            resolved = env_path.resolve()
        except OSError:
            resolved = env_path
        if resolved in seen or not env_path.is_file():
            continue
        seen.add(resolved)
        for key, value in _parse_env_file(env_path).items():
            os.environ.setdefault(key, value)

        if (
            os.environ.get("ORCH_NEO4J_URI")
            and os.environ.get("ORCH_NEO4J_USER")
            and os.environ.get("ORCH_NEO4J_PASS")
        ):
            break


_load_env_defaults()

# Environment overrides with sensible localhost defaults so module-level
# imports don't KeyError if the .env loader missed a path (race condition
# observed by x-claude 2026-05-26: first import of orch-watch failed with
# KeyError ORCH_REDIS_HOST; subsequent loads succeeded). Defaults match
# the typical single-machine fleet layout. Production deployments should
# override via .env or environment; the default-presence is a safety
# net that lets imports succeed even if config is missing, so the failure
# manifests at OrchConfig() use time (with a clear connection error)
# rather than at import time (with a cryptic KeyError that masks the
# actual call site).
ORCH_REDIS_HOST = os.environ.get("ORCH_REDIS_HOST", "127.0.0.1")
ORCH_REDIS_PORT = int(os.environ.get("ORCH_REDIS_PORT", "6379"))
ORCH_NEO4J_URI = os.environ.get("ORCH_NEO4J_URI")
ORCH_NEO4J_USER = os.environ.get("ORCH_NEO4J_USER")
ORCH_NEO4J_PASS = os.environ.get("ORCH_NEO4J_PASS")
ORCH_NEO4J_REQUIRE_AUTH = os.environ.get("ORCH_NEO4J_REQUIRE_AUTH", "")
ORCH_DASHBOARD_URL = os.environ.get("ORCH_DASHBOARD_URL", "http://localhost:5002")
ORCH_NEO4J_DB = os.environ.get("ORCH_NEO4J_DB", "neo4j")
# Sentinel: optional — empty string means not used
ORCH_REDIS_SENTINELS = os.environ.get("ORCH_REDIS_SENTINELS", "")
ORCH_REDIS_SENTINEL_MASTER = os.environ.get("ORCH_REDIS_SENTINEL_MASTER", "orch-master")

# Redis key prefix - ALL orchestration keys MUST use this
KEY_PREFIX = "orch:"


class OrchConfigError(RuntimeError):
    """Raised when required orchestration configuration is missing."""


def _is_truthy(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _parse_sentinels(raw: str):
    """Parse 'host:port,host:port' into [(host, port), ...]."""
    if not raw.strip():
        return []
    pairs = []
    for entry in raw.split(","):
        entry = entry.strip()
        if ":" in entry:
            h, p = entry.rsplit(":", 1)
            pairs.append((h, int(p)))
        else:
            pairs.append((entry, 26379))
    return pairs


@dataclass
class OrchConfig:
    """Orchestration layer configuration."""
    redis_host: str = ORCH_REDIS_HOST
    redis_port: int = ORCH_REDIS_PORT
    neo4j_uri: Optional[str] = ORCH_NEO4J_URI
    neo4j_user: Optional[str] = ORCH_NEO4J_USER
    neo4j_pass: Optional[str] = ORCH_NEO4J_PASS
    neo4j_require_auth: bool = field(default_factory=lambda: _is_truthy(ORCH_NEO4J_REQUIRE_AUTH))
    neo4j_db: str = ORCH_NEO4J_DB
    redis_sentinels: str = ORCH_REDIS_SENTINELS
    redis_sentinel_master: str = ORCH_REDIS_SENTINEL_MASTER

    # Heartbeat (optimal: T=12s, TTL=3T=36s)
    heartbeat_interval_s: float = 12.0
    heartbeat_ttl_s: int = 36

    # Task queue
    task_stream: str = f"{KEY_PREFIX}streams:tasks"
    event_stream: str = f"{KEY_PREFIX}streams:events"
    consumer_group: str = "conductors"
    stream_maxlen: int = 100_000

    # File locks
    file_lock_ttl_s: int = 1800  # 30 minutes
    file_lock_prefix: str = f"{KEY_PREFIX}lock:file:"

    # Agent registry
    agent_prefix: str = f"{KEY_PREFIX}agent:"
    heartbeat_prefix: str = f"{KEY_PREFIX}heartbeat:"
    activity_prefix: str = f"{KEY_PREFIX}activity:"

    # Notifications
    notify_prefix: str = f"{KEY_PREFIX}notify:"
    alert_channel: str = f"{KEY_PREFIX}notify:alerts"

    # Suspected dead agents set
    suspected_dead_key: str = f"{KEY_PREFIX}suspected_dead"


def key(suffix: str) -> str:
    """Generate a namespaced Redis key. All orch keys go through here."""
    return f"{KEY_PREFIX}{suffix}"


# --- Redis connection pool (singleton) ---

_sync_pool: Optional[redis.ConnectionPool] = None
_async_pool: Optional[aioredis.ConnectionPool] = None


_sentinel_sync = None


def get_redis_sync(config: Optional[OrchConfig] = None) -> redis.Redis:
    """Get synchronous Redis client. Uses Sentinel if configured, direct otherwise."""
    global _sync_pool, _sentinel_sync
    cfg = config or OrchConfig()
    sentinels = _parse_sentinels(cfg.redis_sentinels)

    if sentinels:
        if _sentinel_sync is None:
            from redis.sentinel import Sentinel
            _sentinel_sync = Sentinel(sentinels, socket_timeout=3, decode_responses=True)
        return _sentinel_sync.master_for(cfg.redis_sentinel_master, socket_timeout=3)

    if _sync_pool is None:
        _sync_pool = redis.ConnectionPool(
            host=cfg.redis_host,
            port=cfg.redis_port,
            decode_responses=True,
            max_connections=20,
        )
    return redis.Redis(connection_pool=_sync_pool)


_sentinel_async = None


def get_redis_async(config: Optional[OrchConfig] = None) -> aioredis.Redis:
    """Get async Redis client. Uses Sentinel if configured, direct otherwise."""
    global _async_pool, _sentinel_async
    cfg = config or OrchConfig()
    sentinels = _parse_sentinels(cfg.redis_sentinels)

    if sentinels:
        # redis-py async Sentinel support
        if _sentinel_async is None:
            from redis.asyncio.sentinel import Sentinel as AsyncSentinel
            _sentinel_async = AsyncSentinel(sentinels, socket_timeout=3, decode_responses=True)
        return _sentinel_async.master_for(cfg.redis_sentinel_master, socket_timeout=3)

    if _async_pool is None:
        _async_pool = aioredis.ConnectionPool(
            host=cfg.redis_host,
            port=cfg.redis_port,
            decode_responses=True,
            max_connections=20,
        )
    return aioredis.Redis(connection_pool=_async_pool)


_neo4j_driver = None


def get_neo4j_driver(config: Optional[OrchConfig] = None):
    """Get Neo4j driver for the orchestration database (singleton)."""
    global _neo4j_driver
    if _neo4j_driver is None:
        from neo4j import GraphDatabase
        cfg = config or OrchConfig()
        if not cfg.neo4j_uri:
            raise OrchConfigError("ORCH_NEO4J_URI must be set")
        if cfg.neo4j_user and cfg.neo4j_pass:
            _neo4j_driver = GraphDatabase.driver(
                cfg.neo4j_uri,
                auth=basic_auth(cfg.neo4j_user, cfg.neo4j_pass),
            )
        elif cfg.neo4j_require_auth:
            raise OrchConfigError(
                "ORCH_NEO4J_USER and ORCH_NEO4J_PASS must be set when ORCH_NEO4J_REQUIRE_AUTH=1"
            )
        else:
            _neo4j_driver = GraphDatabase.driver(cfg.neo4j_uri, auth=None)
    return _neo4j_driver


def get_neo4j_session(config: Optional[OrchConfig] = None):
    """Get a Neo4j session targeting the orchestration database."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    return driver.session(database=cfg.neo4j_db)
