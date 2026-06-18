"""Acceptance: session .env values are request-scoped context hints only."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fleet_orchestrator.context_assembler as assembler  # noqa: E402


FAILURES: list[str] = []
ENV_KEYS = ("ORCH_RULES_ROOT", "ORCH_SESSION_ROOTS")


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


@contextmanager
def _preserved_env() -> Iterator[None]:
    original = {key: os.environ.get(key) for key in ENV_KEYS}
    try:
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        yield
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextmanager
def _context_mocks() -> Iterator[None]:
    empty_refs = {"ref_context": {"refs": []}}
    with mock.patch.object(assembler, "get_session_next_ready", return_value=None), \
         mock.patch.object(assembler, "get_session_current_work", return_value=None), \
         mock.patch.object(assembler, "get_session_supervised_projects", return_value=[]), \
         mock.patch.object(assembler, "get_overall_refs", return_value=empty_refs), \
         mock.patch.object(assembler, "get_supervisor_refs", return_value=empty_refs):
        yield


def _rules_text(context: dict[str, object]) -> str:
    return "\n".join(str(rule.get("text", "")) for rule in context.get("rules") or [])


def _write_rule(root: Path, session: str, text: str) -> None:
    path = root / "supervisors" / f"{session}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text + "\n", encoding="utf-8")


def _scoped_rules_override_operator_global() -> None:
    with _preserved_env(), tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        session_root = tmp / "alpha-session"
        session_root.mkdir()
        operator_rules = tmp / "operator-rules"
        scoped_rules = tmp / "scoped-rules"
        _write_rule(operator_rules, "alpha", "OPERATOR_ALPHA_RULE")
        _write_rule(scoped_rules, "alpha", "SCOPED_ALPHA_RULE")
        (session_root / ".env").write_text(f"ORCH_RULES_ROOT={scoped_rules}\n", encoding="utf-8")

        os.environ["ORCH_SESSION_ROOTS"] = json.dumps({"alpha": str(session_root)})
        os.environ["ORCH_RULES_ROOT"] = str(operator_rules)
        before = dict(os.environ)
        with _context_mocks():
            context = assembler.select_context("alpha-codex", cli="codex")
        after = dict(os.environ)

    text = _rules_text(context)
    _check("session ORCH_RULES_ROOT overrides operator global only for this request", "SCOPED_ALPHA_RULE" in text and "OPERATOR_ALPHA_RULE" not in text, text)
    _check("scoped rules request does not mutate os.environ", after == before, {"before": {k: before.get(k) for k in ENV_KEYS}, "after": {k: after.get(k) for k in ENV_KEYS}})


def _no_rules_root_leaks_to_next_session() -> None:
    with _preserved_env(), tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        alpha_root = tmp / "alpha-session"
        beta_root = tmp / "beta-session"
        alpha_root.mkdir()
        beta_root.mkdir()
        scoped_rules = tmp / "alpha-scoped-rules"
        _write_rule(scoped_rules, "alpha", "ALPHA_ONLY_RULE")
        _write_rule(scoped_rules, "beta", "BETA_LEAK_FROM_ALPHA_RULE")
        (alpha_root / ".env").write_text(f"ORCH_RULES_ROOT={scoped_rules}\n", encoding="utf-8")

        os.environ["ORCH_SESSION_ROOTS"] = json.dumps({
            "alpha": str(alpha_root),
            "beta": str(beta_root),
        })
        before = dict(os.environ)
        with _context_mocks():
            alpha_context = assembler.select_context("alpha-codex", cli="codex")
            mid = dict(os.environ)
            beta_context = assembler.select_context("beta-codex", cli="codex")
        after = dict(os.environ)

    alpha_text = _rules_text(alpha_context)
    beta_text = _rules_text(beta_context)
    _check("session A sees its scoped rules", "ALPHA_ONLY_RULE" in alpha_text, alpha_text)
    _check("session B does not inherit session A scoped rules", "BETA_LEAK_FROM_ALPHA_RULE" not in beta_text, beta_text)
    _check("rules-root requests do not mutate os.environ", before == mid == after, {"before": {k: before.get(k) for k in ENV_KEYS}, "mid": {k: mid.get(k) for k in ENV_KEYS}, "after": {k: after.get(k) for k in ENV_KEYS}})


def _session_roots_are_request_scoped() -> None:
    with _preserved_env(), tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        bootstrap_root = tmp / "bootstrap-session"
        actual_root = tmp / "actual-session"
        memory_root = tmp / "memory-base"
        bootstrap_root.mkdir()
        actual_root.mkdir()
        scoped_roots = {"gamma": str(actual_root)}
        (bootstrap_root / ".env").write_text(f"ORCH_SESSION_ROOTS={json.dumps(scoped_roots)}\n", encoding="utf-8")

        memory_dir = memory_root / assembler._mangle_project_path(str(actual_root)) / "memory"
        memory_dir.mkdir(parents=True)
        (memory_dir / "MEMORY.md").write_text(
            "---\nname: MEMORY\ndescription: scoped roots\n---\nSCOPED_SESSION_ROOT_MEMORY\n",
            encoding="utf-8",
        )

        old_memory_base = assembler.MEMORY_BASE
        assembler.MEMORY_BASE = memory_root
        before = dict(os.environ)
        try:
            with _context_mocks():
                context = assembler.select_context(
                    "gamma-codex",
                    cli="codex",
                    session_roots={"gamma": str(bootstrap_root)},
                )
            after = dict(os.environ)
        finally:
            assembler.MEMORY_BASE = old_memory_base

    rendered_memory = "\n".join(str(item.get("content", "")) for item in context.get("memory") or [])
    _check("scoped ORCH_SESSION_ROOTS drives current request memory lookup", "SCOPED_SESSION_ROOT_MEMORY" in rendered_memory, context)
    _check("scoped session roots request does not mutate os.environ", after == before, {"before": {k: before.get(k) for k in ENV_KEYS}, "after": {k: after.get(k) for k in ENV_KEYS}})


def main() -> int:
    _scoped_rules_override_operator_global()
    _no_rules_root_leaks_to_next_session()
    _session_roots_are_request_scoped()
    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - session .env context hints are request-scoped.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
