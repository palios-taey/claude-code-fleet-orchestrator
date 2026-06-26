"""Acceptance: dispatch cannot bypass wake-packet rules/context injection."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fleet_orchestrator.context_assembler as assembler  # noqa: E402
import fleet_orchestrator.dispatch as dispatch_module  # noqa: E402


FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _summary(project_id: str, task_id: str, description: str) -> dict:
    return {
        "project": {
            "id": project_id,
            "name": project_id,
            "description": "mandatory dispatch injection",
            "source_path": "",
        },
        "phases": [
            {
                "phase": {"id": f"{project_id}::phase", "name": "Phase"},
                "tasks": [
                    {
                        "id": task_id,
                        "description": description,
                        "status": "in_progress",
                        "owner": "supervisor",
                        "dispatched_to": "worker-codex",
                    }
                ],
            }
        ],
        "ref_tiers": {
            "overall": {"ref_context": {"refs": []}},
            "supervisor": {"ref_context": {"refs": []}},
            "project": {"ref_context": {"refs": []}},
            "phases": [],
            "tasks": [],
        },
    }


def _dispatch_body(*, worker: str, supervisor: str, prompt_body: str | None,
                   endpoint_enabled: str | None, full_context: bool = True) -> str:
    project_id = "mandatory-injection"
    task_id = f"{project_id}::{worker}"
    description = f"{worker} direct dispatch"
    captured: list[list[str]] = []

    def fake_run(args: list[str], **_kwargs: object) -> SimpleNamespace:
        if args and args[0] == "git":
            return SimpleNamespace(returncode=0, stderr="", stdout="test-head\n")
        captured.append(args)
        return SimpleNamespace(returncode=0, stderr="", stdout="")

    with tempfile.TemporaryDirectory(prefix="dispatch-mandatory-injection-") as raw:
        rules_root = Path(raw) / "rules"
        (rules_root / "supervisors").mkdir(parents=True)
        (rules_root / "projects").mkdir(parents=True)
        (rules_root / "global.md").write_text("GLOBAL_RULE_MANDATORY_DISPATCH", encoding="utf-8")
        (rules_root / "supervisors" / f"{worker.removesuffix('-codex')}.md").write_text(
            "SUPERVISOR_RULE_MANDATORY_DISPATCH",
            encoding="utf-8",
        )
        (rules_root / "projects" / f"{project_id}.md").write_text(
            "PROJECT_RULE_MANDATORY_DISPATCH",
            encoding="utf-8",
        )

        old_rules_root = os.environ.get("ORCH_RULES_ROOT")
        old_endpoint = os.environ.get("ORCH_WAKE_PACKET_ENDPOINT_ENABLED")
        old_roots = os.environ.get("ORCH_SESSION_ROOTS")
        os.environ["ORCH_RULES_ROOT"] = str(rules_root)
        os.environ.pop("ORCH_SESSION_ROOTS", None)
        if endpoint_enabled is None:
            os.environ.pop("ORCH_WAKE_PACKET_ENDPOINT_ENABLED", None)
        else:
            os.environ["ORCH_WAKE_PACKET_ENDPOINT_ENABLED"] = endpoint_enabled
        try:
            task_project_patch = (
                mock.patch.object(assembler, "get_task_project", side_effect=RuntimeError("neo unavailable"))
                if not full_context
                else mock.patch.object(
                    assembler,
                    "get_task_project",
                    return_value={"project_id": project_id, "project_name": project_id},
                )
            )
            with mock.patch.object(dispatch_module, "_resolve_product_id", return_value=None), \
                 mock.patch.object(dispatch_module, "_redis_connect", return_value=SimpleNamespace(get=lambda _key: None)), \
                 mock.patch.object(dispatch_module, "_claim_ready_orch_task"), \
                 mock.patch.object(dispatch_module, "mark_superseded_for_task"), \
                 mock.patch.object(dispatch_module, "bind_current_task", return_value=123.0), \
                 mock.patch.object(dispatch_module, "OrchConfig", return_value=SimpleNamespace(notify_cli_path="notify")), \
                 mock.patch.object(dispatch_module, "hook_installation_status", return_value=SimpleNamespace(ok=True, detail="hooked")), \
                 mock.patch.object(dispatch_module.subprocess, "run", side_effect=fake_run), \
                 mock.patch.object(dispatch_module, "maybe_emit_decision_receipt"), \
                 task_project_patch, \
                 mock.patch.object(assembler, "get_project_summary", return_value=_summary(project_id, task_id, description)), \
                 mock.patch.object(assembler, "get_overall_refs", return_value={"ref_context": {"refs": []}}), \
                 mock.patch.object(assembler, "get_supervisor_refs", return_value={"ref_context": {"refs": []}}):
                dispatch_module.dispatch(
                    worker=worker,
                    task_id=task_id,
                    description=description,
                    supervisor=supervisor,
                    prompt_body=prompt_body,
                )
        finally:
            if old_rules_root is None:
                os.environ.pop("ORCH_RULES_ROOT", None)
            else:
                os.environ["ORCH_RULES_ROOT"] = old_rules_root
            if old_endpoint is None:
                os.environ.pop("ORCH_WAKE_PACKET_ENDPOINT_ENABLED", None)
            else:
                os.environ["ORCH_WAKE_PACKET_ENDPOINT_ENABLED"] = old_endpoint
            if old_roots is None:
                os.environ.pop("ORCH_SESSION_ROOTS", None)
            else:
                os.environ["ORCH_SESSION_ROOTS"] = old_roots

    _check(f"{worker}: notify called once", len(captured) == 1, captured)
    return captured[0][2] if captured else ""


def _assert_mandatory_packet(label: str, body: str, expected_dispatch_text: str) -> None:
    _check(f"{label}: notify body is the rendered packet", body.startswith("# AGENTS.md Dynamic Context"), body[:200])
    _check(f"{label}: global rule injected", "GLOBAL_RULE_MANDATORY_DISPATCH" in body, body)
    _check(f"{label}: supervisor rule injected", "SUPERVISOR_RULE_MANDATORY_DISPATCH" in body, body)
    _check(f"{label}: project rule injected", "PROJECT_RULE_MANDATORY_DISPATCH" in body, body)
    _check(f"{label}: dispatch prompt preserved inside packet", expected_dispatch_text in body, body)
    _check(f"{label}: record_outcome footer preserved", "record_outcome" in body, body)


def _direct_dispatch_no_endpoint_contract() -> None:
    body = _dispatch_body(
        worker="direct-bypass-codex",
        supervisor="supervisor",
        prompt_body="DIRECT_DISPATCH_BODY",
        endpoint_enabled="0",
    )
    _assert_mandatory_packet("direct dispatch with endpoint disabled", body, "DIRECT_DISPATCH_BODY")


def _hooked_session_contract() -> None:
    body = _dispatch_body(
        worker="hooked-codex",
        supervisor="supervisor",
        prompt_body="HOOKED_SESSION_BODY",
        endpoint_enabled="0",
    )
    _assert_mandatory_packet("hooked session dispatch", body, "HOOKED_SESSION_BODY")


def _adhoc_no_neo_contract() -> None:
    body = _dispatch_body(
        worker="adhoc-codex",
        supervisor="supervisor",
        prompt_body="ADHOC_NO_NEO_BODY",
        endpoint_enabled="0",
        full_context=False,
    )
    _assert_mandatory_packet("ad-hoc no-Neo4j dispatch", body, "ADHOC_NO_NEO_BODY")
    _check("ad-hoc no-Neo4j dispatch carries visible context warning", "context_warning" in body and "neo unavailable" in body, body)


def main() -> int:
    _direct_dispatch_no_endpoint_contract()
    _hooked_session_contract()
    _adhoc_no_neo_contract()
    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - dispatch mandatorily injects wake-packet rules/context.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
