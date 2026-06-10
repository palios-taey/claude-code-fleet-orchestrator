"""Ship-gate e2e — the self-contained-install invariants (foundation phase).

Proves, against a real Neo4j, that the product runs on ANY user's machine:
  1. DE-UMBILICAL PATHS: with a foreign HOME and no ORCH_* path overrides, the ledger and gate-repo
     defaults follow that HOME / the install root — never the baked '/home/mira/...' literal.
  2. DYNAMIC SESSIONS: list_sessions() / GET /api/sessions derive from data (OrchProject.supervisor),
     and honor the ORCH_DASHBOARD_SESSIONS pin — no hardcoded operator fleet.
  3. LOOPBACK DEFAULT: the product launcher binds 127.0.0.1 by default (ORCH_HOST override).

Env: ORCH_NEO4J_URI (default bolt://localhost:7687), ORCH_NEO4J_DB (default neo4j).
Honest scope: integration e2e of the wiring, not a browser UI e2e.
"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from lib.orch_schema import create_project, init_schema, get_neo4j_driver, list_sessions  # noqa: E402
from lib.config import OrchConfig  # noqa: E402

CFG = OrchConfig()
_PFX = f"sess-ci-{uuid.uuid4().hex[:8]}"
_FAILURES: list[str] = []

_OLD_LEDGER = "/home/mira/the-conductor/accountability/ledger.jsonl"
_OLD_REPO = "/home/mira/claude-code-fleet-orchestrator"


def _check(name: str, cond: bool) -> None:
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond:
        _FAILURES.append(name)


def _cleanup() -> None:
    drv = get_neo4j_driver(CFG)
    with drv.session(database=CFG.neo4j_db) as s:
        s.run("MATCH (p:OrchProject) WHERE p.id STARTS WITH $p DETACH DELETE p", p=_PFX)


def _resolve_paths_under_home(home: str) -> dict:
    """Import the path-bearing modules in a clean subprocess with a foreign HOME, return defaults."""
    env = dict(os.environ)
    for k in ("ACCOUNTABILITY_LEDGER_PATH", "ORCH_GATE_REPO", "ORCH_DATA_DIR", "XDG_DATA_HOME"):
        env.pop(k, None)
    env["HOME"] = home
    env["PYTHONPATH"] = _REPO
    code = (
        "import json, lib.accountability_ledger as L, lib.gate_runner as G;"
        "from lib.paths import data_dir;"
        "print(json.dumps({'ledger': L.LEDGER_PATH, 'repo': G.DEFAULT_REPO, 'data_dir': str(data_dir())}))"
    )
    out = subprocess.check_output([sys.executable, "-c", code], env=env, text=True)
    import json
    return json.loads(out.strip().splitlines()[-1])


def main() -> int:
    init_schema(config=CFG)
    _cleanup()
    try:
        # --- 1. DE-UMBILICAL PATHS: paths follow a foreign HOME, never the baked literal ---
        home = f"/tmp/{_PFX}-home"
        paths = _resolve_paths_under_home(home)
        _check("ledger path follows foreign HOME", paths["ledger"].startswith(home + "/"))
        _check("ledger path is NOT the baked /home/mira literal", paths["ledger"] != _OLD_LEDGER)
        _check("data_dir follows foreign HOME", paths["data_dir"].startswith(home + "/"))
        _check("gate repo default is NOT the baked /home/mira literal", paths["repo"] != _OLD_REPO)

        # --- 2. DYNAMIC SESSIONS: data-derived + env pin, no hardcoded fleet ---
        sup_a, sup_b = f"{_PFX}-alpha", f"{_PFX}-beta"
        create_project(project_id=f"{_PFX}-pa", name="pa", supervisor=sup_a, config=CFG)
        create_project(project_id=f"{_PFX}-pb", name="pb", supervisor=sup_b, config=CFG)
        derived = list_sessions(config=CFG)
        _check("list_sessions includes data supervisor A", sup_a in derived)
        _check("list_sessions includes data supervisor B", sup_b in derived)
        pin = f"{_PFX}-pinned"
        os.environ["ORCH_DASHBOARD_SESSIONS"] = pin
        try:
            _check("ORCH_DASHBOARD_SESSIONS pin appears", pin in list_sessions(config=CFG))
        finally:
            os.environ.pop("ORCH_DASHBOARD_SESSIONS", None)
        _check("pin gone once env unset", pin not in list_sessions(config=CFG))

        # --- 3. LOOPBACK DEFAULT ---
        saved = os.environ.pop("ORCH_HOST", None)
        try:
            from lib.easy_setup import api_host  # noqa: E402
            _check("product launcher defaults to 127.0.0.1", api_host() == "127.0.0.1")
        finally:
            if saved is not None:
                os.environ["ORCH_HOST"] = saved
    finally:
        _cleanup()

    if _FAILURES:
        print(f"\nFAIL — {len(_FAILURES)} assertion(s): {_FAILURES}")
        return 1
    print("\nPASS — self-contained-install invariants hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
