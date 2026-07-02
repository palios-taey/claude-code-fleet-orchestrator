#!/usr/bin/env python3
"""Acceptance: the lazy client singletons init ATOMICALLY under concurrency.

Regression guard for the 2026-06-15 fleet-wide :5002 outage (3rd occurrence of
the "Neo4j driver already initialized with a different configuration" fault).
Root cause: get_neo4j_driver / get_redis_sync / get_redis_async published the
client BEFORE recording its config tuple, with no lock. Under the FastAPI
threadpool, concurrent cold-start requests could observe "client set, config
still None" and wrongly raise the config-mismatch guard; a thread interrupted in
that window left the process permanently broken.

Deterministic teeth: with the constructor widened (sleep) so every thread piles
into the init window, an ATOMIC init constructs the client EXACTLY ONCE and never
raises. The pre-fix code constructs it N times (no lock) and can spuriously raise.
"""
from __future__ import annotations

import os
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fleet_orchestrator.test_isolation import assert_acceptance_redis_isolated  # noqa: E402

assert_acceptance_redis_isolated()
os.environ.setdefault("ORCH_NEO4J_URI", "bolt://127.0.0.1:7687")
os.environ.setdefault("ORCH_NEO4J_DB", "neo4j")
os.environ.setdefault("ORCH_DASHBOARD_URL", "http://127.0.0.1:5002")

from fleet_orchestrator import config  # noqa: E402

FAILURES: list[str] = []
N_THREADS = 24


def _check(label: str, cond: bool, detail: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {detail}"))
    if not cond:
        FAILURES.append(label)


def _run_concurrent(getter, reset, construct_count_box) -> list[str]:
    reset()
    cfg = config.OrchConfig()
    errs: list[str] = []
    barrier = threading.Barrier(N_THREADS)

    def hit() -> None:
        try:
            barrier.wait()
            getter(cfg)
        except Exception as exc:  # noqa: BLE001
            errs.append(repr(exc))

    threads = [threading.Thread(target=hit) for _ in range(N_THREADS)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    return errs


def main() -> int:
    import neo4j

    # --- Neo4j driver (the confirmed outage path) ---
    orig_driver = neo4j.GraphDatabase.driver
    constructions = []

    def fake_driver(*args, **kwargs):
        constructions.append(1)
        time.sleep(0.03)  # widen the publish->config window so every thread piles in
        return object()

    def reset_neo4j():
        config._neo4j_driver = None
        config._neo4j_driver_config = None
        constructions.clear()

    neo4j.GraphDatabase.driver = staticmethod(fake_driver)
    try:
        errs = _run_concurrent(config.get_neo4j_driver, reset_neo4j, constructions)
        _check(
            "neo4j driver constructed exactly once under concurrent init",
            len(constructions) == 1,
            f"constructed {len(constructions)}x (pre-fix: no lock -> N constructions)",
        )
        _check(
            "no spurious OrchConfigError under concurrent neo4j init",
            not errs,
            errs[:3],
        )
    finally:
        neo4j.GraphDatabase.driver = orig_driver
        config._neo4j_driver = None
        config._neo4j_driver_config = None

    # --- Redis sync pool (same class) ---
    orig_pool = config.redis.ConnectionPool
    pool_constructions = []

    def fake_pool(*args, **kwargs):
        pool_constructions.append(1)
        time.sleep(0.03)
        return object()

    def reset_sync():
        config._sync_pool = None
        config._sentinel_sync = None
        config._sync_redis_config = None
        pool_constructions.clear()

    config.redis.ConnectionPool = fake_pool
    # redis.Redis(connection_pool=...) is constructed per-call on the return; stub it
    orig_redis_cls = config.redis.Redis
    config.redis.Redis = lambda *a, **k: object()
    try:
        errs = _run_concurrent(config.get_redis_sync, reset_sync, pool_constructions)
        _check(
            "redis sync pool constructed exactly once under concurrent init",
            len(pool_constructions) == 1,
            f"constructed {len(pool_constructions)}x",
        )
        _check(
            "no spurious OrchConfigError under concurrent redis-sync init",
            not errs,
            errs[:3],
        )
    finally:
        config.redis.ConnectionPool = orig_pool
        config.redis.Redis = orig_redis_cls
        config._sync_pool = None
        config._sentinel_sync = None
        config._sync_redis_config = None

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - lazy client singletons initialize atomically under concurrency.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
