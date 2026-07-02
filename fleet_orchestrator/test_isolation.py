"""Agent acceptance-test store isolation helpers.

The product runtime still uses the operator-configured Redis and Neo4j stores.
Agent-run acceptance tests are different: they are mutation probes, so they must
run against throwaway loopback stores instead of the operator's live services.
"""

from __future__ import annotations

import os
import re
import socket
import uuid
from collections.abc import Mapping
from urllib.parse import urlparse


ISOLATION_ENV = "ORCH_AGENT_TEST_INFRA"
THROWAWAY_MODE = "throwaway"
EPHEMERAL_CI_MODE = "ephemeral-ci"
LIVE_NEO4J_PORTS = {7687, 7689}
LIVE_REDIS_PORTS = {6379}
_TRUTHY = {"1", "true", "yes", "on"}
_SAFE_NAMESPACE = re.compile(r"[^A-Za-z0-9_.:-]+")


class AgentTestIsolationError(RuntimeError):
    """Raised when an agent test command is pointed at live stores."""


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in _TRUTHY


def _loopback_host(host: str | None) -> bool:
    normalized = (host or "").strip().lower().strip("[]")
    if normalized in {"", "localhost", "::1"}:
        return True
    if normalized.startswith("127."):
        return True
    return False


def _endpoint_from_neo4j_uri(uri: str | None) -> tuple[str | None, int | None]:
    if not uri:
        return None, None
    parsed = urlparse(uri)
    host = parsed.hostname
    try:
        port = parsed.port
    except ValueError:
        port = None
    if port is None and parsed.scheme in {"bolt", "neo4j"}:
        port = 7687
    return host, port


def _int_port(raw: str | None) -> int | None:
    if raw is None:
        return None
    try:
        return int(str(raw).strip())
    except ValueError:
        return None


def _mode(env: Mapping[str, str]) -> str:
    if env is os.environ:
        return (os.environ.get("ORCH_AGENT_TEST_INFRA") or "").strip().lower()
    return (env.get("ORCH_AGENT_TEST_INFRA") or "").strip().lower()


def store_isolation_errors(env: Mapping[str, str] | None = None) -> list[str]:
    """Return fail-loud reasons an agent mutation test is not isolated."""

    values = os.environ if env is None else env
    mode = _mode(values)
    errors: list[str] = []

    if mode == EPHEMERAL_CI_MODE:
        if not _truthy(values.get("GITHUB_ACTIONS")):
            errors.append(
                f"{ISOLATION_ENV}=ephemeral-ci is reserved for GitHub Actions service containers; "
                "local agents must run store-backed tests via scripts/orch-acceptance-isolated."
            )
    allow_live_loopback_ports = mode == EPHEMERAL_CI_MODE and _truthy(values.get("GITHUB_ACTIONS"))

    if mode not in {THROWAWAY_MODE, EPHEMERAL_CI_MODE}:
        errors.append(
            f"{ISOLATION_ENV}=throwaway is required for local agent store-backed tests; "
            "run: scripts/orch-acceptance-isolated -- python tests/<name>_acceptance.py"
        )

    neo_host, neo_port = _endpoint_from_neo4j_uri(values.get("ORCH_NEO4J_URI"))
    if not values.get("ORCH_NEO4J_URI"):
        errors.append("ORCH_NEO4J_URI is unset; the isolated runner must provide a throwaway Neo4j URI.")
    elif neo_port is None:
        errors.append(f"ORCH_NEO4J_URI has no parseable Bolt port: {values.get('ORCH_NEO4J_URI')!r}")
    elif not allow_live_loopback_ports and not _loopback_host(neo_host):
        errors.append(
            f"ORCH_NEO4J_URI host {neo_host!r} is non-loopback; "
            "local agent mutation tests must use scripts/orch-acceptance-isolated "
            "so Neo4j is a throwaway loopback container."
        )
    elif not allow_live_loopback_ports and _loopback_host(neo_host) and neo_port in LIVE_NEO4J_PORTS:
        errors.append(
            f"ORCH_NEO4J_URI points at loopback live Neo4j port {neo_port}; "
            "agent mutation tests must use a throwaway Neo4j instance on a non-live port."
        )

    redis_host = values.get("ORCH_REDIS_HOST")
    redis_port = _int_port(values.get("ORCH_REDIS_PORT"))
    if not redis_host or redis_port is None:
        errors.append("ORCH_REDIS_HOST/ORCH_REDIS_PORT must point at a throwaway orchestrator Redis.")
    elif not allow_live_loopback_ports and not _loopback_host(redis_host):
        errors.append(
            f"ORCH_REDIS_HOST {redis_host!r} is non-loopback; "
            "local agent mutation tests must use scripts/orch-acceptance-isolated "
            "so orchestrator Redis is a throwaway loopback container."
        )
    elif not allow_live_loopback_ports and _loopback_host(redis_host) and redis_port in LIVE_REDIS_PORTS:
        errors.append(
            f"ORCH_REDIS_HOST/ORCH_REDIS_PORT point at loopback live Redis port {redis_port}; "
            "agent mutation tests must use throwaway Redis."
        )

    notify_host = values.get("REDIS_HOST")
    notify_port = _int_port(values.get("REDIS_PORT"))
    if not notify_host or notify_port is None:
        errors.append("REDIS_HOST/REDIS_PORT must point at a throwaway notify Redis.")
    elif not allow_live_loopback_ports and not _loopback_host(notify_host):
        errors.append(
            f"REDIS_HOST {notify_host!r} is non-loopback; "
            "local agent mutation tests must use scripts/orch-acceptance-isolated "
            "so notify Redis is a throwaway loopback container."
        )
    elif not allow_live_loopback_ports and _loopback_host(notify_host) and notify_port in LIVE_REDIS_PORTS:
        errors.append(
            f"REDIS_HOST/REDIS_PORT point at loopback live notify Redis port {notify_port}; "
            "agent mutation tests must isolate fleet-notify state too."
        )

    return errors


def acceptance_redis_isolation_errors(
    env: Mapping[str, str] | None = None,
    *,
    require_orch: bool = True,
    require_notify: bool = True,
) -> list[str]:
    """Return fail-loud reasons an acceptance test could touch live Redis."""

    values = os.environ if env is None else env
    mode = _mode(values)
    errors: list[str] = []

    if mode == EPHEMERAL_CI_MODE:
        if not _truthy(values.get("GITHUB_ACTIONS")):
            errors.append(
                f"{ISOLATION_ENV}=ephemeral-ci is reserved for GitHub Actions service containers; "
                "local acceptance tests must use scripts/orch-acceptance-isolated."
            )
    elif mode != THROWAWAY_MODE:
        errors.append(
            f"{ISOLATION_ENV}=throwaway is required for local Redis-backed acceptance tests; "
            "run: scripts/orch-acceptance-isolated -- python tests/<name>_acceptance.py"
        )

    allow_live_loopback_ports = mode == EPHEMERAL_CI_MODE and _truthy(values.get("GITHUB_ACTIONS"))

    def _check_redis_pair(label: str, host_key: str, port_key: str) -> None:
        host = values.get(host_key)
        port = _int_port(values.get(port_key))
        if not host or port is None:
            errors.append(f"{host_key}/{port_key} must point at a throwaway {label} Redis.")
            return
        if not allow_live_loopback_ports and not _loopback_host(host):
            errors.append(
                f"{host_key} {host!r} is non-loopback; "
                "acceptance tests must use throwaway loopback Redis."
            )
            return
        if not allow_live_loopback_ports and _loopback_host(host) and port in LIVE_REDIS_PORTS:
            errors.append(
                f"{host_key}/{port_key} point at loopback live Redis port {port}; "
                "acceptance tests must use throwaway Redis and never default to 6379."
            )

    if require_orch:
        _check_redis_pair("orchestrator", "ORCH_REDIS_HOST", "ORCH_REDIS_PORT")
    if require_notify:
        _check_redis_pair("notify", "REDIS_HOST", "REDIS_PORT")

    return errors


def assert_acceptance_redis_isolated(
    env: Mapping[str, str] | None = None,
    *,
    require_orch: bool = True,
    require_notify: bool = True,
) -> None:
    errors = acceptance_redis_isolation_errors(
        env,
        require_orch=require_orch,
        require_notify=require_notify,
    )
    if errors:
        raise SystemExit("acceptance Redis isolation failed:\n- " + "\n- ".join(errors))


def assert_agent_test_store_isolated(env: Mapping[str, str] | None = None) -> None:
    errors = store_isolation_errors(env)
    if errors:
        raise AgentTestIsolationError("agent test store isolation failed:\n- " + "\n- ".join(errors))


def free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def acceptance_namespace(raw: str | None = None) -> str:
    value = (raw or f"acceptance-{uuid.uuid4().hex[:12]}").strip()
    value = _SAFE_NAMESPACE.sub("-", value).strip(".:-_")
    if "acceptance" not in value.lower():
        value = f"acceptance-{value}"
    return value[:80] or f"acceptance-{uuid.uuid4().hex[:12]}"


def build_throwaway_env(
    *,
    base_env: Mapping[str, str] | None = None,
    neo4j_port: int,
    redis_port: int,
    notify_redis_port: int,
    namespace: str | None = None,
    repo_root: str | None = None,
) -> dict[str, str]:
    env = dict(os.environ if base_env is None else base_env)
    ns = acceptance_namespace(namespace)
    env.update(
        {
            ISOLATION_ENV: THROWAWAY_MODE,
            "ORCH_DOTENV": "empty",
            "ORCH_NEO4J_URI": f"bolt://127.0.0.1:{neo4j_port}",
            "ORCH_NEO4J_DB": "neo4j",
            "ORCH_REDIS_HOST": "127.0.0.1",
            "ORCH_REDIS_PORT": str(redis_port),
            "REDIS_HOST": "127.0.0.1",
            "REDIS_PORT": str(notify_redis_port),
            "ORCH_DASHBOARD_URL": "http://127.0.0.1:5002",
            "ORCH_TEST_NAMESPACE": ns,
            "NOTIFY_KEY_PREFIX": f"taey-{ns}",
        }
    )
    if repo_root:
        existing = env.get("PYTHONPATH")
        env["PYTHONPATH"] = repo_root if not existing else f"{repo_root}{os.pathsep}{existing}"
    assert_agent_test_store_isolated(env)
    return env
