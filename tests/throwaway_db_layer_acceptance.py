#!/usr/bin/env python3
"""Acceptance: agent mutation tests are routed to throwaway stores."""

from __future__ import annotations

import os
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fleet_orchestrator.test_isolation import (  # noqa: E402
    EPHEMERAL_CI_MODE,
    ISOLATION_ENV,
    THROWAWAY_MODE,
    build_throwaway_env,
    store_isolation_errors,
)


FAILURES: list[str] = []


def _check(label: str, cond: bool, detail: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {detail}"))
    if not cond:
        FAILURES.append(label)


def _live_defaults_env() -> dict[str, str]:
    return {
        "ORCH_NEO4J_URI": "bolt://127.0.0.1:7687",
        "ORCH_NEO4J_DB": "neo4j",
        "ORCH_REDIS_HOST": "127.0.0.1",
        "ORCH_REDIS_PORT": "6379",
        "REDIS_HOST": "127.0.0.1",
        "REDIS_PORT": "6379",
    }


def main() -> int:
    live_errors = store_isolation_errors(_live_defaults_env())
    live_text = "\n".join(live_errors)
    _check("live loopback Neo4j is rejected", "live Neo4j port 7687" in live_text, live_errors)
    _check("live loopback orchestrator Redis is rejected", "live Redis port 6379" in live_text, live_errors)
    _check("error teaches isolated runner", "scripts/orch-acceptance-isolated" in live_text, live_errors)

    fake_throwaway = {**_live_defaults_env(), ISOLATION_ENV: THROWAWAY_MODE}
    fake_errors = store_isolation_errors(fake_throwaway)
    _check("throwaway marker alone cannot bless live ports", any("live Neo4j" in item for item in fake_errors), fake_errors)

    isolated = build_throwaway_env(
        base_env={},
        neo4j_port=17687,
        redis_port=16379,
        notify_redis_port=16380,
        namespace="agent-test-isolation",
        repo_root=str(ROOT),
    )
    _check("isolated throwaway env passes guard", store_isolation_errors(isolated) == [], store_isolation_errors(isolated))
    _check("isolated env suppresses deployment dotenv", isolated["ORCH_DOTENV"] == "empty", isolated)
    _check("isolated env separates ORCH and notify Redis", isolated["ORCH_REDIS_PORT"] != isolated["REDIS_PORT"], isolated)
    _check("isolated env namespace is visibly acceptance-scoped", "acceptance" in isolated["ORCH_TEST_NAMESPACE"], isolated)

    ci_env = {**_live_defaults_env(), ISOLATION_ENV: EPHEMERAL_CI_MODE, "GITHUB_ACTIONS": "true"}
    _check("GitHub service-container mode accepts CI loopback ports", store_isolation_errors(ci_env) == [], store_isolation_errors(ci_env))
    fake_ci = {**_live_defaults_env(), ISOLATION_ENV: EPHEMERAL_CI_MODE}
    _check("ephemeral-ci marker is refused outside GitHub Actions", any("GitHub Actions" in item for item in store_isolation_errors(fake_ci)), fake_ci)

    script = (ROOT / "scripts" / "orch-acceptance-isolated").read_text(encoding="utf-8")
    _check("isolated runner keeps Neo4j no-auth posture", "NEO4J_AUTH=none" in script, "missing no-auth container env")
    _check("isolated runner does not introduce internal auth env", "ORCH_NEO4J_USER" not in script and "ORCH_NEO4J_PASS" not in script)

    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    claude = (ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    config_doc = (ROOT / "docs" / "CONFIGURATION.md").read_text(encoding="utf-8")
    workflow = (ROOT / ".github" / "workflows" / "ship-gate.yml").read_text(encoding="utf-8")
    _check("README routes store-backed agent tests through isolated runner", "scripts/orch-acceptance-isolated" in readme, "README missing runner")
    _check("CLAUDE agent guide forbids live stores for mutation tests", "scripts/orch-acceptance-isolated" in claude, "CLAUDE.md missing runner")
    _check("CONFIGURATION documents ORCH_AGENT_TEST_INFRA", "ORCH_AGENT_TEST_INFRA" in config_doc, "CONFIGURATION.md missing flag")
    _check("ship-gate runs this acceptance", "tests/throwaway_db_layer_acceptance.py" in workflow, "workflow missing test")
    _check("ship-gate declares ephemeral CI store mode", "ORCH_AGENT_TEST_INFRA: ephemeral-ci" in workflow, "workflow missing ephemeral-ci marker")

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - agent mutation tests are routed to throwaway stores, with no internal auth added.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
