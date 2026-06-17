#!/usr/bin/env python3
"""Acceptance: the orchestrator supports NO internal-service auth (Neo4j/Redis/Weaviate).

Standing rule (Jesse, repeatedly): internal services run with no auth — the network
is the trust boundary. Supporting internal credentials is the disease, not a feature:
the recurring fleet-wide :5002 outage ("Neo4j driver already initialized with a
different configuration") was a per-session .env's ORCH_NEO4J_USER/PASS flipping the
no-auth config tuple after driver init. This test FAILS if internal-auth support is
reintroduced, so the principle is enforced mechanically, not by memory.
"""
from __future__ import annotations

import inspect
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fleet_orchestrator import config  # noqa: E402

FAILURES: list[str] = []


def _check(label: str, cond: bool, detail: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {detail}"))
    if not cond:
        FAILURES.append(label)


def main() -> int:
    # 1. OrchConfig must not carry Neo4j credential fields.
    cfg_fields = set(getattr(config.OrchConfig, "__dataclass_fields__", {}))
    _check("OrchConfig has no neo4j_user field", "neo4j_user" not in cfg_fields, cfg_fields & {"neo4j_user"})
    _check("OrchConfig has no neo4j_pass field", "neo4j_pass" not in cfg_fields, cfg_fields & {"neo4j_pass"})

    # 2. get_neo4j_driver source must connect with auth=None and never build an auth tuple.
    src = inspect.getsource(config.get_neo4j_driver)
    _check("get_neo4j_driver uses auth=None", "auth=None" in src, "no auth=None found")
    _check(
        "get_neo4j_driver builds no credential auth tuple",
        "auth=(" not in src.replace(" ", "") and "auth=(cfg" not in src,
        "an auth=(...) credential tuple is present",
    )

    # 3. config.py source must not read the internal-auth env vars at all.
    cfg_src = inspect.getsource(config)
    # Exclude comment lines (the rule is documented there); check live code only.
    code_lines = [ln for ln in cfg_src.splitlines() if not ln.lstrip().startswith("#")]
    code = "\n".join(code_lines)
    for var in ("ORCH_NEO4J_USER", "ORCH_NEO4J_PASS"):
        _check(f"config.py does not read {var}", var not in code, f"{var} is read in config.py")

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - no internal-service auth support (Neo4j connects auth=None, no credential config).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
