#!/usr/bin/env python3
"""Acceptance: wake packets lead with terse state-adapted operating instructions."""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fleet_orchestrator.context_assembler as assembler  # noqa: E402


FAILURES: list[str] = []
MAX_OPERATING_BYTES = 950


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _packet_for(resolved_work: dict[str, object]) -> str:
    context = {
        "overall_refs": [],
        "supervisor_refs": [],
        "project_refs": [],
        "phase_refs": [],
        "task_refs": [],
        "memory": [],
        "rules": [],
        "snapshot": {
            "repo_head": "test",
            "session_id": "sup",
            "cli": "codex",
            "requested_task_id": None,
            "resolved_work": resolved_work,
            "memory_files": [],
            "rules_files": [],
        },
        "budget_used": 0,
    }
    packet = assembler.build_packet("sup-codex", context)
    return assembler.assemble(packet, "codex")


def _selected_packet_for(session: str, root: Path, cli: str = "codex", root_session: str | None = None) -> str:
    roots = {root_session or session: str(root)}
    with mock.patch.object(assembler, "get_session_next_ready", return_value=None), \
         mock.patch.object(assembler, "get_session_current_work", return_value=None), \
         mock.patch.object(assembler, "get_session_supervised_projects", return_value=[]), \
         mock.patch.object(assembler, "get_overall_refs", return_value={"ref_context": {"refs": []}}), \
         mock.patch.object(assembler, "get_supervisor_refs", return_value={"ref_context": {"refs": []}}):
        context = assembler.select_context(session, cli=cli, session_roots=roots)
    return assembler.assemble(assembler.build_packet(session, context), cli)


def _operating_section(rendered: str) -> str:
    marker = "## Operating"
    next_marker = "\n## Identity"
    if marker not in rendered or next_marker not in rendered:
        return ""
    return rendered.split(marker, 1)[1].split(next_marker, 1)[0].strip()


def _assert_first_and_bounded(label: str, rendered: str) -> str:
    section = _operating_section(rendered)
    _check(f"{label}: Operating section exists", bool(section), rendered)
    _check(
        f"{label}: Operating before Context Refs",
        rendered.index("## Provenance") < rendered.index("## Operating") < rendered.index("## Identity") < rendered.index("## Context Refs"),
        rendered,
    )
    _check(
        f"{label}: Operating section bounded",
        len(section.encode("utf-8")) <= MAX_OPERATING_BYTES,
        section,
    )
    return section


def _rendering_contract() -> None:
    none_section = _assert_first_and_bounded("none", _packet_for({"source": "none"}))
    _check("none source names taey-plan next", "taey-plan next" in none_section, none_section)
    _check("none source names taey-plan ingest", "taey-plan ingest" in none_section, none_section)
    _check("none source does not dispatch", "taey-task dispatch" not in none_section, none_section)
    _check("none source without root has no access affordance", "registered supervisor" not in none_section, none_section)

    pending_section = _assert_first_and_bounded(
        "pending",
        _packet_for({"source": "pending", "status": "pending", "task_id": "demo::build", "owner": "sup-gemini"}),
    )
    _check("pending source names dispatch verb", "taey-task dispatch" in pending_section, pending_section)
    _check("pending source warns bare notify does not bind", "Bare `taey-notify` does NOT bind" in pending_section, pending_section)
    _check("pending source says BINDS", "BINDS owner+current_task" in pending_section, pending_section)

    own_section = _assert_first_and_bounded(
        "own in_progress",
        _packet_for({"source": "in_progress_own", "status": "in_progress", "task_id": "demo::build", "owner": "sup"}),
    )
    _check("own in_progress names update verb", "taey-task update demo::build completed" in own_section, own_section)
    _check("own in_progress requires evidence", "--evidence" in own_section, own_section)
    _check("own in_progress warns evidence-less rejected", "Evidence-less terminal writes are REJECTED" in own_section, own_section)

    dispatched_section = _assert_first_and_bounded(
        "dispatched in_progress",
        _packet_for({
            "source": "in_progress_own",
            "status": "in_progress",
            "task_id": "demo::build",
            "owner": "sup",
            "dispatched_to": "sup-codex",
        }),
    )
    _check("dispatched in_progress says RESPONSE_READY", "RESPONSE_READY" in dispatched_section, dispatched_section)
    _check("dispatched in_progress says record_outcome", "record_outcome" in dispatched_section, dispatched_section)
    _check("dispatched in_progress forbids terminal tracker write", "Do NOT run `taey-task update demo::build completed`" in dispatched_section, dispatched_section)
    _check("dispatched in_progress does not say drive terminal", "Drive `demo::build` to terminal" not in dispatched_section, dispatched_section)

    peer_section = _assert_first_and_bounded(
        "peer reported done",
        _packet_for({
            "source": "peer_reported_done",
            "status": "in_progress",
            "task_id": "demo::review",
            "owner": "sup-gemini",
        }),
    )
    _check("peer done says verify", "verify" in peer_section, peer_section)
    _check("peer done forbids peer self-complete", "Peers cannot self-complete" in peer_section, peer_section)
    _check("peer done still names evidence closure", "--evidence" in peer_section, peer_section)

    blocked_section = _assert_first_and_bounded(
        "blocked downstream",
        _packet_for({
            "source": "explicit_task",
            "status": "pending",
            "task_id": "demo::blocked",
            "blocked_on": "demo::dep",
        }),
    )
    _check("blocked source names blocker", "blocked on `demo::dep`" in blocked_section, blocked_section)
    _check("blocked source names completed-only deps", "deps are `completed`" in blocked_section, blocked_section)


def _resolver_contract() -> None:
    current = {"top_task_id": "sup::current", "top_task_desc": "current work", "project_id": "p", "phase_id": "ph"}
    ready = {"task_id": "sup::ready", "description": "ready work", "project_id": "p", "phase_id": "ph", "owner": "sup"}
    peer_pending = {"task_id": "p::peer", "description": "peer pending", "owner": "sup-gemini", "project_id": "p"}
    peer_done = {"task_id": "p::done", "description": "peer done", "owner": "sup-gemini", "project_id": "p"}
    project = {"id": "p", "name": "project", "status": "active", "priority": 1}

    with mock.patch.object(assembler, "get_session_next_ready", return_value=ready), \
         mock.patch.object(assembler, "get_session_current_work", return_value=current), \
         mock.patch.object(assembler, "get_session_supervised_projects", return_value=[project]), \
         mock.patch.object(assembler, "get_supervisor_dispatchable_peer_task", return_value=peer_pending), \
         mock.patch.object(assembler, "get_supervisor_inflight_peer_task", return_value=peer_done), \
         mock.patch.object(assembler, "get_task_step_governance", return_value={}):
        work = assembler._resolve_work("sup", None)
    _check("resolver surfaces current work before pending", work.get("source") == "in_progress_own" and work.get("task_id") == "sup::current", work)

    with mock.patch.object(assembler, "get_session_next_ready", return_value=ready), \
         mock.patch.object(assembler, "get_session_current_work", return_value=None), \
         mock.patch.object(assembler, "get_session_supervised_projects", return_value=[project]), \
         mock.patch.object(assembler, "get_supervisor_dispatchable_peer_task", return_value=peer_pending), \
         mock.patch.object(assembler, "get_supervisor_inflight_peer_task", return_value=peer_done):
        work = assembler._resolve_work("sup", None)
    _check("resolver still surfaces pending when no current work exists", work.get("source") == "pending" and work.get("task_id") == "sup::ready", work)

    with mock.patch.object(assembler, "get_session_next_ready", return_value=None), \
         mock.patch.object(assembler, "get_session_current_work", return_value=None), \
         mock.patch.object(assembler, "get_session_supervised_projects", return_value=[project]), \
         mock.patch.object(assembler, "get_supervisor_dispatchable_peer_task", return_value=peer_pending), \
         mock.patch.object(assembler, "get_supervisor_inflight_peer_task", return_value=peer_done):
        work = assembler._resolve_work("sup", None)
    _check("resolver surfaces peer-dispatchable work", work.get("source") == "pending" and work.get("task_id") == "p::peer", work)

    with mock.patch.object(assembler, "get_session_next_ready", return_value=None), \
         mock.patch.object(assembler, "get_session_current_work", return_value=None), \
         mock.patch.object(assembler, "get_session_supervised_projects", return_value=[project]), \
         mock.patch.object(assembler, "get_supervisor_dispatchable_peer_task", return_value=None), \
         mock.patch.object(assembler, "get_supervisor_inflight_peer_task", return_value=peer_done):
        work = assembler._resolve_work("sup", None)
    _check("resolver surfaces peer-reported-done work", work.get("source") == "peer_reported_done" and work.get("task_id") == "p::done", work)

    with mock.patch.object(assembler, "get_session_next_ready", return_value=None), \
         mock.patch.object(assembler, "get_session_current_work", return_value=current), \
         mock.patch.object(assembler, "get_session_supervised_projects", return_value=[]), \
         mock.patch.object(assembler, "get_task_step_governance", return_value={}):
        work = assembler._resolve_work("sup", None)
    _check("resolver surfaces own in-progress work", work.get("source") == "in_progress_own" and work.get("task_id") == "sup::current", work)


def _operating_source_contract() -> None:
    cases = [
        ("none stays none", {"source": "none"}, None, "none"),
        ("pending stays pending", {"source": "pending", "status": "pending"}, None, "pending"),
        ("status pending maps without explicit source", {"status": "pending"}, None, "pending"),
        ("explicit pending task maps to pending", {"source": "explicit_task", "status": "pending"}, "demo::pending", "pending"),
        (
            "explicit in-progress task maps to own work",
            {"source": "explicit_task", "status": "in_progress"},
            "demo::active",
            "in_progress_own",
        ),
        (
            "status in-progress maps without explicit source",
            {"status": "in_progress"},
            None,
            "in_progress_own",
        ),
        (
            "peer reported done is preserved",
            {"source": "peer_reported_done", "status": "in_progress"},
            None,
            "peer_reported_done",
        ),
        ("explicit unknown stays explicit", {"source": "explicit_task"}, "demo::unknown", "explicit_task"),
    ]
    for label, work, task_id, expected in cases:
        actual = assembler._operating_source(work, task_id)
        _check(f"operating source: {label}", actual == expected, {"actual": actual, "expected": expected})


def _snapshot_source_chain_contract() -> None:
    work = {
        "source": "explicit_task",
        "status": "in_progress",
        "task_id": "demo::active",
        "description": "active explicit task",
        "owner": "sup",
    }
    context = {
        "overall_refs": [],
        "supervisor_refs": [],
        "project_refs": [],
        "phase_refs": [],
        "task_refs": [],
        "memory": [],
        "rules": [],
        "snapshot": assembler._build_snapshot("sup", "codex", "demo::active", work, None, [], []),
        "budget_used": 0,
    }
    rendered = assembler.assemble(assembler.build_packet("sup-codex", context), "codex")
    section = _operating_section(rendered)
    _check(
        "snapshot chain maps explicit in-progress source",
        context["snapshot"]["resolved_work"]["source"] == "in_progress_own",
        context["snapshot"]["resolved_work"],
    )
    _check(
        "snapshot chain renders own in-progress variant",
        "taey-task update demo::active completed" in section,
        section,
    )


def _supervisor_affordance_contract() -> None:
    first_root = Path("/tmp/taeys-hands-root")
    original_ids = os.environ.get("ORCH_SESSION_IDS")
    original_roots = os.environ.get("ORCH_SESSION_ROOTS")
    try:
        os.environ["ORCH_SESSION_IDS"] = "taeys-hands,weaver"
        os.environ["ORCH_SESSION_ROOTS"] = '{"taeys-hands":"/tmp/wrong-global-root"}'
        first_section = _assert_first_and_bounded(
            "supervisor affordance first",
            _selected_packet_for("taeys-hands", first_root, cli="codex"),
        )
        _check("affordance names exact supervisor", "You are `taeys-hands`, a registered supervisor" in first_section, first_section)
        _check("affordance uses resolved scoped plan/ref root", f"plan/ref root is `{first_root}`" in first_section, first_section)
        _check("affordance ignores stale global root", "wrong-global-root" not in first_section, first_section)
        _check("affordance names exact ingest command", "`taey-plan ingest <file under it>`" in first_section, first_section)
        _check("affordance says never request access", "you never request access" in first_section, first_section)

        peer_section = _assert_first_and_bounded(
            "unregistered peer affordance",
            _selected_packet_for("taeys-hands-codex", first_root, cli="codex", root_session="taeys-hands"),
        )
        _check("unregistered peer has no supervisor affordance", "registered supervisor" not in peer_section, peer_section)
        _check("unregistered peer does not leak base root", str(first_root) not in peer_section, peer_section)
    finally:
        if original_ids is None:
            os.environ.pop("ORCH_SESSION_IDS", None)
        else:
            os.environ["ORCH_SESSION_IDS"] = original_ids
        if original_roots is None:
            os.environ.pop("ORCH_SESSION_ROOTS", None)
        else:
            os.environ["ORCH_SESSION_ROOTS"] = original_roots

    second_root = Path("/tmp/weaver-root")
    try:
        os.environ["ORCH_SESSION_IDS"] = "weaver"
        second_section = _assert_first_and_bounded(
            "supervisor affordance second",
            _selected_packet_for("weaver", second_root, cli="grok"),
        )
        _check("second affordance names exact supervisor", "You are `weaver`, a registered supervisor" in second_section, second_section)
        _check("second affordance names plan/ref root", f"plan/ref root is `{second_root}`" in second_section, second_section)
    finally:
        if original_ids is None:
            os.environ.pop("ORCH_SESSION_IDS", None)
        else:
            os.environ["ORCH_SESSION_IDS"] = original_ids


def main() -> int:
    _rendering_contract()
    _resolver_contract()
    _operating_source_contract()
    _snapshot_source_chain_contract()
    _supervisor_affordance_contract()
    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - wake packet Operating section is first, bounded, and state-adapted.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
