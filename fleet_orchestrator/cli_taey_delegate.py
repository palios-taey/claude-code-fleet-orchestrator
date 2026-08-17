#!/usr/bin/env python3
"""Collect filesystem-derived artifact evidence for delegated work."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from fleet_orchestrator.easy_setup import atomic_write_text
from fleet_orchestrator.version import __version__


SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ArtifactCollectionError(RuntimeError):
    pass


def _artifact_from_disk(declared_path: str) -> dict[str, Any]:
    path = os.path.realpath(os.path.abspath(os.path.expanduser(declared_path)))
    exists = os.path.exists(path)
    if not exists:
        raise ArtifactCollectionError(f"artifact does not exist: {path}")

    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise ArtifactCollectionError(f"artifact size read failed for {path}: {exc}") from exc
    if size <= 0:
        raise ArtifactCollectionError(f"artifact is empty: {path}")

    digest = hashlib.sha256()
    bytes_read = 0
    try:
        with open(path, "rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
                bytes_read += len(chunk)
    except OSError as exc:
        raise ArtifactCollectionError(f"artifact read failed for {path}: {exc}") from exc
    if bytes_read != size:
        raise ArtifactCollectionError(
            f"artifact changed while hashing {path}: getsize={size} bytes_read={bytes_read}"
        )

    sha256 = digest.hexdigest()
    if not SHA256_PATTERN.fullmatch(sha256):
        raise ArtifactCollectionError(f"invalid sha256 for {path}: {sha256!r}")
    return {
        "path": path,
        "exists": exists,
        "bytes": size,
        "sha256": sha256,
    }


def collect_artifacts(declared_paths: Sequence[str]) -> list[dict[str, Any]]:
    for declared_path in declared_paths:
        _artifact_from_disk(declared_path)
    return [_artifact_from_disk(declared_path) for declared_path in declared_paths]


def cmd_collect(args: argparse.Namespace) -> int:
    artifacts = collect_artifacts(args.files)
    manifest = {
        "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "tool_version": __version__,
        "artifacts": artifacts,
    }
    output = Path(args.output).expanduser().resolve()
    atomic_write_text(output, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
    print(f"wrote {output} ({len(artifacts)} artifacts)")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="taey-delegate",
        description="Build filesystem-derived delegation artifacts",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    collect = subparsers.add_parser(
        "collect",
        help="Hash declared files and atomically write artifacts.json",
    )
    collect.add_argument("files", nargs="+", help="artifact file paths")
    collect.add_argument(
        "-o",
        "--output",
        default="artifacts.json",
        help="manifest path (default: ./artifacts.json)",
    )
    collect.set_defaults(handler=cmd_collect)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.handler(args)
    except (ArtifactCollectionError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
