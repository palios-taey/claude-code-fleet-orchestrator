"""Ship-gate e2e — the self-contained-install invariants (foundation phase).

Proves, against a real Neo4j, that the product runs on ANY user's machine:
  1. DE-UMBILICAL PATHS: with a foreign HOME and no ORCH_* path overrides, the ledger and gate-repo
     defaults follow that HOME / the install root — never the baked '/home/mira/...' literal.
  2. DYNAMIC SESSIONS: list_dashboard_sessions() / GET /api/sessions fail closed to the configured
     canonical supervisor allowlist, so data fixtures and peers do not leak as dashboard cards.
  3. LOOPBACK DEFAULT: the product launcher binds 127.0.0.1 by default (ORCH_HOST override).

Env: ORCH_NEO4J_URI (default bolt://localhost:7687), ORCH_NEO4J_DB (default neo4j).
Honest scope: integration e2e of the wiring, not a browser UI e2e.
"""
from __future__ import annotations

import os
import subprocess
import sys
import uuid
from dataclasses import replace

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from fleet_orchestrator.orch_schema import create_project, init_schema, get_neo4j_driver, list_dashboard_sessions  # noqa: E402
from fleet_orchestrator.config import OrchConfig  # noqa: E402

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
        "import json, fleet_orchestrator.accountability_ledger as L, fleet_orchestrator.gate_runner as G;"
        "from fleet_orchestrator.paths import data_dir;"
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

        # --- 2. DYNAMIC SESSIONS: fail-closed canonical supervisor allowlist ---
        sup_a, sup_b = f"{_PFX}-alpha", f"{_PFX}-beta"
        peer_a = f"{sup_a}-codex"
        create_project(project_id=f"{_PFX}-pa", name="pa", supervisor=sup_a, config=CFG)
        create_project(project_id=f"{_PFX}-pb", name="pb", supervisor=sup_b, config=CFG)
        create_project(project_id=f"{_PFX}-peer", name="peer", supervisor=peer_a, config=CFG)
        create_project(project_id=f"{_PFX}-orphan", name="orphan", supervisor=f"{_PFX}-fixture", config=CFG)
        _check("dashboard sessions empty without configured allowlist", list_dashboard_sessions(config=replace(CFG, session_ids=[])) == [])
        derived = list_dashboard_sessions(config=replace(CFG, session_ids=[peer_a]))
        _check("dashboard sessions include canonical configured supervisor", derived == [sup_a])
        _check("dashboard sessions exclude configured peer spelling", peer_a not in derived)
        _check("dashboard sessions exclude unconfigured data supervisor", sup_b not in derived)
        _check("dashboard sessions exclude unconfigured fixture", f"{_PFX}-fixture" not in derived)

        # --- 3. LOOPBACK DEFAULT ---
        saved = os.environ.pop("ORCH_HOST", None)
        try:
            from fleet_orchestrator.easy_setup import api_host  # noqa: E402
            _check("product launcher defaults to 127.0.0.1", api_host() == "127.0.0.1")
        finally:
            if saved is not None:
                os.environ["ORCH_HOST"] = saved

        # --- 4. PUBLIC-SURFACE SECURITY (gatekeeper + grok p0-foundation BLOCK fixes) ---
        import json as _json
        import fleet_orchestrator.public_readonly as PR  # noqa: E402
        for _k in ("ORCH_PUBLIC_SHOW_SESSIONS", "ORCH_PUBLIC_HIDE_SESSIONS"):
            os.environ.pop(_k, None)
        # 4a. XSS: script-context-safe encoding neutralizes a </script> breakout, stays valid JSON
        xss = 'x</script><script>alert(1)</script>'
        enc = PR._script_safe_json([xss])
        _check("script-safe JSON has no raw '<' (no </script> breakout)", "<" not in enc)
        _check("script-safe JSON round-trips to the original value", _json.loads(enc) == [xss])
        # 4b. fail-closed: public surface exposes NOTHING unless the operator opts sessions in
        _check("public _shown_sessions empty by default (fail-closed)", PR._shown_sessions() == set())
        _check("public _public_sessions empty by default (no data leaked)", PR._public_sessions() == [])
        # 4c. opt-in allowlist works
        os.environ["ORCH_PUBLIC_SHOW_SESSIONS"] = sup_a
        try:
            _check("public shows ONLY the opted-in session", PR._public_sessions() == [sup_a])
        finally:
            os.environ.pop("ORCH_PUBLIC_SHOW_SESSIONS", None)
        # 4d. rendered public HTML with a malicious opted-in supervisor has no script breakout
        create_project(project_id=f"{_PFX}-px", name="px", supervisor=xss, config=CFG)
        os.environ["ORCH_PUBLIC_SHOW_SESSIONS"] = xss
        try:
            html = PR._public_index_html()
            _check("rendered public HTML has no </script> breakout", "</script><script>alert" not in html)
        finally:
            os.environ.pop("ORCH_PUBLIC_SHOW_SESSIONS", None)

        # --- 5. CROSS-PLATFORM PATH REDACTION (gemini R2 #1: scrubber was Linux-only) ---
        from pathlib import Path as _Path
        _check("macOS /Users path redacted on public text",
               "/Users/alice" not in PR._scrub_public_text("see /Users/alice/secret/repo"))
        _check("Windows C:\\Users (backslash) path redacted on public text",
               "alice" not in PR._scrub_public_text(r"at C:\Users\alice\proj here"))
        _check("Windows C:/Users (forward-slash) path redacted on public text",
               "alice" not in PR._scrub_public_text("at C:/Users/alice/proj here"))
        _check("Linux /home path still redacted", "/home/bob" not in PR._scrub_public_text("/home/bob/x y"))
        _home = str(_Path.home())
        _check("actual resolved home prefix redacted dynamically (cross-platform)",
               _home not in PR._scrub_public_text(f"path {_home}/myfile rest"))
    finally:
        _cleanup()

    if _FAILURES:
        print(f"\nFAIL — {len(_FAILURES)} assertion(s): {_FAILURES}")
        return 1
    print("\nPASS — self-contained-install invariants hold")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
