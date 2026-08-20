#!/usr/bin/env python3
from __future__ import annotations

import ast
import re
import sys
from collections import defaultdict
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
    column: int
    function: str
    exception_type: str
    ordinal: int
    source: str

    @property
    def key(self) -> tuple[str, str, str, int]:
        return (self.file, self.function, self.exception_type, self.ordinal)

    @property
    def label(self) -> str:
        return f"{self.file}:{self.line} {self.function} except {self.exception_type}#{self.ordinal}"


@dataclass(frozen=True)
class RegistryEntry:
    file: str
    function: str
    exception_type: str
    ordinal: int
    line_hint: int
    category: str
    rationale: str
    remediation: str

    @property
    def key(self) -> tuple[str, str, str, int]:
        return (self.file, self.function, self.exception_type, self.ordinal)

    @property
    def label(self) -> str:
        return f"{self.file}:{self.line_hint} {self.function} except {self.exception_type}#{self.ordinal}"


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


def _exception_type(node: ast.AST | None) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return ast.unparse(node)
    if isinstance(node, ast.Tuple):
        return ", ".join(_exception_type(item) for item in node.elts)
    if node is None:
        return "<bare>"
    return ast.unparse(node)


def discover_handlers(root: Path, critical_files: Iterable[str] = CRITICAL_FILES) -> list[Handler]:
    raw_handlers: list[tuple[str, int, int, str, str, str]] = []
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
            raw_handlers.append(
                (
                    file,
                    int(node.lineno),
                    int(node.col_offset),
                    _function_path(node, parents),
                    _exception_type(node.type),
                    segment,
                )
            )
    ordinals: defaultdict[tuple[str, str, str], int] = defaultdict(int)
    handlers: list[Handler] = []
    for file, line, column, function, exception_type, segment in sorted(
        raw_handlers,
        key=lambda item: (item[0], item[3], item[4], item[1], item[2]),
    ):
        group_key = (file, function, exception_type)
        ordinals[group_key] += 1
        handlers.append(
            Handler(
                file=file,
                line=line,
                column=column,
                function=function,
                exception_type=exception_type,
                ordinal=ordinals[group_key],
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
        if len(cells) != 8:
            raise ValueError(f"registry row must have 8 cells: {raw_line}")
        file, function, exception_type, ordinal, line_hint, category, rationale, remediation = cells
        try:
            parsed_ordinal = int(ordinal)
        except ValueError as exc:
            raise ValueError(f"registry ordinal is not an integer: {raw_line}") from exc
        try:
            parsed_line_hint = int(line_hint)
        except ValueError as exc:
            raise ValueError(f"registry line hint is not an integer: {raw_line}") from exc
        entries.append(
            RegistryEntry(
                file=file,
                function=function,
                exception_type=exception_type,
                ordinal=parsed_ordinal,
                line_hint=parsed_line_hint,
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
    entry_by_key: dict[tuple[str, str, str, int], RegistryEntry] = {}
    for entry in entries:
        if entry.category not in ALLOWED_CATEGORIES:
            errors.append(f"{entry.label} has invalid category {entry.category!r}")
        if entry.key in entry_by_key:
            errors.append(f"{entry.label} duplicates registry entry")
        entry_by_key[entry.key] = entry

    for handler in handlers:
        if handler.key not in entry_by_key:
            errors.append(f"{handler.label} is unclassified")

    for entry in entries:
        handler = handler_by_key.get(entry.key)
        if handler is None:
            errors.append(f"{entry.label} is classified but no matching handler exists")
            continue
        if entry.line_hint != handler.line:
            errors.append(
                f"{entry.label} line hint does not match AST handler line {handler.line}"
            )
        if entry.category == "defect" and not LOGGING_CALL_RE.search(handler.source):
            errors.append(f"{entry.label} defect handler lacks observable logging")
        if not entry.rationale:
            errors.append(f"{entry.label} missing rationale")
        if not entry.remediation:
            errors.append(f"{entry.label} missing remediation note")

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
