#!/usr/bin/env python3
"""Acceptance: supervisor access errors teach the fix inline."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

FAILURES: list[str] = []


def _check(label: str, cond: bool, detail: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {detail}"))
    if not cond:
        FAILURES.append(label)


def _set_env(key: str, value: str | None, saved: dict[str, str | None]) -> None:
    if key not in saved:
        saved[key] = os.environ.get(key)
    if value is None:
        os.environ.pop(key, None)
    else:
        os.environ[key] = value


def main() -> int:
    saved: dict[str, str | None] = {}
    keys = (
        "ORCH_SESSION_IDS",
        "ORCH_SESSION_ROOTS",
        "ORCH_REF_ALLOWED_ROOT",
        "ORCH_REDIS_HOST",
        "ORCH_REDIS_PORT",
        "ORCH_NEO4J_URI",
        "ORCH_NEO4J_DB",
        "ORCH_DOTENV",
    )
    for key in keys:
        saved[key] = os.environ.get(key)
    try:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            plan_root = root / "registered-root"
            outside_root = root / "outside"
            plan_root.mkdir()
            outside_root.mkdir()
            bad_plan = outside_root / "bad-plan.md"
            bad_plan.write_text("# Project: demo - Demo\n", encoding="utf-8")

            _set_env("ORCH_DOTENV", "empty", saved)
            _set_env("ORCH_REDIS_HOST", "127.0.0.1", saved)
            _set_env("ORCH_REDIS_PORT", "6379", saved)
            _set_env("ORCH_NEO4J_URI", "bolt://127.0.0.1:7687", saved)
            _set_env("ORCH_NEO4J_DB", "neo4j", saved)
            _set_env("ORCH_SESSION_IDS", "registered-supervisor,weaver", saved)
            _set_env(
                "ORCH_SESSION_ROOTS",
                json.dumps({"registered-supervisor": str(plan_root), "weaver": str(root / "weaver")}),
                saved,
            )
            _set_env("ORCH_REF_ALLOWED_ROOT", "", saved)

            from fleet_orchestrator.config import OrchConfig
            from fleet_orchestrator.orch_schema import session_ref_root, supervisor_access_resolution
            from fleet_orchestrator.tasks_api import app

            cfg = OrchConfig()
            client = TestClient(app)

            md = """# Project: demo - Demo

## Phase: build - Build [order: 1]

### Task: one - Uses a ref [owner: registered-supervisor] [ref: README.md]
Do it.
"""
            response = client.post(
                "/api/projects/load-md",
                json={
                    "md_text": md,
                    "source_path": str(bad_plan),
                    "supervisor": "registered-supervisor",
                    "ingested_by": "registered-supervisor",
                },
            )
            detail = response.json().get("detail", "")
            _check("outside-root ingest rejects", response.status_code == 422, response.text)
            _check("ingest error names caller session", "You are session registered-supervisor" in detail, detail)
            _check("ingest error names session root", str(plan_root) in detail, detail)
            _check("ingest error says do not request access", "You already have access" in detail, detail)
            _check("ingest error keeps rejected path", str(bad_plan) in detail, detail)
            _check("registered session resolves exact root", session_ref_root("registered-supervisor", config=cfg) == str(plan_root), session_ref_root("registered-supervisor", config=cfg))

            peer_response = client.post(
                "/api/projects/load-md",
                json={
                    "md_text": md,
                    "source_path": str(bad_plan),
                    "supervisor": "registered-supervisor-codex",
                    "ingested_by": "registered-supervisor-codex",
                },
            )
            peer_detail = peer_response.json().get("detail", "")
            peer_access = supervisor_access_resolution("registered-supervisor-codex", config=cfg)
            _check("unregistered peer ingest rejects", peer_response.status_code == 422, peer_response.text)
            _check("unregistered peer has no supervisor access", not peer_access["registered"] and not peer_access["plan_ref_root"], peer_access)
            _check("unregistered peer root does not alias base session", session_ref_root("registered-supervisor-codex", config=cfg) is None, session_ref_root("registered-supervisor-codex", config=cfg))
            _check("unregistered peer ingest teaches registration", "tmux session registered-supervisor-codex is not a registered supervisor" in peer_detail, peer_detail)
            _check("unregistered peer ingest does not promise access", "You already have access" not in peer_detail and str(plan_root) not in peer_detail, peer_detail)

            current_response = client.get("/api/sessions/not-registered/current")
            current_detail = current_response.json().get("detail", "")
            _check("unregistered current rejects", current_response.status_code == 400, current_response.text)
            _check("unregistered error names tmux session", "tmux session not-registered" in current_detail, current_detail)
            _check("unregistered error lists registered sessions", "registered-supervisor" in current_detail and "weaver" in current_detail, current_detail)
            _check("unregistered error teaches tmux identity", "supervisors are identified by tmux session name" in current_detail, current_detail)

            next_response = client.get("/api/sessions/not-registered/next-ready")
            next_detail = next_response.json().get("detail", "")
            _check("unregistered next-ready rejects", next_response.status_code == 400, next_response.text)
            _check("next-ready carries same teaching", "tmux session not-registered" in next_detail, next_detail)
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - supervisor access failures teach the corrective inline.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
