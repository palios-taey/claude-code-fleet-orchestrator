#!/usr/bin/env python3
from __future__ import annotations

import ast
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


ROOT = Path(__file__).resolve().parents[1]
REGISTRY = ROOT / "docs" / "EXCEPTION_HANDLERS.md"
CRITICAL_FILES = (
    "fleet_orchestrator/orch_schema.py",
    "fleet_orchestrator/context_assembler.py",
    "fleet_orchestrator/cli_orch_watch.py",
    "fleet_orchestrator/dispatch.py",
    "fleet_orchestrator/worker_liveness.py",
    "fleet_orchestrator/inflight.py",
    "fleet_orchestrator/tasks_api.py",
    "fleet_orchestrator/handoff_validation.py",
    "fleet_orchestrator/plan_readiness.py",
    "fleet_orchestrator/out_of_band.py",
    "fleet_orchestrator/decision_receipt.py",
)
ALLOWED_CATEGORIES = {
    "intentional-fail-open",
    "intentional-fail-closed",
    "harmless-best-effort",
    "defect",
}
LOGGING_CALL_RE = re.compile(r"\b(?:LOGGER|LOG|logger|log|_LOG)\.(?:exception|warning|error)\s*\(")


@dataclass(frozen=True)
class Handler:
    file: str
    line: int
    function: str
    source: str

    @property
    def key(self) -> tuple[str, int, str]:
        return (self.file, self.line, self.function)


@dataclass(frozen=True)
class RegistryEntry:
    file: str
    line: int
    function: str
    category: str
    rationale: str
    remediation: str

    @property
    def key(self) -> tuple[str, int, str]:
        return (self.file, self.line, self.function)


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    result: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            result[child] = node
    return result


def _function_path(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str:
    names: list[str] = []
    parent = parents.get(node)
    while parent is not None:
        if isinstance(parent, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(parent.name)
        parent = parents.get(parent)
    return ".".join(reversed(names)) or "<module>"


def discover_handlers(root: Path, critical_files: Iterable[str] = CRITICAL_FILES) -> list[Handler]:
    handlers: list[Handler] = []
    for file in critical_files:
        path = root / file
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=file)
        parents = _parents(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.ExceptHandler):
                continue
            if not isinstance(node.type, ast.Name) or node.type.id != "Exception":
                continue
            segment = ast.get_source_segment(source, node) or ""
            handlers.append(
                Handler(
                    file=file,
                    line=int(node.lineno),
                    function=_function_path(node, parents),
                    source=segment,
                )
            )
    return sorted(handlers, key=lambda item: (item.file, item.line, item.function))


def parse_registry(path: Path) -> list[RegistryEntry]:
    entries: list[RegistryEntry] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line.startswith("| fleet_orchestrator/"):
            continue
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != 6:
            raise ValueError(f"registry row must have 6 cells: {raw_line}")
        file, line_no, function, category, rationale, remediation = cells
        try:
            parsed_line = int(line_no)
        except ValueError as exc:
            raise ValueError(f"registry line number is not an integer: {raw_line}") from exc
        entries.append(
            RegistryEntry(
                file=file,
                line=parsed_line,
                function=function,
                category=category,
                rationale=rationale,
                remediation=remediation,
            )
        )
    return entries


def check(
    root: Path = ROOT,
    *,
    critical_files: Iterable[str] = CRITICAL_FILES,
    registry_path: Path | None = None,
) -> list[str]:
    registry = registry_path or (root / "docs" / "EXCEPTION_HANDLERS.md")
    handlers = discover_handlers(root, critical_files)
    entries = parse_registry(registry)
    errors: list[str] = []

    handler_by_key = {handler.key: handler for handler in handlers}
    entry_by_key: dict[tuple[str, int, str], RegistryEntry] = {}
    for entry in entries:
        if entry.category not in ALLOWED_CATEGORIES:
            errors.append(f"{entry.file}:{entry.line} has invalid category {entry.category!r}")
        if entry.key in entry_by_key:
            errors.append(f"{entry.file}:{entry.line} duplicates registry entry for {entry.function}")
        entry_by_key[entry.key] = entry

    for handler in handlers:
        if handler.key not in entry_by_key:
            errors.append(f"{handler.file}:{handler.line} {handler.function} is unclassified")

    for entry in entries:
        handler = handler_by_key.get(entry.key)
        if handler is None:
            errors.append(f"{entry.file}:{entry.line} {entry.function} is classified but no matching handler exists")
            continue
        if entry.category == "defect" and not LOGGING_CALL_RE.search(handler.source):
            errors.append(f"{entry.file}:{entry.line} {entry.function} defect handler lacks observable logging")
        if not entry.rationale:
            errors.append(f"{entry.file}:{entry.line} {entry.function} missing rationale")
        if not entry.remediation:
            errors.append(f"{entry.file}:{entry.line} {entry.function} missing remediation note")

    return errors


def main() -> int:
    errors = check(ROOT)
    if errors:
        print("Exception-handler classification check FAILED:", file=sys.stderr)
        for error in errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print(f"exception classification: PASS ({len(discover_handlers(ROOT))} critical handlers)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
