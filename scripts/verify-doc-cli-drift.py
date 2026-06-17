#!/usr/bin/env python3
from __future__ import annotations

import argparse
import ast
import re
import shlex
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path


COMMAND_RE = re.compile(r"\b(taey-[a-z0-9-]+)\b")
OPTION_RE = re.compile(r"(?<![\w-])--[a-z][a-z0-9-]*")
SUBCOMMAND_RE = re.compile(r"\{([a-z0-9][a-z0-9_-]*(?:,[a-z0-9][a-z0-9_-]*)*)\}\s+\.\.\.")
INLINE_CODE_RE = re.compile(r"`([^`]*\btaey-[^`]*)`")
BACKTICK_CALL_RE = re.compile(r"`([A-Za-z_][A-Za-z0-9_]*)\(\)`")
BACKTICK_FILE_RE = re.compile(r"`([^`]+\.(?:py|md|sh|json|ya?ml|toml))`")
MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
REFERENCE_LINK_USE_RE = re.compile(r"(?<!!)\[[^\]]+\]\[([^\]]+)\]")
REFERENCE_LINK_DEF_RE = re.compile(r"^\s*\[([^\]]+)\]:\s*(\S+)")
HTML_HREF_RE = re.compile(r"""<a\s+[^>]*href=["']([^"']+)["']""", re.IGNORECASE)
ENV_RE = re.compile(r"\bORCH_[A-Z0-9_]+\b")
REPO_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_./-])"
    r"("
    r"(?:README|CHANGELOG|SETUP|SECURITY|SUPPORT|CLAUDE)\.md"
    r"|\.env\.example"
    r"|requirements\.txt"
    r"|setup\.py"
    r"|(?:docs|tests|scripts|fleet_orchestrator|ui|\.github)/[A-Za-z0-9_./:-]+"
    r"|[A-Za-z0-9_.-]+/[A-Za-z0-9_./-]+\.(?:py|md|sh|json|ya?ml|toml)"
    r")"
)
PY_MODULE_RE = re.compile(r"\bfleet_orchestrator(?:\.[A-Za-z_][A-Za-z0-9_]*)+(?::[A-Za-z_][A-Za-z0-9_]*)?\b")
MARKDOWN_TOKEN_BOUNDARY = set("|`)")
DEFAULT_PATH_CLIS = (
    "taey-stop-reason",
    "taey-task",
    "taey-plan",
    "taey-notify",
    "taey-dispatch",
    "taey-question",
    "taey-ack",
    "taey-handoff",
    "taey-trace",
)
DOC_CURRENCY_DEFAULT = True
DOC_CURRENCY_LOCAL_PREFIXES = (
    ".github/",
    "docs/",
    "fleet_orchestrator/",
    "scripts/",
    "tests/",
    "ui/",
)
DOC_CURRENCY_ROOT_FILES = {
    ".env.example",
    "CHANGELOG.md",
    "CLAUDE.md",
    "README.md",
    "SECURITY.md",
    "SETUP.md",
    "SUPPORT.md",
    "requirements.txt",
    "setup.py",
}
EXTERNAL_DELEGATED_PREFIXES = (
    "claude-code-fleet-notify/",
    "notifications/",
)
SAFE_EXTERNAL_CALL_NAMES = {
    "dict",
    "getattr",
    "len",
    "list",
    "open",
    "print",
    "set",
    "str",
}
SAFE_ILLUSTRATIVE_FILES = {
    "MEMORY.md",
}


@dataclass
class CommandHelp:
    path: tuple[str, ...]
    subcommands: set[str] = field(default_factory=set)
    options: set[str] = field(default_factory=set)


def _script_names(scripts_dir: Path) -> set[str]:
    if not scripts_dir.is_dir():
        return set()
    return {
        path.name
        for path in scripts_dir.iterdir()
        if path.is_file() and path.name.startswith("taey-")
    }


def _script_executables(root: Path) -> dict[str, str]:
    return {name: str(root / "scripts" / name) for name in _script_names(root / "scripts")}


def _path_executables(cli_names: list[str] | None) -> dict[str, str]:
    executables: dict[str, str] = {}
    for name in cli_names or list(DEFAULT_PATH_CLIS):
        found = shutil.which(name)
        if found:
            executables[name] = found
    return executables


def _help_text(root: Path, command_path: tuple[str, ...], executables: dict[str, str]) -> str:
    executable = executables.get(command_path[0])
    if not executable:
        raise RuntimeError(f"{command_path[0]} is not available")
    result = subprocess.run(
        [executable, *command_path[1:], "--help"],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"{' '.join(command_path)} --help failed with {result.returncode}: {result.stderr.strip()}"
        )
    return result.stdout


def _parse_help(command_path: tuple[str, ...], text: str) -> CommandHelp:
    subcommands: set[str] = set()
    for raw_group in SUBCOMMAND_RE.findall(text):
        subcommands.update(item.strip() for item in raw_group.split(",") if item.strip())
    return CommandHelp(
        path=command_path,
        subcommands=subcommands,
        options=set(OPTION_RE.findall(text)),
    )


def discover_cli_help(
    root: Path,
    max_depth: int = 4,
    *,
    include_scripts: bool = True,
    include_path: bool = False,
    path_cli_names: list[str] | None = None,
) -> dict[tuple[str, ...], CommandHelp]:
    discovered: dict[tuple[str, ...], CommandHelp] = {}
    setup_commands = _setup_declared_commands(root)
    executables = _script_executables(root) if include_scripts else {}
    if include_path:
        for name, executable in _path_executables(path_cli_names).items():
            executables.setdefault(name, executable)
    queue = [(name,) for name in sorted(executables)]
    while queue:
        command_path = queue.pop(0)
        if command_path in discovered:
            continue
        info = _parse_help(command_path, _help_text(root, command_path, executables))
        if len(command_path) == 1 and command_path[0] in setup_commands:
            info.options.add("--version")
        discovered[command_path] = info
        if len(command_path) >= max_depth:
            continue
        for subcommand in sorted(info.subcommands):
            queue.append((*command_path, subcommand))
    return discovered


def _markdown_files(root: Path, doc_paths: list[Path] | None = None) -> list[Path]:
    if doc_paths is not None:
        files: list[Path] = []
        for raw_path in doc_paths:
            path = raw_path if raw_path.is_absolute() else root / raw_path
            if path.is_file() and path.suffix.lower() == ".md":
                files.append(path)
            elif path.is_dir():
                files.extend(sorted(item for item in path.rglob("*.md") if item.is_file()))
        return files
    candidates = [root / "README.md", root / "CHANGELOG.md", root / "SETUP.md", root / "SECURITY.md", root / "SUPPORT.md"]
    candidates.extend(sorted((root / "docs").glob("*.md")))
    return [path for path in candidates if path.is_file()]


def _display_path(root: Path, path: Path) -> Path:
    try:
        return path.relative_to(root)
    except ValueError:
        return path


def _strip_markdown_tail(text: str) -> str:
    for idx, char in enumerate(text):
        if char in MARKDOWN_TOKEN_BOUNDARY:
            return text[:idx]
    return text


def _setup_declared_commands(root: Path) -> set[str]:
    setup_path = root / "setup.py"
    commands: set[str] = set()
    if not setup_path.is_file():
        return commands
    try:
        tree = ast.parse(setup_path.read_text(encoding="utf-8"))
    except SyntaxError:
        return commands
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not (isinstance(func, ast.Name) and func.id == "setup"):
            continue
        for keyword in node.keywords:
            if keyword.arg == "scripts" and isinstance(keyword.value, ast.List):
                for item in keyword.value.elts:
                    if isinstance(item, ast.Constant) and isinstance(item.value, str):
                        commands.add(Path(item.value).name)
            if keyword.arg == "entry_points" and isinstance(keyword.value, ast.Dict):
                for key, value in zip(keyword.value.keys, keyword.value.values):
                    if not (isinstance(key, ast.Constant) and key.value == "console_scripts"):
                        continue
                    if isinstance(value, ast.List):
                        for item in value.elts:
                            if isinstance(item, ast.Constant) and isinstance(item.value, str):
                                name, _, _target = item.value.partition("=")
                                if name.strip():
                                    commands.add(name.strip())
    return commands


def _tokens_from_command_text(raw: str) -> list[str]:
    raw = _strip_markdown_tail(raw).strip()
    raw = raw.replace("…", " ").replace("—", " ")
    try:
        tokens = shlex.split(raw)
    except ValueError:
        tokens = raw.split()
    return [token.strip("`.,;:") for token in tokens if token.strip("`.,;:")]


def documented_invocations(root: Path, doc_paths: list[Path] | None = None) -> list[tuple[Path, int, list[str], str]]:
    invocations: list[tuple[Path, int, list[str], str]] = []
    for path in _markdown_files(root, doc_paths):
        in_fence = False
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
            stripped = line.strip()
            if stripped.startswith("```"):
                in_fence = not in_fence
                continue
            if in_fence:
                match = COMMAND_RE.search(stripped)
                if match and not stripped[: match.start()].strip("$ "):
                    tokens = _tokens_from_command_text(stripped[match.start() :])
                    if tokens:
                        invocations.append((_display_path(root, path), lineno, tokens, stripped))
                continue
            for match in INLINE_CODE_RE.finditer(line):
                tokens = _tokens_from_command_text(match.group(1))
                if len(tokens) > 1 and tokens[0].startswith("taey-"):
                    invocations.append((_display_path(root, path), lineno, tokens, line.strip()))
            seen_commands = {item[0][0] for item in [(tokens,) for _path, _lineno, tokens, _raw in invocations if _path == _display_path(root, path) and _lineno == lineno and tokens]}
            for match in COMMAND_RE.finditer(line):
                command = match.group(1)
                if command in seen_commands:
                    continue
                tokens = _tokens_from_command_text(line[match.start():])
                if tokens:
                    invocations.append((_display_path(root, path), lineno, tokens, line.strip()))
    return invocations


def _is_placeholder(token: str) -> bool:
    return (
        token.startswith("<")
        or token.startswith("[")
        or token.startswith("{")
        or token.isupper()
        or "/" in token
        or "=" in token
        or token.startswith('"')
        or token.startswith("'")
    )


def validate_invocation(
    tokens: list[str],
    help_by_path: dict[tuple[str, ...], CommandHelp],
) -> list[str]:
    errors: list[str] = []
    command = tokens[0]
    if (command,) not in help_by_path:
        if command in DEFAULT_PATH_CLIS:
            return []
        return [f"unknown documented CLI `{command}`"]

    active_path = (command,)
    idx = 1
    while idx < len(tokens):
        token = tokens[idx]
        if not token or _is_placeholder(token):
            break
        if token.startswith("-"):
            break
        parent = help_by_path[active_path]
        if token in parent.subcommands:
            active_path = (*active_path, token)
            idx += 1
            continue
        if parent.subcommands:
            errors.append(f"`{' '.join(active_path)}` does not expose subcommand `{token}`")
            break
        if any(token in info.subcommands for info in help_by_path.values() if info.path[0] == command):
            errors.append(f"`{' '.join(active_path)}` does not expose subcommand `{token}`")
        break

    visible_options: set[str] = set()
    for depth in range(1, len(active_path) + 1):
        visible_options.update(help_by_path.get(active_path[:depth], CommandHelp(active_path[:depth])).options)
    for option in (token.split("=", 1)[0] for token in tokens if token.startswith("--")):
        if option not in visible_options:
            errors.append(f"`{' '.join(active_path)}` help does not expose option `{option}`")
    return errors


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _clean_doc_path(raw: str) -> str:
    value = raw.strip().strip("<>`\"'")
    value = value.split("#", 1)[0]
    value = value.rstrip(").,;:")
    value = re.sub(r":\d+(?:-\d+)?$", "", value)
    return value


def _path_and_symbol(raw: str) -> tuple[str, str | None]:
    value = _clean_doc_path(raw)
    match = re.match(r"^(.+\.py):([A-Za-z_][A-Za-z0-9_]*)$", value)
    if match:
        return match.group(1), match.group(2)
    return value, None


def _is_external_or_placeholder(value: str) -> bool:
    if not value:
        return True
    lowered = value.lower()
    return (
        lowered.startswith(("http://", "https://", "mailto:", "file:"))
        or lowered.startswith("#")
        or "<" in value
        or ">" in value
        or "{" in value
        or value.startswith("~")
        or value.startswith("/path/")
        or value.startswith("/abs/")
        or value.startswith("OWNER/")
        or value.startswith("REPO/")
        or any(value.startswith(prefix) for prefix in EXTERNAL_DELEGATED_PREFIXES)
    )


def _resolve_doc_reference(root: Path, doc_file: Path, raw: str) -> Path | None:
    value, _symbol = _path_and_symbol(raw)
    if _is_external_or_placeholder(value):
        return None
    candidate = Path(value)
    if candidate.is_absolute():
        return candidate
    if value.startswith("../") or value.startswith("./"):
        return (doc_file.parent / candidate).resolve(strict=False)
    return (root / candidate).resolve(strict=False)


def _resolve_markdown_link(doc_file: Path, raw: str) -> Path | None:
    value = _clean_doc_path(raw)
    if _is_external_or_placeholder(value):
        return None
    candidate = Path(value)
    if candidate.is_absolute():
        return candidate
    return (doc_file.parent / candidate).resolve(strict=False)


def _reference_definitions(root: Path, doc_paths: list[Path] | None = None) -> dict[Path, dict[str, str]]:
    definitions: dict[Path, dict[str, str]] = {}
    for path in _markdown_files(root, doc_paths):
        refs: dict[str, str] = {}
        for line in _read_text(path).splitlines():
            match = REFERENCE_LINK_DEF_RE.match(line)
            if match:
                refs[match.group(1).strip().casefold()] = match.group(2).strip()
        definitions[path] = refs
    return definitions


def _is_repo_local_reference(raw: str) -> bool:
    value, _symbol = _path_and_symbol(raw)
    if _is_external_or_placeholder(value):
        return False
    return value in DOC_CURRENCY_ROOT_FILES or any(value.startswith(prefix) for prefix in DOC_CURRENCY_LOCAL_PREFIXES)


def _repo_file_reference_exists(root: Path, actual_doc: Path, raw: str) -> bool:
    value, _symbol = _path_and_symbol(raw)
    if _is_external_or_placeholder(value):
        return True
    if value in SAFE_ILLUSTRATIVE_FILES:
        return True
    candidate = Path(value)
    if candidate.is_absolute():
        return True
    if "/" in value:
        if value.startswith("../") or value.startswith("./"):
            return (actual_doc.parent / candidate).resolve(strict=False).exists()
        return (root / candidate).resolve(strict=False).exists()
    return any(path.name == value for path in root.rglob(value))


def _iter_doc_lines(root: Path, doc_paths: list[Path] | None = None) -> list[tuple[Path, Path, int, str]]:
    lines: list[tuple[Path, Path, int, str]] = []
    for display, actual in ((_display_path(root, path), path) for path in _markdown_files(root, doc_paths)):
        for lineno, line in enumerate(_read_text(actual).splitlines(), start=1):
            lines.append((display, actual, lineno, line))
    return lines


def _source_text_for_currency(root: Path) -> str:
    parts: list[str] = []
    skip_dirs = {".git", ".venv", "__pycache__", "build"}
    skip_suffixes = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".pyc"}
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(part in skip_dirs for part in rel.parts):
            continue
        if path.suffix.lower() in skip_suffixes:
            continue
        if rel.parts and rel.parts[0] == "docs":
            continue
        if rel.name in {"README.md", "CLAUDE.md", "SETUP.md", "SECURITY.md", "SUPPORT.md", "CHANGELOG.md"}:
            continue
        try:
            parts.append(_read_text(path))
        except UnicodeDecodeError:
            continue
    return "\n".join(parts)


def _top_level_names(module_file: Path) -> set[str]:
    try:
        tree = ast.parse(_read_text(module_file))
    except SyntaxError:
        return set()
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".", 1)[0])
    return names


def _repo_function_symbols(root: Path) -> set[str]:
    names: set[str] = set()
    for path in root.rglob("*.py"):
        rel = path.relative_to(root)
        if any(part in {".git", ".venv", "__pycache__", "build"} for part in rel.parts):
            continue
        try:
            tree = ast.parse(_read_text(path))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                names.add(node.name)
    return names


def _module_file_for(root: Path, module: str) -> Path | None:
    rel = Path(*module.split("."))
    package = root / rel / "__init__.py"
    if package.is_file():
        return package
    module_file = root / rel.with_suffix(".py")
    if module_file.is_file():
        return module_file
    return None


def _module_reference_exists(root: Path, reference: str) -> bool:
    module_ref, _, colon_attr = reference.partition(":")
    parts = module_ref.split(".")
    for idx in range(len(parts), 0, -1):
        module = ".".join(parts[:idx])
        module_file = _module_file_for(root, module)
        if module_file is None:
            continue
        attrs = [part for part in parts[idx:] if part]
        if colon_attr:
            attrs.append(colon_attr)
        if not attrs:
            return True
        names = _top_level_names(module_file)
        return attrs[0] in names
    return False


def _file_symbol_exists(path: Path, symbol: str | None) -> bool:
    if symbol is None:
        return True
    if not path.is_file() or path.suffix != ".py":
        return False
    return symbol in _top_level_names(path)


def check_doc_currency(root: Path, doc_paths: list[Path] | None = None) -> list[str]:
    errors: list[str] = []
    source_text = _source_text_for_currency(root)
    function_symbols = _repo_function_symbols(root)
    reference_defs = _reference_definitions(root, doc_paths)
    setup_commands = _setup_declared_commands(root)
    script_commands = {path.name for path in (root / "scripts").iterdir() if path.is_file()} if (root / "scripts").is_dir() else set()
    repo_commands = setup_commands | script_commands

    for display, actual_doc, lineno, line in _iter_doc_lines(root, doc_paths):
        if "[ref:" not in line:
            for match in REPO_PATH_RE.finditer(line):
                raw = match.group(1)
                if not _is_repo_local_reference(raw):
                    continue
                resolved = _resolve_doc_reference(root, actual_doc, raw)
                if resolved is not None and not resolved.exists():
                    errors.append(f"{display}:{lineno}: documented repo path does not exist: `{raw}`")
                    continue
                _path_value, symbol = _path_and_symbol(raw)
                if resolved is not None and not _file_symbol_exists(resolved, symbol):
                    errors.append(f"{display}:{lineno}: documented Python file symbol does not exist: `{raw}`")

        for match in MARKDOWN_LINK_RE.finditer(line):
            raw = match.group(1)
            if _is_external_or_placeholder(_clean_doc_path(raw)):
                continue
            resolved = _resolve_markdown_link(actual_doc, raw)
            if resolved is not None and not resolved.exists():
                errors.append(f"{display}:{lineno}: markdown link target does not exist: `{raw}`")

        ref_def = REFERENCE_LINK_DEF_RE.match(line)
        if ref_def:
            raw = ref_def.group(2)
            if not _is_external_or_placeholder(_clean_doc_path(raw)):
                resolved = _resolve_markdown_link(actual_doc, raw)
                if resolved is not None and not resolved.exists():
                    errors.append(f"{display}:{lineno}: reference-style link target does not exist: `{raw}`")
        for ref_match in REFERENCE_LINK_USE_RE.finditer(line):
            label = ref_match.group(1).strip().casefold()
            if label not in reference_defs.get(actual_doc, {}):
                errors.append(f"{display}:{lineno}: reference-style link label is undefined: `{ref_match.group(1)}`")
        for href_match in HTML_HREF_RE.finditer(line):
            raw = href_match.group(1)
            if _is_external_or_placeholder(_clean_doc_path(raw)):
                continue
            resolved = _resolve_markdown_link(actual_doc, raw)
            if resolved is not None and not resolved.exists():
                errors.append(f"{display}:{lineno}: HTML link target does not exist: `{raw}`")

        for env in sorted(set(ENV_RE.findall(line))):
            if env not in source_text:
                errors.append(f"{display}:{lineno}: documented env var is not referenced by repo code/config: `{env}`")

        tokens = _tokens_from_command_text(line)
        if tokens:
            command = Path(tokens[0]).name if tokens[0].startswith("scripts/") else tokens[0]
            if command in repo_commands:
                if command in script_commands and not (root / "scripts" / command).is_file():
                    errors.append(f"{display}:{lineno}: documented script command is missing: `{tokens[0]}`")
            if command in setup_commands and command not in repo_commands:
                errors.append(f"{display}:{lineno}: documented setup entrypoint is missing: `{command}`")

        for raw in sorted(set(BACKTICK_FILE_RE.findall(line))):
            if _is_repo_local_reference(raw):
                continue
            if not _repo_file_reference_exists(root, actual_doc, raw):
                errors.append(f"{display}:{lineno}: documented repo-shaped file does not exist: `{raw}`")

        for ref in PY_MODULE_RE.findall(line):
            if ref.endswith(".py"):
                continue
            if not _module_reference_exists(root, ref):
                errors.append(f"{display}:{lineno}: documented Python entrypoint does not exist: `{ref}`")

        for call_name in sorted(set(BACKTICK_CALL_RE.findall(line))):
            if call_name in SAFE_EXTERNAL_CALL_NAMES:
                continue
            if call_name not in function_symbols:
                errors.append(f"{display}:{lineno}: documented function call does not exist: `{call_name}()`")
    return errors


def check_repo(
    root: Path,
    *,
    doc_paths: list[Path] | None = None,
    include_scripts: bool = True,
    include_path: bool = False,
    path_cli_names: list[str] | None = None,
) -> list[str]:
    help_by_path = discover_cli_help(
        root,
        include_scripts=include_scripts,
        include_path=include_path,
        path_cli_names=path_cli_names,
    )
    errors: list[str] = []
    for path, lineno, tokens, raw_line in documented_invocations(root, doc_paths):
        for error in validate_invocation(tokens, help_by_path):
            errors.append(f"{path}:{lineno}: {error}: {raw_line}")
    if DOC_CURRENCY_DEFAULT:
        errors.extend(check_doc_currency(root, doc_paths))
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify documented CLI and repo-reference drift")
    parser.add_argument("--root", default=".", help="Repository root")
    parser.add_argument("--doc-root", action="append", default=[], help="Markdown file or directory to scan recursively; repeatable")
    parser.add_argument("--no-scripts", action="store_true", help="Do not discover taey-* CLIs from <root>/scripts")
    parser.add_argument("--include-path", action="store_true", help="Also discover selected taey-* CLIs from PATH")
    parser.add_argument("--path-cli", action="append", default=[], help="taey-* CLI name to discover from PATH; repeatable")
    parser.add_argument("--no-doc-currency", action="store_true", help="Only validate taey-* CLI help drift")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    doc_paths = [Path(item) for item in args.doc_root] if args.doc_root else None
    global DOC_CURRENCY_DEFAULT
    DOC_CURRENCY_DEFAULT = not bool(args.no_doc_currency)
    errors = check_repo(
        root,
        doc_paths=doc_paths,
        include_scripts=not bool(args.no_scripts),
        include_path=bool(args.include_path),
        path_cli_names=args.path_cli or None,
    )
    if errors:
        print("documented doc drift detected:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print("documented CLI and repo references match live code")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
