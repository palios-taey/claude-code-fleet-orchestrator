"""Ship-gate e2e — dynamic wake packet endpoint is additive, gated, and provenance-bound."""
from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lib.context_assembler as assembler  # noqa: E402
import lib.tasks_api as tasks_api  # noqa: E402


FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _client() -> TestClient:
    return TestClient(tasks_api.app)


def _endpoint_contract() -> None:
    client = _client()
    os.environ.pop("ORCH_WAKE_PACKET_ENABLED", None)
    with mock.patch.object(tasks_api, "select_wake_context", side_effect=RuntimeError("should not assemble")):
        disabled = client.get("/api/sessions/conductor-codex/wake-packet?cli=codex")
    _check("disabled wake packet is a config-free no-op", disabled.status_code == 200 and disabled.json() == {"ok": True, "enabled": False}, disabled.text)

    os.environ["ORCH_WAKE_PACKET_ENABLED"] = "1"
    with mock.patch.object(tasks_api, "_cfg", return_value=SimpleNamespace(session_ids=["conductor-codex"])):
        invalid = client.get("/api/sessions/conductor-codex/wake-packet?cli=bogus")
    _check("invalid cli is rejected without 500", invalid.status_code == 400, invalid.text)

    context = {
        "overall_refs": [],
        "supervisor_refs": [],
        "project_refs": [],
        "phase_refs": [],
        "task_refs": [],
        "memory": [{"name": "MEMORY", "type": "reference", "description": "wake", "content": "remember the task"}],
        "rules": [{"scope": "supervisor", "text": "stay within budget", "path": "/tmp/rules.md", "sha256": "abc", "mtime_ns": 1}],
        "snapshot": {"repo_head": "abc123", "memory_files": [], "rules_files": []},
        "budget_used": 0,
    }
    with mock.patch.object(tasks_api, "_cfg", return_value=SimpleNamespace(session_ids=["conductor-codex"])), \
         mock.patch.object(tasks_api, "select_wake_context", return_value=context):
        ok = client.get("/api/sessions/conductor-codex/wake-packet?cli=codex")
    body = ok.json()
    _check("enabled wake packet returns rendered packet", ok.status_code == 200 and body.get("ok") is True and body.get("enabled") is True and bool(body.get("packet")), body)
    _check("enabled wake packet returns provenance metadata", bool(body.get("packet_meta", {}).get("provenance_hash")) and body["packet_meta"]["size_report"]["under_budget"] is True, body)

    with mock.patch.object(tasks_api, "_cfg", return_value=SimpleNamespace(session_ids=["conductor-codex"])), \
         mock.patch.object(tasks_api, "select_wake_context", side_effect=RuntimeError("assembler boom")):
        failed = client.get("/api/sessions/conductor-codex/wake-packet?cli=codex")
    _check("assembler failure is fail-open JSON not 500", failed.status_code == 200 and failed.json().get("ok") is False and "assembler boom" in failed.json().get("error", ""), failed.text)
    os.environ.pop("ORCH_WAKE_PACKET_ENABLED", None)


def _assembler_contract() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        session_root = tmp / "session"
        session_root.mkdir()
        source_root = tmp / "project"
        source_root.mkdir()
        source = source_root / "plan.md"
        source.write_text("plan", encoding="utf-8")

        memory_root = tmp / "memory"
        mangled = assembler._mangle_project_path(str(session_root))
        memory_dir = memory_root / mangled / "memory"
        memory_dir.mkdir(parents=True)
        (memory_dir / "MEMORY.md").write_text(
            "---\nname: MEMORY\ndescription: wake task rules\n---\nUse the selected memory.\n",
            encoding="utf-8",
        )

        rules_root = tmp / "rules"
        (rules_root / "supervisors").mkdir(parents=True)
        (rules_root / "projects").mkdir(parents=True)
        (rules_root / "supervisors" / "conductor.md").write_text("Supervisor wake rule", encoding="utf-8")
        (rules_root / "projects" / "dynctx.md").write_text("Project wake rule", encoding="utf-8")

        summary = {
            "project": {"id": "dynctx", "name": "Dynamic Context", "description": "wake task rules", "source_path": str(source)},
            "phases": [
                {
                    "phase": {"id": "dynctx::w1", "name": "W1"},
                    "tasks": [{"id": "dynctx::w1-build", "description": "wake task rules"}],
                }
            ],
            "ref_tiers": {},
        }

        old_memory_base = assembler.MEMORY_BASE
        old_rules_root = os.environ.get("ORCH_RULES_ROOT")
        os.environ["ORCH_RULES_ROOT"] = str(rules_root)
        assembler.MEMORY_BASE = memory_root
        try:
            with mock.patch.object(assembler, "get_task_project", return_value={"project_id": "dynctx", "project_name": "Dynamic Context"}), \
                 mock.patch.object(assembler, "get_project_summary", return_value=summary):
                context = assembler.select_context(
                    "conductor-codex",
                    task_id="dynctx::w1-build",
                    cli="codex",
                    session_roots={"conductor": str(session_root)},
                )
            packet = assembler.build_packet("conductor-codex", context)
            rendered = assembler.assemble(packet, "codex")
            report = assembler.size_report(rendered, packet)
        finally:
            assembler.MEMORY_BASE = old_memory_base
            if old_rules_root is None:
                os.environ.pop("ORCH_RULES_ROOT", None)
            else:
                os.environ["ORCH_RULES_ROOT"] = old_rules_root

    snapshot = packet.get("snapshot", {})
    _check("select_context reloads supplied session roots per request", context["memory"] and "Use the selected memory." in context["memory"][0]["content"], context)
    _check("rules_tier is the assembler rule source", len(context["rules"]) == 2 and all("sha256" in rule for rule in context["rules"]), context["rules"])
    _check("snapshot carries memory and rules fingerprints", bool(snapshot.get("memory_files")) and len(snapshot.get("rules_files") or []) == 2, snapshot)
    _check("provenance binds rendered packet plus snapshot", bool(packet.get("provenance_hash")) and report["under_budget"] is True and "AGENTS.md Dynamic Context" in rendered, report)


def _empty_work_context_contract() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        session_root = tmp / "actual-session-root"
        session_root.mkdir()
        memory_root = tmp / "memory"
        memory_dir = memory_root / assembler._mangle_project_path(str(session_root)) / "memory"
        memory_dir.mkdir(parents=True)
        (memory_dir / "MEMORY.md").write_text(
            "---\nname: MEMORY\ndescription: standing context\n---\nCarry the standing session memory.\n",
            encoding="utf-8",
        )
        overall = {"ref_context": {"refs": [{"path": "/tmp/overall.md", "content": "overall ref"}]}}
        supervisor = {"ref_context": {"refs": [{"path": "/tmp/supervisor.md", "content": "supervisor ref"}]}}

        old_memory_base = assembler.MEMORY_BASE
        assembler.MEMORY_BASE = memory_root
        try:
            with mock.patch.object(assembler, "get_session_next_ready", return_value=None), \
                 mock.patch.object(assembler, "get_overall_refs", return_value=overall), \
                 mock.patch.object(assembler, "get_supervisor_refs", return_value=supervisor):
                context = assembler.select_context(
                    "conductor-codex",
                    cli="codex",
                    session_roots={"conductor-codex": str(session_root)},
                )
        finally:
            assembler.MEMORY_BASE = old_memory_base

    _check("no-current-task still selects MEMORY.md from actual session root",
           context["memory"] and "standing session memory" in context["memory"][0]["content"], context)
    _check("no-current-task still includes overall refs",
           context["overall_refs"] and context["overall_refs"][0]["content"] == "overall ref", context)
    _check("no-current-task still includes supervisor refs",
           context["supervisor_refs"] and context["supervisor_refs"][0]["content"] == "supervisor ref", context)


def _memory_traversal_contract() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        memory_root = tmp / "memory-base"
        memory_root.mkdir()
        outside = tmp / "memory"
        outside.mkdir()
        outside_secret = "OUTSIDE_MEMORY_SHOULD_NOT_RENDER"
        (outside / "SECRET.md").write_text(outside_secret, encoding="utf-8")

        abs_dir = tmp / "absolute-session" / "memory"
        abs_dir.mkdir(parents=True)
        absolute_secret = "ABSOLUTE_MEMORY_SHOULD_NOT_RENDER"
        (abs_dir / "SECRET.md").write_text(absolute_secret, encoding="utf-8")

        old_memory_base = assembler.MEMORY_BASE
        assembler.MEMORY_BASE = memory_root
        try:
            cases = ["..", "../..", str(abs_dir.parent), "bad\x00session"]
            for session in cases:
                try:
                    dirs = assembler._memory_dirs(session, {"project_id": None}, None, {})
                    items = assembler._read_memory_files(dirs)
                    rendered = "\n".join(str(item.get("content", "")) for item in items)
                    _check(
                        f"memory traversal blocked for {session!r}",
                        not dirs and outside_secret not in rendered and absolute_secret not in rendered,
                        {"dirs": [str(path) for path in dirs], "rendered": rendered},
                    )
                except ValueError:
                    _check(f"memory traversal rejected for {session!r}", True)
        finally:
            assembler.MEMORY_BASE = old_memory_base


def main() -> int:
    _endpoint_contract()
    _assembler_contract()
    _empty_work_context_contract()
    _memory_traversal_contract()
    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - wake packet endpoint and assembler contracts hold.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
