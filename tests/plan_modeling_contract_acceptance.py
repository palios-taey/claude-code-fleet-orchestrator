#!/usr/bin/env python3
"""Acceptance: every plan ingest response carries the modeling contract and lint."""
from __future__ import annotations

import contextlib
import io
import os
import sys
import tempfile
import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fleet_orchestrator.config import OrchConfigError  # noqa: E402
from fleet_orchestrator import plan_loader  # noqa: E402

FAILURES: list[str] = []

MEGA_PLAN = """# Project: plan-modeling-mega - Mega Plan
> exercises the plan-modeling lint

## Phase: p1 - Work

### Task: bundled - Grok RCA + Codex fix + conductor gate and deploy [owner: conductor]
- Grok runs RCA, Codex implements the fix, conductor gates and deploys live.
"""

CLEAN_PLAN = """# Project: plan-modeling-clean - Clean Plan
> decomposed by executor

## Phase: p1 - Work

### Task: rca - RCA finding [owner: reviewer-grok]
- inspect the handoff and record root cause

### Task: fix - Implement scoped fix [owner: reviewer-codex] [depends: rca]
- apply the code change

### Task: gate - Gate result [owner: reviewer] [depends: fix]
- verify evidence and decide merge
"""

SUPERVISOR_PEER_PROSE_PLAN = """# Project: plan-modeling-supervisor-peer-prose - Supervisor Peer Prose
> supervisor-owned gate work can mention peers without making them executors

## Phase: p1 - Work

### Task: r5-gate - Reviewer peer-review decision [owner: reviewer]
- reviewer reads grok's peer-review note and records the acceptance decision
"""

OWNERLESS_PLAN = """# Project: plan-modeling-ownerless - Ownerless Plan
> ownerless tasks are claimable by matched sessions

## Phase: p1 - Work

### Task: claimable - Grok RCA + Codex fix + conductor gate and deploy
- Grok runs RCA, Codex implements the fix, conductor gates and deploys live.
"""

CUSTOM_SUPERVISOR_PLAN = """# Project: plan-modeling-custom-supervisor - Custom Supervisor
> configured session ids are the source of truth

## Phase: p1 - Work

### Task: custom-bundled - Grok RCA + Codex fix + review-lead gate and deploy [owner: review-lead]
- Grok runs RCA, Codex implements the fix, review-lead gates and deploys live.
"""


def _check(label: str, cond: bool, detail: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {detail}"))
    if not cond:
        FAILURES.append(label)


def _noop(*_: object, **__: object) -> None:
    return None


def _create_project(**kwargs: object) -> object:
    return kwargs.get("project_id")


def _existing_project_state(_: str, __: object) -> dict[str, object]:
    return {"phase_ids": set(), "task_phase": {}}


def _add_dependency(_: str, __: str, **___: object) -> bool:
    return True


@contextlib.contextmanager
def _mock_plan_storage():
    with contextlib.ExitStack() as stack:
        stack.enter_context(patch.object(plan_loader, "_existing_project_state", _existing_project_state))
        stack.enter_context(patch.object(plan_loader, "create_project", _create_project))
        stack.enter_context(patch.object(plan_loader, "create_phase", _noop))
        stack.enter_context(patch.object(plan_loader, "create_task", _noop))
        stack.enter_context(patch.object(plan_loader, "assign_task_to_phase", _noop))
        stack.enter_context(patch.object(plan_loader, "_set_task_metadata", _noop))
        stack.enter_context(patch.object(plan_loader, "_release_ingest_holds", _noop))
        stack.enter_context(patch.object(plan_loader, "add_dependency", _add_dependency))
        yield


def _ingest(md: str, session_ids: list[str] | None = None) -> dict[str, object]:
    with _mock_plan_storage():
        return plan_loader.load_plan_from_text(
            md=md,
            source_path="",
            source_kind="markdown",
            ingested_by="plan-modeling-acceptance",
            supervisor="reviewer",
            priority=50,
            config=SimpleNamespace(session_ids=session_ids or ["reviewer"]),
        )


def _ingest_without_registered_sessions(md: str) -> str:
    try:
        with _mock_plan_storage():
            plan_loader.load_plan_from_text(
                md=md,
                source_path="",
                source_kind="markdown",
                ingested_by="plan-modeling-acceptance",
                supervisor="reviewer",
                priority=50,
                config=SimpleNamespace(session_ids=[]),
            )
    except OrchConfigError as exc:
        return str(exc)
    return ""


def _contract_rules(result: dict[str, object]) -> list[str]:
    contract = result.get("plan_modeling_contract")
    if not isinstance(contract, dict):
        return []
    rules = contract.get("rules")
    if not isinstance(rules, list):
        return []
    return [str(rule) for rule in rules]


def _load_taey_plan_module():
    return importlib.import_module("fleet_orchestrator.cli_taey_plan")


def _cli_stdout(result: dict[str, object]) -> str:
    module = _load_taey_plan_module()
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False) as handle:
        handle.write(CLEAN_PLAN)
        path = handle.name
    try:
        module.api_call = lambda method, endpoint, data=None: result
        module.detect_session = lambda: "reviewer-codex"
        args = SimpleNamespace(path=path, supervisor=None, priority=None, migration_exempt=False)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            module.cmd_ingest(args)
        return out.getvalue()
    finally:
        os.unlink(path)


def main() -> int:
    mega = _ingest(MEGA_PLAN)
    mega_rules = _contract_rules(mega)
    mega_warnings = mega.get("plan_modeling_warnings")
    _check(
        "mega ingest response includes contract rules",
        any("single session" in rule and "executes the task" in rule for rule in mega_rules),
        mega,
    )
    _check(
        "mega ingest response warns on the bundled task",
        isinstance(mega_warnings, list)
        and any(
            "task bundled looks like multiple steps across actors" in str(item)
            for item in mega_warnings
        ),
        mega_warnings,
    )

    dynamic = _ingest(CUSTOM_SUPERVISOR_PLAN, session_ids=["review-lead"])
    dynamic_warnings = dynamic.get("plan_modeling_warnings")
    _check(
        "configured ORCH_SESSION_IDS supervisor role drives plan modeling warning",
        isinstance(dynamic_warnings, list)
        and any(
            "task custom-bundled looks like multiple steps across actors" in str(item)
            for item in dynamic_warnings
        ),
        dynamic_warnings,
    )

    missing_config_error = _ingest_without_registered_sessions(MEGA_PLAN)
    _check(
        "missing ORCH_SESSION_IDS fails loud for plan modeling",
        "ORCH_SESSION_IDS must be set" in missing_config_error,
        missing_config_error,
    )

    clean = _ingest(CLEAN_PLAN)
    clean_rules = _contract_rules(clean)
    clean_warnings = clean.get("plan_modeling_warnings")
    _check(
        "clean decomposed ingest response still includes contract",
        any("taey-plan assign" in rule for rule in clean_rules),
        clean,
    )
    _check(
        "clean decomposed ingest response has zero plan-modeling warnings",
        clean_warnings == [],
        clean_warnings,
    )

    supervisor_peer_prose = _ingest(SUPERVISOR_PEER_PROSE_PLAN)
    _check(
        "supervisor-owned gate task mentioning peers has zero plan-modeling warnings",
        supervisor_peer_prose.get("plan_modeling_warnings") == [],
        supervisor_peer_prose.get("plan_modeling_warnings"),
    )

    ownerless = _ingest(OWNERLESS_PLAN)
    _check(
        "ownerless multi-actor task is not flagged",
        ownerless.get("plan_modeling_warnings") == [],
        ownerless.get("plan_modeling_warnings"),
    )

    stdout = _cli_stdout(clean)
    _check("taey-plan ingest stdout includes the contract block", "plan_modeling_contract:" in stdout, stdout)
    _check("taey-plan ingest stdout renders contract content", "single session that executes the task" in stdout, stdout)
    _check("taey-plan ingest stdout renders warning count", "plan_modeling_warnings=0" in stdout, stdout)

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - plan ingest injects modeling contract and plan-modeling lint.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
