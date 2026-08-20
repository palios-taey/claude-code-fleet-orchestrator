"""Acceptance contract for legacy and explicit Codex-supervisor topologies."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace

from fastapi import HTTPException

_REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _REPO)

from fleet_orchestrator.completion_guard import _autonomous_peer_supervisor  # noqa: E402
from fleet_orchestrator.config import OrchConfigError, _parse_session_ids  # noqa: E402
from fleet_orchestrator.control_principal_migration import codex_supervisor_mappings  # noqa: E402
from fleet_orchestrator.session_topology import (  # noqa: E402
    control_principal_for_session,
    session_aliases,
    supervised_worker_sessions,
)
from fleet_orchestrator.tasks_api import _ensure_registered_session, _infer_dispatch_supervisor  # noqa: E402


FAILURES: list[str] = []


def _check(label: str, condition: bool, detail: object = "") -> None:
    print(("  PASS " if condition else "  FAIL ") + label)
    if not condition:
        FAILURES.append(f"{label}: {detail}")


def _config_contract() -> None:
    original = os.environ.get("ORCH_SESSION_IDS")
    try:
        os.environ["ORCH_SESSION_IDS"] = "conductor-codex,weaver-codex"
        _check(
            "explicit codex controls parse",
            _parse_session_ids() == ["conductor-codex", "weaver-codex"],
        )
        for malformed in (
            "conductor,conductor-codex",
            "conductor-codex,conductor-codex",
            "conductor-grok",
            "conductor-codex-codex",
        ):
            os.environ["ORCH_SESSION_IDS"] = malformed
            try:
                _parse_session_ids()
            except OrchConfigError:
                rejected = True
            else:
                rejected = False
            _check(f"ambiguous control registration fails loud: {malformed}", rejected)
    finally:
        if original is None:
            os.environ.pop("ORCH_SESSION_IDS", None)
        else:
            os.environ["ORCH_SESSION_IDS"] = original


def _topology_contract() -> None:
    legacy = ["conductor"]
    explicit = ["conductor-codex"]
    expected_workers = (
        "conductor",
        "conductor-gemini",
        "conductor-grok",
        "conductor-claude",
    )

    _check(
        "legacy supervisor remains bare",
        control_principal_for_session("conductor-codex", legacy) == "conductor",
    )
    _check(
        "legacy worker roster remains suffixed",
        supervised_worker_sessions("conductor", legacy)
        == ("conductor-codex", "conductor-gemini", "conductor-grok", "conductor-claude"),
    )
    _check(
        "explicit codex control remains exact",
        control_principal_for_session("conductor-codex", explicit) == "conductor-codex",
    )
    _check(
        "bare Claude maps to explicit codex control",
        control_principal_for_session("conductor", explicit) == "conductor-codex",
    )
    _check(
        "Gemini and Grok map to explicit codex control",
        all(
            control_principal_for_session(worker, explicit) == "conductor-codex"
            for worker in ("conductor-gemini", "conductor-grok")
        ),
    )
    _check(
        "configured topology beats stale Redis parent input",
        control_principal_for_session(
            "conductor-grok",
            explicit,
            explicit_parent="wrong-supervisor",
        )
        == "conductor-codex",
    )
    _check(
        "explicit codex worker roster includes Claude and excludes itself",
        supervised_worker_sessions("conductor-codex", explicit) == expected_workers,
    )
    aliases = session_aliases("conductor-codex", explicit)
    _check("aliases contain no nested codex session", "conductor-codex-codex" not in aliases, aliases)
    _check(
        "suffix-like family name remains intact",
        control_principal_for_session("x-claude", ["x-claude-codex"]) == "x-claude-codex"
        and supervised_worker_sessions("x-claude-codex", ["x-claude-codex"])[0] == "x-claude",
    )
    _check(
        "migration maps only explicit codex controls",
        codex_supervisor_mappings(["conductor-codex", "legacy", "x-claude-codex"])
        == (
            {"from": "conductor", "to": "conductor-codex"},
            {"from": "x-claude", "to": "x-claude-codex"},
        ),
    )
    _check("taey remains peerless", supervised_worker_sessions("taey", ["taey"]) == ())
    _check("taey aliases do not invent peers", session_aliases("taey", ["taey"]) == ("taey",))


def _authority_contract() -> None:
    explicit_cfg = SimpleNamespace(session_ids=["conductor-codex"])
    legacy_cfg = SimpleNamespace(session_ids=["conductor"])

    _check(
        "registered codex is not treated as an autonomous worker",
        _autonomous_peer_supervisor("conductor-codex", config=explicit_cfg) is None,
    )
    _check(
        "bare Claude is guarded as codex-supervised worker",
        _autonomous_peer_supervisor("conductor", config=explicit_cfg) == "conductor-codex",
    )
    _check(
        "legacy codex remains a bare-supervisor worker",
        _autonomous_peer_supervisor("conductor-codex", config=legacy_cfg) == "conductor",
    )
    _check(
        "dispatch inference assigns explicit codex owner",
        _infer_dispatch_supervisor("conductor-grok", {}, config=explicit_cfg) == "conductor-codex",
    )
    allowed = []
    for worker in ("conductor", "conductor-gemini", "conductor-grok"):
        try:
            _ensure_registered_session(worker, explicit_cfg)
        except HTTPException:
            allowed.append(False)
        else:
            allowed.append(True)
    _check("derived workers pass the API target allowlist", all(allowed), allowed)
    try:
        _ensure_registered_session("unrelated-grok", explicit_cfg)
    except HTTPException as exc:
        rejected = exc.status_code == 400
    else:
        rejected = False
    _check("unrelated worker remains rejected", rejected)


def main() -> int:
    _config_contract()
    _topology_contract()
    _authority_contract()
    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - legacy and explicit Codex-supervisor authority remain unambiguous")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
