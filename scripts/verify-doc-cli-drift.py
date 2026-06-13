#!/usr/bin/env python3
from __future__ import annotations

import argparse
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
    return errors


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify documented taey-* CLI invocations against live --help output")
    parser.add_argument("--root", default=".", help="Repository root")
    parser.add_argument("--doc-root", action="append", default=[], help="Markdown file or directory to scan recursively; repeatable")
    parser.add_argument("--no-scripts", action="store_true", help="Do not discover taey-* CLIs from <root>/scripts")
    parser.add_argument("--include-path", action="store_true", help="Also discover selected taey-* CLIs from PATH")
    parser.add_argument("--path-cli", action="append", default=[], help="taey-* CLI name to discover from PATH; repeatable")
    args = parser.parse_args()
    root = Path(args.root).resolve()
    doc_paths = [Path(item) for item in args.doc_root] if args.doc_root else None
    errors = check_repo(
        root,
        doc_paths=doc_paths,
        include_scripts=not bool(args.no_scripts),
        include_path=bool(args.include_path),
        path_cli_names=args.path_cli or None,
    )
    if errors:
        print("documented CLI drift detected:", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
        return 1
    print("documented taey-* CLI invocations match live help")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
