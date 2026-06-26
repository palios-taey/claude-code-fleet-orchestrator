#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import re
import sys
from pathlib import Path


ENV_PREFIXES = (
    "ORCH_",
    "CF_",
    "ACCOUNTABILITY_",
    "NOTIFY_",
    "TAEY_",
    "REDIS_",
    "DBUS_",
    "XDG_",
    "CLAUDE_SETTINGS_",
    "EASY_SETUP_",
    "REF_ACCEPTANCE_",
    "PROBE_",
)
ENV_EXACT = {"DISPLAY", "PATH"}
DOC_ENV_RE = re.compile(r"`([A-Z][A-Z0-9_]*(?:_[A-Z0-9]+)*)`")
HELPER_ENV_READERS = {
    "_dotenv_lookup",
    "_bool_env",
    "_int_env",
    "_optional_env",
    "_require_env",
    "_scoped_env_value",
    "_csv_env_values",
    "default_on_feature_enabled",
}
DOCUMENTED_ONLY = {
    "EASY_SETUP_ACCEPTANCE_INJECT_FAIL",
    "ORCH_TEST_NAMESPACE",
    "PATH",
    "PROBE_CHECK_MODE",
    "PROBE_HEAD_SHA",
    "REF_ACCEPTANCE_INJECT_FAIL",
}
REQUIRED_CONTRACT = "(required)"


def _is_env_name(value: str) -> bool:
    return value in ENV_EXACT or any(value.startswith(prefix) for prefix in ENV_PREFIXES)


def _string_value(node: ast.AST) -> str | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value
    return None


def _literal_text(node: ast.AST) -> str | None:
    if not isinstance(node, ast.Constant):
        return None
    if node.value is None:
        return "unset"
    if isinstance(node.value, (str, int, float, bool)):
        return str(node.value)
    return None


def _is_os_environ(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "environ"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _is_os_getenv(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Attribute)
        and node.attr == "getenv"
        and isinstance(node.value, ast.Name)
        and node.value.id == "os"
    )


def _env_reads_in_tree(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            if _is_os_getenv(node.func):
                name = _string_value(node.args[0]) if node.args else None
                if name and _is_env_name(name):
                    names.add(name)
            if isinstance(node.func, ast.Attribute) and node.func.attr in {"get", "pop", "setdefault"} and _is_os_environ(node.func.value):
                name = _string_value(node.args[0]) if node.args else None
                if name and _is_env_name(name):
                    names.add(name)
            if isinstance(node.func, ast.Name) and node.func.id in HELPER_ENV_READERS:
                for item in node.args:
                    name = _string_value(item)
                    if name and _is_env_name(name):
                        names.add(name)
                for keyword in node.keywords:
                    if keyword.arg != "aliases":
                        continue
                    values = keyword.value.elts if isinstance(keyword.value, (ast.Tuple, ast.List, ast.Set)) else []
                    for item in values:
                        name = _string_value(item)
                        if name and _is_env_name(name):
                            names.add(name)
        elif isinstance(node, ast.Subscript) and _is_os_environ(node.value) and isinstance(node.ctx, ast.Load):
            name = _string_value(node.slice)
            if name and _is_env_name(name):
                names.add(name)
        elif isinstance(node, ast.Compare):
            left = _string_value(node.left)
            if not left or not _is_env_name(left):
                continue
            if any(isinstance(op, (ast.In, ast.NotIn)) for op in node.ops) and any(_is_os_environ(comp) for comp in node.comparators):
                names.add(left)
    return names


def product_python_paths(root: Path) -> list[Path]:
    paths = list((root / "fleet_orchestrator").glob("**/*.py"))
    scripts_dir = root / "scripts"
    if scripts_dir.is_dir():
        paths.extend(path for path in scripts_dir.iterdir() if path.is_file())
    return sorted(paths)


def code_env_reads(root: Path) -> set[str]:
    names: set[str] = set()
    for path in product_python_paths(root):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            continue
        names.update(_env_reads_in_tree(tree))
    return names


def documented_env_names(root: Path) -> set[str]:
    path = root / "docs" / "CONFIGURATION.md"
    text = path.read_text(encoding="utf-8")
    return {name for name in DOC_ENV_RE.findall(text) if _is_env_name(name)}


def _split_markdown_row(line: str) -> list[str]:
    return [cell.strip() for cell in line.strip().strip("|").split("|")]


def _split_default_cell(default_cell: str, expected: int) -> list[str]:
    parts = [part.strip() for part in re.split(r"\s+/\s+", default_cell)]
    if len(parts) == expected:
        return parts
    return [default_cell] * expected


def documented_env_defaults(root: Path) -> dict[str, str]:
    path = root / "docs" / "CONFIGURATION.md"
    text = path.read_text(encoding="utf-8")
    defaults: dict[str, str] = {}
    for line in text.splitlines():
        if not line.startswith("|"):
            continue
        cells = _split_markdown_row(line)
        if len(cells) < 3 or cells[0].lower() == "flag" or set(cells[1]) <= {"-", ":"}:
            continue
        names = [name for name in DOC_ENV_RE.findall(cells[0]) if _is_env_name(name)]
        if not names:
            continue
        for name, default in zip(names, _split_default_cell(cells[1], len(names))):
            defaults[name] = default
    return defaults


def code_env_default_contracts(root: Path) -> dict[str, str]:
    path = root / "fleet_orchestrator" / "config.py"
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (FileNotFoundError, SyntaxError, UnicodeDecodeError):
        return {}

    contracts: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
            name = _string_value(node.args[0]) if node.args else None
            if name and _is_env_name(name):
                if node.func.id == "_require_env":
                    contracts[name] = REQUIRED_CONTRACT
                elif node.func.id == "_int_env":
                    default = _literal_text(node.args[1]) if len(node.args) > 1 else None
                    for keyword in node.keywords:
                        if keyword.arg == "default":
                            default = _literal_text(keyword.value)
                    contracts[name] = default if default is not None else REQUIRED_CONTRACT
                elif node.func.id == "_optional_env":
                    default = _literal_text(node.args[1]) if len(node.args) > 1 else "unset"
                    for keyword in node.keywords:
                        if keyword.arg == "default":
                            default = _literal_text(keyword.value)
                    if default is not None:
                        contracts.setdefault(name, default)
        if not isinstance(node, ast.Assign):
            continue
        target_names = {target.id for target in node.targets if isinstance(target, ast.Name)}
        if "REQUIRED_ENV" in target_names:
            values = node.value.elts if isinstance(node.value, (ast.Tuple, ast.List, ast.Set)) else []
            for item in values:
                name = _string_value(item)
                if name and _is_env_name(name):
                    contracts[name] = REQUIRED_CONTRACT
        if "OPTIONAL_ENV" in target_names:
            values = node.value.elts if isinstance(node.value, (ast.Tuple, ast.List, ast.Set)) else []
            for item in values:
                if not isinstance(item, (ast.Tuple, ast.List)) or len(item.elts) < 2:
                    continue
                name = _string_value(item.elts[0])
                default = _literal_text(item.elts[1])
                if name and _is_env_name(name) and default is not None:
                    contracts[name] = default
    return contracts


def _doc_default_is_required(default: str) -> bool:
    return "required" in default.lower()


def default_contract_errors(root: Path) -> list[str]:
    docs = documented_env_defaults(root)
    contracts = code_env_default_contracts(root)
    errors: list[str] = []
    for name, code_default in sorted(contracts.items()):
        doc_default = docs.get(name)
        if doc_default is None:
            continue
        if code_default == REQUIRED_CONTRACT and not _doc_default_is_required(doc_default):
            errors.append(
                f"CONFIGURATION.md documents `{name}` default as `{doc_default}`, but code requires it"
            )
        elif code_default != REQUIRED_CONTRACT and _doc_default_is_required(doc_default):
            errors.append(
                f"CONFIGURATION.md documents `{name}` as required, but code default contract is `{code_default}`"
            )
    return errors


def check_repo(root: Path) -> list[str]:
    code = code_env_reads(root)
    docs = documented_env_names(root)
    errors: list[str] = []
    for name in sorted(code - docs):
        errors.append(f"code reads undocumented env flag: `{name}`")
    for name in sorted(docs - code - DOCUMENTED_ONLY):
        errors.append(f"CONFIGURATION.md documents env flag not read by product code: `{name}`")
    errors.extend(default_contract_errors(root))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify CONFIGURATION.md env flags match product env reads")
    parser.add_argument("--root", default=".", help="Repository root")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    errors = check_repo(root)
    if errors:
        print("doc flag coherence failed:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print("CONFIGURATION.md env flags and required defaults match product code")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
