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

# Load .env from a few standard locations (priority order):
#   1. Explicit ORCH_DOTENV env var (full path)
#   2. CWD/.env — supports the lib-extract-then-re-import pattern where
#      conductor's tasks-api daemon runs from /path/to/repo
#      and its .env should still be read.
#   3. Orchestrator package root's .env — standalone orchestrator deploys.
_dotenv_candidates = [
    Path(os.environ["ORCH_DOTENV"]) if os.environ.get("ORCH_DOTENV") else None,
    Path.cwd() / ".env",
    Path(__file__).resolve().parent.parent / ".env",
]
for _env_path in _dotenv_candidates:
    if _env_path and _env_path.is_file():
        with open(_env_path) as _f:
            for _line in _f:
                _line = _line.strip()
                if _line and not _line.startswith("#") and "=" in _line:
                    _k, _, _v = _line.partition("=")
                    _k = _k.replace("export ", "").strip()
                    os.environ.setdefault(_k, _v.strip())
        break  # first found wins

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
ORCH_NEO4J_URI = os.environ.get("ORCH_NEO4J_URI", "bolt://localhost:7687")
ORCH_NEO4J_USER = os.environ.get("ORCH_NEO4J_USER")
ORCH_NEO4J_PASS = os.environ.get("ORCH_NEO4J_PASS")
ORCH_DASHBOARD_URL = os.environ.get("ORCH_DASHBOARD_URL", "http://localhost:5002")
ORCH_NEO4J_DB = os.environ.get("ORCH_NEO4J_DB", "neo4j")
# Sentinel: optional — empty string means not used
ORCH_REDIS_SENTINELS = os.environ.get("ORCH_REDIS_SENTINELS", "")
ORCH_REDIS_SENTINEL_MASTER = os.environ.get("ORCH_REDIS_SENTINEL_MASTER", "orch-master")

# Redis key prefix - ALL orchestration keys MUST use this
KEY_PREFIX = "orch:"


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
    neo4j_uri: str = ORCH_NEO4J_URI
    neo4j_user: Optional[str] = ORCH_NEO4J_USER
    neo4j_pass: Optional[str] = ORCH_NEO4J_PASS
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
        if cfg.neo4j_user and cfg.neo4j_pass:
            _neo4j_driver = GraphDatabase.driver(
                cfg.neo4j_uri,
                auth=(cfg.neo4j_user, cfg.neo4j_pass),
            )
        else:
            _neo4j_driver = GraphDatabase.driver(cfg.neo4j_uri, auth=None)
    return _neo4j_driver


def get_neo4j_session(config: Optional[OrchConfig] = None):
    """Get a Neo4j session targeting the orchestration database."""
    cfg = config or OrchConfig()
    driver = get_neo4j_driver(cfg)
    return driver.session(database=cfg.neo4j_db)
