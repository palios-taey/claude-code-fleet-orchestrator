#!/usr/bin/env python3
"""Collect filesystem-derived artifact evidence for delegated work."""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import stat
import sys
import tempfile
from contextlib import ExitStack
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Sequence

from fleet_orchestrator.version import __version__


SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class ArtifactCollectionError(RuntimeError):
    pass


Fingerprint = tuple[int, int, int, int, int]


@dataclass
class OpenArtifact:
    path: str
    handle: BinaryIO
    record: dict[str, Any]
    fingerprint: Fingerprint


def _resolved_path(declared_path: str) -> str:
    return os.path.realpath(os.path.abspath(os.path.expanduser(declared_path)))


def _fingerprint(value: os.stat_result) -> Fingerprint:
    return (
        value.st_dev,
        value.st_ino,
        value.st_size,
        value.st_mtime_ns,
        value.st_ctime_ns,
    )


def _artifact_from_open_file(
    path: str, handle: BinaryIO
) -> tuple[dict[str, Any], Fingerprint]:
    exists = os.path.exists(path)
    if not exists:
        raise ArtifactCollectionError(f"artifact does not exist: {path}")

    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise ArtifactCollectionError(f"artifact size read failed for {path}: {exc}") from exc
    if size <= 0:
        raise ArtifactCollectionError(f"artifact is empty: {path}")

    descriptor_before = os.fstat(handle.fileno())
    path_before = os.stat(path)
    if not stat.S_ISREG(descriptor_before.st_mode):
        raise ArtifactCollectionError(f"artifact is not a regular file: {path}")
    fingerprint_before = _fingerprint(descriptor_before)
    if fingerprint_before != _fingerprint(path_before) or descriptor_before.st_size != size:
        raise ArtifactCollectionError(f"artifact changed before hashing: {path}")

    digest = hashlib.sha256()
    bytes_read = 0
    try:
        handle.seek(0)
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            bytes_read += len(chunk)
    except OSError as exc:
        raise ArtifactCollectionError(f"artifact read failed for {path}: {exc}") from exc

    descriptor_after = os.fstat(handle.fileno())
    path_after = os.stat(path)
    fingerprint_after = _fingerprint(descriptor_after)
    if (
        bytes_read != size
        or fingerprint_after != fingerprint_before
        or fingerprint_after != _fingerprint(path_after)
    ):
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
    }, fingerprint_after


def _assert_output_is_distinct(output: Path, artifact_paths: Sequence[str]) -> None:
    output_path = str(output)
    output_exists = os.path.exists(output_path)
    for artifact_path in artifact_paths:
        if output_path == artifact_path:
            raise ArtifactCollectionError(f"manifest output aliases artifact: {artifact_path}")
        if (
            output_exists
            and os.path.exists(artifact_path)
            and os.path.samefile(output_path, artifact_path)
        ):
            raise ArtifactCollectionError(f"manifest output aliases artifact: {artifact_path}")


def _open_artifact(path: str, stack: ExitStack) -> BinaryIO:
    if not os.path.exists(path):
        raise ArtifactCollectionError(f"artifact does not exist: {path}")
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise ArtifactCollectionError(f"artifact size read failed for {path}: {exc}") from exc
    if size <= 0:
        raise ArtifactCollectionError(f"artifact is empty: {path}")
    try:
        handle = stack.enter_context(open(path, "rb"))
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError as exc:
        raise ArtifactCollectionError(f"artifact open or lock failed for {path}: {exc}") from exc
    return handle


def collect_artifacts(
    declared_paths: Sequence[str], output: Path, stack: ExitStack
) -> list[OpenArtifact]:
    artifact_paths = [_resolved_path(declared_path) for declared_path in declared_paths]
    _assert_output_is_distinct(output, artifact_paths)
    opened: list[OpenArtifact] = []
    for path in artifact_paths:
        handle = _open_artifact(path, stack)
        record, fingerprint = _artifact_from_open_file(path, handle)
        opened.append(OpenArtifact(path, handle, record, fingerprint))
    return opened


def _assert_artifacts_stable(opened: Sequence[OpenArtifact]) -> None:
    for artifact in opened:
        exists = os.path.exists(artifact.path)
        if not exists:
            raise ArtifactCollectionError(
                f"artifact disappeared before manifest write: {artifact.path}"
            )
        size = os.path.getsize(artifact.path)
        descriptor_now = os.fstat(artifact.handle.fileno())
        path_now = os.stat(artifact.path)
        if (
            size != artifact.record["bytes"]
            or _fingerprint(descriptor_now) != artifact.fingerprint
            or _fingerprint(path_now) != artifact.fingerprint
        ):
            raise ArtifactCollectionError(
                f"artifact changed before manifest write: {artifact.path}"
            )


def _write_manifest_transaction(output: Path, opened: Sequence[OpenArtifact]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(
        "w",
        delete=False,
        dir=str(output.parent),
        prefix=f".{output.name}.",
        encoding="utf-8",
    )
    temp_path = Path(handle.name)
    try:
        artifacts: list[dict[str, Any]] = []
        for artifact in opened:
            record, fingerprint = _artifact_from_open_file(artifact.path, artifact.handle)
            if record != artifact.record:
                raise ArtifactCollectionError(
                    f"artifact changed during verification: {artifact.path}"
                )
            artifact.record = record
            artifact.fingerprint = fingerprint
            artifacts.append(record)

        manifest = {
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "tool_version": __version__,
            "artifacts": artifacts,
        }
        handle.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()

        # Advisory locks cannot bind writers that ignore flock. A second full read plus
        # this final descriptor/path sweep closes detectable drift; strict simultaneity
        # across arbitrary files requires a filesystem snapshot.
        _assert_artifacts_stable(opened)
        _assert_output_is_distinct(output, [artifact.path for artifact in opened])
        os.replace(temp_path, output)
    except BaseException:
        handle.close()
        temp_path.unlink(missing_ok=True)
        raise


def cmd_collect(args: argparse.Namespace) -> int:
    output = Path(args.output).expanduser().resolve()
    with ExitStack() as stack:
        opened = collect_artifacts(args.files, output, stack)
        _write_manifest_transaction(output, opened)
    print(f"wrote {output} ({len(opened)} artifacts)")
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
