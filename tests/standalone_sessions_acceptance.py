"""Ship-gate e2e — the self-contained-install invariants (foundation phase).

Proves, against a real Neo4j, that the product runs on ANY user's machine:
  1. DE-UMBILICAL PATHS: with a synthetic install root, a foreign HOME, and no ORCH_* path
     overrides, the ledger and gate-repo defaults follow data_dir() / repo_root() dynamically.
  2. DYNAMIC SESSIONS: list_dashboard_sessions() / GET /api/sessions fail closed to the configured
     canonical supervisor allowlist, so data fixtures and peers do not leak as dashboard cards.
  3. LOOPBACK DEFAULT: the product launcher binds 127.0.0.1 by default (ORCH_HOST override).

Env: ORCH_NEO4J_URI (default bolt://localhost:7687), ORCH_NEO4J_DB (default neo4j).
Honest scope: integration e2e of the wiring, not a browser UI e2e.
The path probe runs with `ORCH_DOTENV=empty`, so operator `.env` overrides such
as ORCH_GATE_REPO cannot mask the default-install contract.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import replace
from pathlib import Path

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from fleet_orchestrator.orch_schema import (  # noqa: E402
    _dashboard_supervisor_session,
    _resolve_supervisor_session,
    _supervisor_badge_session,
    create_project,
    get_neo4j_driver,
    init_schema,
    list_dashboard_sessions,
)
from fleet_orchestrator.config import OrchConfig  # noqa: E402
from fleet_orchestrator.notify_state import redis_connect as notify_redis_connect, state_key as notify_state_key  # noqa: E402

CFG = OrchConfig()
_PFX = f"sess-ci-{uuid.uuid4().hex[:8]}"
_FAILURES: list[str] = []


def _check(name: str, cond: bool) -> None:
    print(("  PASS " if cond else "  FAIL ") + name)
    if not cond:
        _FAILURES.append(name)


def _cleanup() -> None:
    drv = get_neo4j_driver(CFG)
    with drv.session(database=CFG.neo4j_db) as s:
        s.run("MATCH (p:OrchProject) WHERE p.id STARTS WITH $p DETACH DELETE p", p=_PFX)
    notify_redis_connect().delete(
        notify_state_key(f"{_PFX}-hands", "parent"),
        notify_state_key(f"{_PFX}-claude", "parent"),
    )


def _synthetic_install_root(parent: str) -> Path:
    install = Path(parent) / "synthetic-orchestrator"
    shutil.copytree(
        Path(_REPO) / "fleet_orchestrator",
        install / "fleet_orchestrator",
        ignore=shutil.ignore_patterns("__pycache__"),
    )
    Path(install, ".env").write_text(
        "ORCH_GATE_REPO=/tmp/poison-gate-repo\n"
        "ACCOUNTABILITY_LEDGER_PATH=/tmp/poison-ledger.jsonl\n"
        "ACCOUNTABILITY_CI_AUDIT_PATH=/tmp/poison-ci-audit.jsonl\n"
        "ORCH_DATA_DIR=/tmp/poison-data-dir\n",
        encoding="utf-8",
    )
    return install


def _resolve_paths_under_home(home: str, install_root: Path) -> dict:
    """Import path-bearing modules from a synthetic install root with a foreign HOME."""
    env = dict(os.environ)
    for k in ("ACCOUNTABILITY_LEDGER_PATH", "ORCH_GATE_REPO", "ORCH_DATA_DIR", "XDG_DATA_HOME"):
        env.pop(k, None)
    env["HOME"] = home
    env["PYTHONPATH"] = str(install_root)
    env["ORCH_DOTENV"] = "empty"
    code = (
        "import json, fleet_orchestrator.accountability_ledger as L, fleet_orchestrator.gate_runner as G;"
        "from fleet_orchestrator.paths import data_dir, repo_root;"
        "print(json.dumps({'ledger': L.LEDGER_PATH, 'ci_audit': L.CI_AUDIT_PATH, 'repo': G.DEFAULT_REPO, 'repo_root': str(repo_root()), 'data_dir': str(data_dir())}))"
    )
    out = subprocess.check_output([sys.executable, "-c", code], env=env, text=True, cwd=str(install_root))
    return json.loads(out.strip().splitlines()[-1])


def main() -> int:
    init_schema(config=CFG)
    _cleanup()
    try:
        # --- 1. DE-UMBILICAL PATHS: defaults are derived from a synthetic install root ---
        with tempfile.TemporaryDirectory(prefix=f"{_PFX}-install-") as tmp:
            install_root = _synthetic_install_root(tmp)
            home = str(Path(tmp) / "home")
            paths = _resolve_paths_under_home(home, install_root)
        data_dir = Path(paths["data_dir"])
        ledger = Path(paths["ledger"])
        ci_audit = Path(paths["ci_audit"])
        repo = Path(paths["repo"])
        repo_root = Path(paths["repo_root"])
        _check("probe imported from synthetic install root", repo_root == install_root)
        _check("data_dir follows foreign HOME", data_dir == Path(home) / ".local" / "share" / "claude-code-fleet-orchestrator")
        _check("ledger path is derived from data_dir", ledger == data_dir / "accountability" / "ledger.jsonl")
        _check("CI audit path is derived from data_dir", ci_audit == data_dir / "accountability" / "ci-audit.jsonl")
        _check("gate repo default follows repo_root()", repo == repo_root)

        # --- 2. DYNAMIC SESSIONS: fail-closed canonical supervisor allowlist ---
        sup_a, sup_b = f"{_PFX}-alpha", f"{_PFX}-beta"
        peer_a = f"{sup_a}-codex"
        create_project(project_id=f"{_PFX}-pa", name="pa", supervisor=sup_a, config=CFG)
        create_project(project_id=f"{_PFX}-pb", name="pb", supervisor=sup_b, config=CFG)
        create_project(project_id=f"{_PFX}-peer", name="peer", supervisor=peer_a, config=CFG)
        create_project(project_id=f"{_PFX}-orphan", name="orphan", supervisor=f"{_PFX}-fixture", config=CFG)
        _check("dashboard sessions empty without configured allowlist", list_dashboard_sessions(config=replace(CFG, session_ids=[])) == [])
        derived = list_dashboard_sessions(config=replace(CFG, session_ids=[peer_a]))
        _check("dashboard sessions preserve explicitly configured codex supervisor", derived == [peer_a])
        _check("dashboard sessions exclude its bare Claude worker", sup_a not in derived)
        _check("dashboard sessions exclude unconfigured data supervisor", sup_b not in derived)
        _check("dashboard sessions exclude unconfigured fixture", f"{_PFX}-fixture" not in derived)
        legacy_derived = list_dashboard_sessions(config=replace(CFG, session_ids=[sup_a]))
        _check("legacy bare supervisor topology remains unchanged", legacy_derived == [sup_a])
        hands_sup = f"{_PFX}-hands"
        notify_redis_connect().set(notify_state_key(hands_sup, "parent"), sup_b)
        hands_derived = list_dashboard_sessions(config=replace(CFG, session_ids=[hands_sup]))
        _check("configured non-peer supervisor is not collapsed by Redis parent", hands_derived == [hands_sup])
        _check("configured non-peer shared resolver stays itself", _resolve_supervisor_session(hands_sup, config=replace(CFG, session_ids=[hands_sup])) == hands_sup)
        _check("configured non-peer supervisor parent does not mint dashboard card", sup_b not in hands_derived)
        claude_sup = f"{_PFX}-claude"
        notify_redis_connect().set(notify_state_key(claude_sup, "parent"), sup_b)
        claude_derived = list_dashboard_sessions(config=replace(CFG, session_ids=[claude_sup]))
        _check("configured claude-suffixed supervisor is not collapsed", claude_derived == [claude_sup])
        _check(
            "configured claude dashboard resolver stays itself",
            _dashboard_supervisor_session(
                claude_sup,
                config=replace(CFG, session_ids=[claude_sup]),
            )
            == claude_sup,
        )
        _check(
            "configured claude badge resolver stays itself",
            _supervisor_badge_session(
                claude_sup,
                config=replace(CFG, session_ids=[claude_sup]),
            )
            == claude_sup,
        )
        _check("configured claude shared resolver stays itself", _resolve_supervisor_session(claude_sup, config=replace(CFG, session_ids=[claude_sup])) == claude_sup)
        notify_redis_connect().set(notify_state_key(peer_a, "parent"), peer_a)
        _check("non-configured peer still resolves to parent", _resolve_supervisor_session(peer_a, config=replace(CFG, session_ids=[sup_a])) == sup_a)
        notify_redis_connect().set(notify_state_key(sup_a, "parent"), sup_b)
        _check("configured codex topology beats stale bare-worker parent", _resolve_supervisor_session(sup_a, config=replace(CFG, session_ids=[peer_a])) == peer_a)
        _check("configured codex control remains itself", _resolve_supervisor_session(peer_a, config=replace(CFG, session_ids=[peer_a])) == peer_a)

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
