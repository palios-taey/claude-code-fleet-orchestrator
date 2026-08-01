#!/usr/bin/env python3
"""Acceptance: wake packets carry a task-ref injection receipt echo."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fleet_orchestrator.context_assembler as assembler  # noqa: E402
import fleet_orchestrator.dispatch as dispatch_module  # noqa: E402
import fleet_orchestrator.tasks_api as tasks_api  # noqa: E402


FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _context() -> dict[str, object]:
    return {
        "overall_refs": [{"path": "overall.md", "label": "overall", "content": "overall"}],
        "supervisor_refs": [],
        "project_refs": [{"path": "project.md", "label": "project", "content": "project"}],
        "phase_refs": [],
        "task_refs": [
            {"path": "voice.md", "label": "voice guide", "l_start": 1, "l_end": 3, "content": "voice"},
            {"path": "plans/step.md", "l_start": 7, "l_end": 9, "content": "step"},
            {"path": "extra.md", "label": "extra", "l_start": 1, "l_end": 1, "content": "extra"},
        ],
        "memory": [],
        "rules": [],
        "snapshot": {
            "repo_head": "acceptance",
            "session_id": "worker",
            "cli": "codex",
            "requested_task_id": "proj::task",
            "resolved_work": {
                "source": "in_progress_own",
                "status": "in_progress",
                "task_id": "proj::task",
                "owner": "supervisor",
                "dispatched_to": "worker-codex",
            },
            "memory_files": [],
            "rules_files": [],
        },
        "budget_used": 0,
    }


def _operating_section(rendered: str) -> str:
    return rendered.split("## Operating", 1)[1].split("\n## Identity", 1)[0]


def _assembler_receipt_contract() -> None:
    packet = assembler.build_packet("worker-codex", _context())
    rendered = assembler.assemble(packet, "codex", max_refs_per_tier=2)
    section = _operating_section(rendered)
    receipt = assembler.task_ref_receipt(packet, max_refs_per_tier=2)
    expected = "loaded refs: voice_guide,plans/step.md:L7-L9"

    _check("assembler receipt uses rendered task refs", receipt["line"] == expected, receipt)
    _check("operating section tells executor to echo receipt first", f"reply exactly `{expected}`" in section, section)
    _check("receipt excludes project/overall refs", "project" not in receipt["line"] and "overall" not in receipt["line"], receipt)
    _check("receipt honors render cap", "extra" not in receipt["line"] and "extra.md" not in receipt["line"], receipt)
    _check("capped task ref is absent from rendered task section", "extra.md" not in rendered, rendered)


def _endpoint_metadata_contract() -> None:
    client = TestClient(tasks_api.app)
    old_endpoint = os.environ.get("ORCH_WAKE_PACKET_ENDPOINT_ENABLED")
    os.environ["ORCH_WAKE_PACKET_ENDPOINT_ENABLED"] = "1"
    receipts: list[tuple[str, dict[str, object]]] = []
    try:
        with mock.patch.object(tasks_api, "_cfg", return_value=SimpleNamespace(session_ids=["worker-codex"])), \
             mock.patch.object(tasks_api, "select_wake_context", return_value=_context()), \
             mock.patch.object(tasks_api, "maybe_emit_decision_receipt", side_effect=lambda kind, ctx: receipts.append((kind, ctx))):
            response = client.get("/api/sessions/worker-codex/wake-packet?cli=codex")
    finally:
        if old_endpoint is None:
            os.environ.pop("ORCH_WAKE_PACKET_ENDPOINT_ENABLED", None)
        else:
            os.environ["ORCH_WAKE_PACKET_ENDPOINT_ENABLED"] = old_endpoint

    body = response.json()
    meta_receipt = body.get("packet_meta", {}).get("injection_receipt", {})
    receipt_ctx = receipts[0][1] if receipts else {}
    _check("endpoint returns expected injection receipt metadata", meta_receipt.get("line") == "loaded refs: voice_guide,plans/step.md:L7-L9,extra", body)
    _check("endpoint packet carries first-action receipt instruction", "reply exactly `loaded refs: voice_guide,plans/step.md:L7-L9,extra`" in body.get("packet", ""), body)
    _check("endpoint decision receipt next_contract names expected echo", "loaded refs: voice_guide,plans/step.md:L7-L9,extra" in str(receipt_ctx.get("next_contract") or ""), receipt_ctx)
    _check(
        "endpoint metadata carries proof capsule",
        str(body.get("packet_meta", {}).get("proof_capsule", {}).get("world_id") or "").startswith("world:")
        and str(receipt_ctx.get("world_id") or "").startswith("world:"),
        {"body": body, "receipt": receipt_ctx},
    )


def _dispatch_metadata_contract() -> None:
    context = _context()
    old_manifest = os.environ.get("ORCH_WORLD_MANIFEST_PATH")
    old_ledger = os.environ.get("ORCH_CAUSAL_LEDGER_PATH")
    with tempfile.TemporaryDirectory(prefix="injection-world-") as raw:
        root = Path(raw)
        os.environ["ORCH_WORLD_MANIFEST_PATH"] = str(root / "world-manifest-v0.json")
        os.environ["ORCH_CAUSAL_LEDGER_PATH"] = str(root / "causal-events.jsonl")
        try:
            with mock.patch.object(dispatch_module, "_select_dispatch_context", return_value=(context, "")):
                rendered, meta = dispatch_module._assemble_dispatch_prompt(
                    "worker-codex",
                    "proj::task",
                    "dispatch with task refs",
                    "supervisor",
                    "supervisor",
                    "DISPATCH BODY",
                    causal_event_ids=["event:dispatch-claimed"],
                )
            rows = [
                json.loads(line)
                for line in (root / "causal-events.jsonl").read_text(encoding="utf-8").splitlines()
                if line and not line.startswith("#")
            ]
        finally:
            if old_manifest is None:
                os.environ.pop("ORCH_WORLD_MANIFEST_PATH", None)
            else:
                os.environ["ORCH_WORLD_MANIFEST_PATH"] = old_manifest
            if old_ledger is None:
                os.environ.pop("ORCH_CAUSAL_LEDGER_PATH", None)
            else:
                os.environ["ORCH_CAUSAL_LEDGER_PATH"] = old_ledger
    _check("dispatch packet carries first-action receipt instruction", "reply exactly `loaded refs: voice_guide,plans/step.md:L7-L9,extra`" in rendered, rendered)
    _check("dispatch metadata carries expected injection receipt", meta.get("injection_receipt", {}).get("line") == "loaded refs: voice_guide,plans/step.md:L7-L9,extra", meta)
    _check(
        "dispatch metadata carries pre-assembly proof capsule",
        "## Proof Capsule" in rendered
        and str(meta.get("world_id") or "").startswith("world:")
        and meta.get("proof_capsule", {}).get("causal_event_ids", [None])[0] == "event:dispatch-claimed"
        and str(meta.get("world_manifest_event_id") or "").startswith("event:")
        and str(meta.get("world_manifest_sha256") or ""),
        meta,
    )
    _check("dispatch assembly publishes world manifest event", rows[-1]["event"]["event_type"] == "world_manifest_published", rows)


def main() -> int:
    _assembler_receipt_contract()
    _endpoint_metadata_contract()
    _dispatch_metadata_contract()
    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - wake packets expose a first-action task-ref injection receipt.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
