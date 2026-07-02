#!/usr/bin/env python3
"""Acceptance: Redis-backed acceptances fail closed instead of defaulting live."""

from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fleet_orchestrator.test_isolation import (  # noqa: E402
    EPHEMERAL_CI_MODE,
    ISOLATION_ENV,
    THROWAWAY_MODE,
    acceptance_redis_isolation_errors,
    assert_acceptance_redis_isolated,
    build_throwaway_env,
)


FAILURES: list[str] = []


def _check(label: str, condition: bool, detail: object = "") -> None:
    print(("PASS " if condition else "FAIL ") + label + ("" if condition else f" -> {detail}"))
    if not condition:
        FAILURES.append(label)


def _raises_system_exit(env: dict[str, str]) -> bool:
    try:
        assert_acceptance_redis_isolated(env)
    except SystemExit:
        return True
    return False


def _forbidden_default_violations() -> list[str]:
    patterns = [
        re.compile(r'os\.environ\.setdefault\("ORCH_REDIS_(?:HOST|PORT)",\s*"(?:127\.0\.0\.1|6379)"\)'),
        re.compile(r'os\.environ\.setdefault\("REDIS_(?:HOST|PORT)",\s*"(?:127\.0\.0\.1|6379)"\)'),
        re.compile(r'os\.environ\.get\("REDIS_(?:HOST|PORT)"\)\s+or\s+"(?:127\.0\.0\.1|6379)"'),
        re.compile(r'os\.environ\.get\("REDIS_(?:HOST|PORT)",\s*"(?:127\.0\.0\.1|6379)"\)'),
    ]
    violations: list[str] = []
    for path in sorted((ROOT / "tests").glob("*acceptance*.py")):
        text = path.read_text(encoding="utf-8")
        for idx, line in enumerate(text.splitlines(), start=1):
            if any(pattern.search(line) for pattern in patterns):
                violations.append(f"{path.relative_to(ROOT)}:{idx}:{line.strip()}")
    return violations


def main() -> int:
    unset_errors = acceptance_redis_isolation_errors({})
    unset_text = "\n".join(unset_errors)
    _check("unset Redis env is rejected", "ORCH_REDIS_HOST/ORCH_REDIS_PORT" in unset_text and "REDIS_HOST/REDIS_PORT" in unset_text, unset_errors)
    _check("unset Redis env raises SystemExit", _raises_system_exit({}), unset_errors)

    live_loopback = {
        ISOLATION_ENV: THROWAWAY_MODE,
        "ORCH_REDIS_HOST": "127.0.0.1",
        "ORCH_REDIS_PORT": "6379",
        "REDIS_HOST": "127.0.0.1",
        "REDIS_PORT": "6379",
    }
    live_errors = acceptance_redis_isolation_errors(live_loopback)
    _check("throwaway marker alone cannot bless live Redis", any("live Redis port 6379" in item for item in live_errors), live_errors)

    isolated = build_throwaway_env(
        base_env={},
        neo4j_port=17687,
        redis_port=16379,
        notify_redis_port=16380,
        namespace="redis-isolation-acceptance",
        repo_root=str(ROOT),
    )
    _check("throwaway Redis env passes guard", acceptance_redis_isolation_errors(isolated) == [], acceptance_redis_isolation_errors(isolated))

    ci_env = {
        ISOLATION_ENV: EPHEMERAL_CI_MODE,
        "GITHUB_ACTIONS": "true",
        "ORCH_REDIS_HOST": "127.0.0.1",
        "ORCH_REDIS_PORT": "6379",
        "REDIS_HOST": "127.0.0.1",
        "REDIS_PORT": "6380",
    }
    _check("GitHub service Redis may use loopback service ports", acceptance_redis_isolation_errors(ci_env) == [], acceptance_redis_isolation_errors(ci_env))

    violations = _forbidden_default_violations()
    _check("acceptance tests contain no live Redis fallback defaults", violations == [], violations)

    runner = (ROOT / "scripts" / "orch-acceptance-isolated").read_text(encoding="utf-8")
    _check("isolated runner snapshots live Redis before/after", "_live_redis_snapshot" in runner and "_run_with_live_redis_guard" in runner, "missing live Redis guard")
    _check("live Redis guard fails on acceptance-attributable keys", "127.0.0.1:6379 changed" in runner and "acceptance-attributable keys" in runner, "missing leak failure text")

    workflow = (ROOT / ".github" / "workflows" / "ship-gate.yml").read_text(encoding="utf-8")
    _check("ship-gate runs Redis isolation sweep", "tests/redis_acceptance_isolation_acceptance.py" in workflow, "workflow missing Redis sweep")
    _check("ship-gate orchestrator Redis points at dedicated throwaway service", 'ORCH_REDIS_PORT: "6381"' in workflow, "workflow ORCH Redis still uses live guard port")
    _check("ship-gate notify Redis points at dedicated service", 'REDIS_PORT: "6380"' in workflow, "workflow notify Redis not isolated")
    _check("ship-gate snapshots live Redis before suite", "Snapshot live Redis before acceptance suite" in workflow, "workflow missing pre-snapshot")
    _check("ship-gate fails on live Redis acceptance leaks", "live Redis 6379 changed acceptance-attributable keys" in workflow, "workflow missing post-snapshot failure")

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - Redis-backed acceptances fail closed and the live-Redis guard is wired.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
