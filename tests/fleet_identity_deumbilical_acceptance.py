"""Acceptance: shipped orchestrator surfaces do not embed private fleet identity.

This is the process gate for the v1.8.1 de-umbilical fix: the gate template must
ship generic role placeholders when ORCH_GATE_OWNERS is unset, support operator
mapping from env, and the dashboard chat target must derive from /api/sessions
rather than a hardcoded private session.
"""
from __future__ import annotations

import os
import re
import sys
import ast
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet_orchestrator.evidence_verification import (  # noqa: E402
    COMPLETION_ALLOWLIST_UNSET_WARNING,
    allowed_completion_repos,
    warn_if_completion_allowlist_unset,
)
from fleet_orchestrator.orch_template import apply_gate_template  # noqa: E402


ROOT = Path(__file__).resolve().parents[1]
FAILURES: list[str] = []
FLEET_OWNER_NAMES = {
    "gemini",
    "codex",
    "grok",
    "conductor",
    "family",
    "weaver",
    "tutor",
    "infra",
    "taeys-hands",
    "treasurer",
    "hunter",
    "taey-ed",
}
PRIVATE_NAME_PATTERN = re.compile(
    # operator personal name(s) — a repo-grounded audit (2026-06-15) found `"jesse"`
    # hardcoded as a default in tasks_api.py + chat_layer.py that this scan had
    # missed because the pattern only listed fleet roles/codenames, not the operator.
    r"(?i:\bjesse\b)"
    r"|(?i:\b(conductor|weaver|tutor|treasurer|hunter|taey-ed)\b|taeys-hands)"
    r"|\b(Gaia|Logos|Cosmos|Clarity|Horizon|Prophet|Brain|Math|PATHOS|POTENTIAL|TRUTH)\b"
    r"|\bthe Map\b|\bthe Family\b|\bFamily\b",
)
OPERATOR_ORG_REPO_PATTERN = re.compile(r"\bpalios-taey/[A-Za-z0-9_.-]+\b")
SHIPPED_SURFACES = [
    "README.md",
    ".env.example",  # the file README tells users to `cp` — a 2026-06-15 DR audit found
                     # the whole fleet topology (conductor/weaver/.../project IDs) baked here.
    "docs/CAPABILITIES.md",
    "docs/SHIPPABILITY.md",
    "fleet_orchestrator/accountability_ledger.py",
    "fleet_orchestrator/chat_layer.py",
    "fleet_orchestrator/config.py",
    "fleet_orchestrator/orch_template.py",
    "fleet_orchestrator/orch_schema.py",
    "fleet_orchestrator/context_assembler.py",
    "fleet_orchestrator/dispatch.py",
    "fleet_orchestrator/evidence_verification.py",
    "fleet_orchestrator/loop_engine.py",
    "fleet_orchestrator/plan_loader.py",
    "fleet_orchestrator/plan_readiness.py",
    "fleet_orchestrator/tasks_api.py",
    "scripts/orch-cron",
    "scripts/taey-task",
    "scripts/orch-watch",
    "scripts/verify-public-readonly.py",
    "ui/index.html",
    "ui/static/app.js",
]
CONFIG_DEFAULT_SURFACES = sorted(str(path.relative_to(ROOT)) for path in (ROOT / "fleet_orchestrator").glob("*.py"))


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _gate_plan() -> dict:
    return {
        "project": {"id": "demo"},
        "phases": [
            {
                "id": "build",
                "tasks": [
                    {"id": "work", "description": "work", "owner": "worker", "depends": []},
                ],
            },
        ],
    }


def _gate_tasks(plan: dict) -> list[dict]:
    for phase in plan["phases"]:
        if phase.get("id") == "forced-subrole-gate":
            return list(phase.get("tasks") or [])
    return []


def _text(path: str) -> str:
    return (ROOT / path).read_text(encoding="utf-8")


def _literal_strings(node: ast.AST) -> list[str]:
    values = []
    for child in ast.walk(node):
        if isinstance(child, ast.Constant) and isinstance(child.value, str):
            values.append(child.value)
    return values


def _assignment_names(node: ast.AST) -> list[str]:
    names: list[str] = []
    targets: list[ast.AST] = []
    if isinstance(node, ast.Assign):
        targets = list(node.targets)
    elif isinstance(node, ast.AnnAssign):
        targets = [node.target]
    for target in targets:
        if isinstance(target, ast.Name):
            names.append(target.id)
    return names


def _operator_org_default_leaks() -> list[str]:
    leaks: list[str] = []
    for path in CONFIG_DEFAULT_SURFACES:
        tree = ast.parse(_text(path), filename=path)
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            names = _assignment_names(node)
            if not any(name.isupper() or "DEFAULT" in name.upper() for name in names):
                continue
            value = node.value
            for literal in _literal_strings(value):
                matches = sorted(set(OPERATOR_ORG_REPO_PATTERN.findall(literal)))
                if matches:
                    leaks.append(f"{path}:{getattr(node, 'lineno', '?')} {','.join(names)} -> {matches}")
    return leaks


class _WarningRecorder:
    def __init__(self) -> None:
        self.messages: list[str] = []

    def warning(self, message: str, *args: object) -> None:
        self.messages.append(message % args if args else message)


def main() -> int:
    old_gate_owners = os.environ.get("ORCH_GATE_OWNERS")
    try:
        os.environ.pop("ORCH_GATE_OWNERS", None)
        generic_tasks = _gate_tasks(apply_gate_template(_gate_plan()))
        generic_owners = {task["owner"] for task in generic_tasks}
        _check(
            "unset ORCH_GATE_OWNERS yields generic stage owners",
            generic_owners == {"scout", "code", "audit", "review", "approval"},
            generic_owners,
        )
        _check(
            "unset gate owners contain no private fleet owner names",
            not (generic_owners & FLEET_OWNER_NAMES),
            generic_owners & FLEET_OWNER_NAMES,
        )

        os.environ["ORCH_GATE_OWNERS"] = (
            "scout=scout-worker,code=code-worker,audit=audit-worker,"
            "review=review-worker,approval=approval-worker"
        )
        mapped_tasks = _gate_tasks(apply_gate_template(_gate_plan()))
        mapped_owners = {task["owner"] for task in mapped_tasks}
        _check(
            "ORCH_GATE_OWNERS maps stage owners from env",
            mapped_owners == {"scout-worker", "code-worker", "audit-worker", "review-worker", "approval-worker"},
            mapped_owners,
        )
    finally:
        if old_gate_owners is None:
            os.environ.pop("ORCH_GATE_OWNERS", None)
        else:
            os.environ["ORCH_GATE_OWNERS"] = old_gate_owners

    html = _text("ui/index.html")
    app_js = _text("ui/static/app.js")
    _check("chat HTML initial target is generic", "selected session" in html, html[:200])
    _check("chat HTML has no private supervisor literal", "conductor" not in html.lower(), "ui/index.html")
    _check("chat JS loads session list dynamically", 'fetchJson("/api/sessions")' in app_js, "ui/static/app.js")
    _check("chat JS defaults selection from first configured session", "SESSIONS[0] || null" in app_js, "ui/static/app.js")
    _check("chat JS has no private supervisor literal", "conductor" not in app_js.lower(), "ui/static/app.js")

    leaks = {}
    for path in SHIPPED_SURFACES:
        matches = sorted({m.group(0) for m in PRIVATE_NAME_PATTERN.finditer(_text(path))})
        if matches:
            leaks[path] = matches
    _check("shipped surfaces contain no private fleet identity literals", not leaks, leaks)

    old_completion_allowlist = os.environ.get("ORCH_COMPLETION_ALLOWED_REPOS")
    try:
        os.environ.pop("ORCH_COMPLETION_ALLOWED_REPOS", None)
        recorder = _WarningRecorder()
        _check(
            "unset ORCH_COMPLETION_ALLOWED_REPOS has no product default allowlist",
            allowed_completion_repos() == (),
            allowed_completion_repos(),
        )
        _check(
            "unset ORCH_COMPLETION_ALLOWED_REPOS emits loud UNVERIFIED startup warning",
            warn_if_completion_allowlist_unset(recorder) is True
            and any(COMPLETION_ALLOWLIST_UNSET_WARNING in message for message in recorder.messages),
            recorder.messages,
        )
    finally:
        if old_completion_allowlist is None:
            os.environ.pop("ORCH_COMPLETION_ALLOWED_REPOS", None)
        else:
            os.environ["ORCH_COMPLETION_ALLOWED_REPOS"] = old_completion_allowlist

    org_default_leaks = _operator_org_default_leaks()
    _check(
        "config/default surfaces contain no operator org repo defaults",
        not org_default_leaks,
        org_default_leaks,
    )

    # Durable recurrence guard (the "extend the scan" gatekeeper recommended on PR#101):
    # PERSONAL name + operator MACHINE profile must not appear ANYWHERE in the tracked
    # tree — including tests/ and migrations/, which are outside SHIPPED_SURFACES but
    # still ship in a public clone. This is narrower than PRIVATE_NAME_PATTERN on purpose:
    # it flags only personal/machine identity (jesse, ff-profile-mira), NOT generic fleet
    # ROLE names (conductor/weaver), which are acceptable test data.
    import subprocess
    try:
        grep = subprocess.run(
            ["git", "grep", "-nIE", "-i", r"\bjesse\b|ff-profile-mira"],
            cwd=str(ROOT), capture_output=True, text=True,
        )
        pm_leaks = []
        for line in grep.stdout.splitlines():
            if not line.strip():
                continue
            file_path = line.split(":", 1)[0]
            if file_path == "tests/fleet_identity_deumbilical_acceptance.py":
                continue  # this scanner names the pattern in its own code/comments
            if "/home/mira" in line:
                continue  # legit de-umbilical negative-test guards assert the absence of this path
            pm_leaks.append(line)
        _check("no personal/machine operator identity (jesse / ff-profile-mira) anywhere in the tree", not pm_leaks, pm_leaks)
    except Exception as exc:  # git unavailable — don't fail-open silently
        _check("personal/machine identity scan ran (git available)", False, str(exc))

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - fleet identity removed from gate defaults, chat target, and shipped examples.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
