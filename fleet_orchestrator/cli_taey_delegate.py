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
MISSING_NEXT_STEP = (
    "Create the missing artifact at the reported path or pass the correct artifact path, "
    "then rerun `taey-delegate collect`."
)
READ_NEXT_STEP = (
    "Grant the current user read access, release any exclusive advisory lock, and rerun "
    "`taey-delegate collect`."
)
EMPTY_NEXT_STEP = (
    "Write the intended artifact bytes to the reported file, then rerun "
    "`taey-delegate collect`; zero-byte artifacts are not certifiable."
)
REGULAR_FILE_NEXT_STEP = (
    "Snapshot the source into a non-empty regular file and pass that file to "
    "`taey-delegate collect`."
)
STABLE_FILE_NEXT_STEP = (
    "Stop the process mutating the artifact or snapshot it to a stable regular file, then "
    "rerun `taey-delegate collect`."
)
OUTPUT_ALIAS_NEXT_STEP = (
    "Choose an --output path that is not any declared artifact path, then rerun "
    "`taey-delegate collect`."
)
INTERNAL_NEXT_STEP = (
    "Do not use a manifest from this run; rerun `taey-delegate collect` and report an "
    "internal collector defect if this error repeats."
)
OUTPUT_FILESYSTEM_NEXT_STEP = (
    "Choose a writable --output directory on one filesystem, then rerun "
    "`taey-delegate collect`."
)
OS_ERROR_NEXT_STEP = (
    "Repair the reported filesystem path or permissions, ensure the output parent is "
    "writable, then rerun `taey-delegate collect`."
)


class ArtifactCollectionError(RuntimeError):
    def __init__(self, message: str, next_step: str):
        super().__init__(message)
        self.next_step = next_step


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
        raise ArtifactCollectionError(f"artifact does not exist: {path}", MISSING_NEXT_STEP)

    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise ArtifactCollectionError(
            f"artifact size read failed for {path}: {exc}", READ_NEXT_STEP
        ) from exc
    if size <= 0:
        raise ArtifactCollectionError(f"artifact is empty: {path}", EMPTY_NEXT_STEP)

    descriptor_before = os.fstat(handle.fileno())
    path_before = os.stat(path)
    if not stat.S_ISREG(descriptor_before.st_mode):
        raise ArtifactCollectionError(
            f"artifact is not a regular file: {path}", REGULAR_FILE_NEXT_STEP
        )
    fingerprint_before = _fingerprint(descriptor_before)
    if fingerprint_before != _fingerprint(path_before) or descriptor_before.st_size != size:
        raise ArtifactCollectionError(
            f"artifact changed before hashing: {path}", STABLE_FILE_NEXT_STEP
        )

    digest = hashlib.sha256()
    bytes_read = 0
    try:
        handle.seek(0)
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            bytes_read += len(chunk)
    except OSError as exc:
        raise ArtifactCollectionError(
            f"artifact read failed for {path}: {exc}", READ_NEXT_STEP
        ) from exc

    descriptor_after = os.fstat(handle.fileno())
    path_after = os.stat(path)
    fingerprint_after = _fingerprint(descriptor_after)
    if (
        bytes_read != size
        or fingerprint_after != fingerprint_before
        or fingerprint_after != _fingerprint(path_after)
    ):
        raise ArtifactCollectionError(
            f"artifact changed while hashing {path}: getsize={size} bytes_read={bytes_read}",
            STABLE_FILE_NEXT_STEP,
        )

    sha256 = digest.hexdigest()
    if not SHA256_PATTERN.fullmatch(sha256):
        raise ArtifactCollectionError(
            f"invalid sha256 for {path}: {sha256!r}", INTERNAL_NEXT_STEP
        )
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
            raise ArtifactCollectionError(
                f"manifest output aliases artifact: {artifact_path}",
                OUTPUT_ALIAS_NEXT_STEP,
            )
        if (
            output_exists
            and os.path.exists(artifact_path)
            and os.path.samefile(output_path, artifact_path)
        ):
            raise ArtifactCollectionError(
                f"manifest output aliases artifact: {artifact_path}",
                OUTPUT_ALIAS_NEXT_STEP,
            )


def _open_artifact(path: str, stack: ExitStack) -> BinaryIO:
    if not os.path.exists(path):
        raise ArtifactCollectionError(f"artifact does not exist: {path}", MISSING_NEXT_STEP)
    try:
        size = os.path.getsize(path)
    except OSError as exc:
        raise ArtifactCollectionError(
            f"artifact size read failed for {path}: {exc}", READ_NEXT_STEP
        ) from exc
    if size <= 0:
        raise ArtifactCollectionError(f"artifact is empty: {path}", EMPTY_NEXT_STEP)
    try:
        handle = stack.enter_context(open(path, "rb"))
        fcntl.flock(handle.fileno(), fcntl.LOCK_SH | fcntl.LOCK_NB)
    except OSError as exc:
        raise ArtifactCollectionError(
            f"artifact open or lock failed for {path}: {exc}", READ_NEXT_STEP
        ) from exc
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


def _verification_step(method: str, artifact_count: int) -> dict[str, Any]:
    return {"method": method, "artifact_count": artifact_count}


def _held_descriptor_verification(opened: Sequence[OpenArtifact]) -> dict[str, Any]:
    for artifact in opened:
        if artifact.handle.closed:
            raise ArtifactCollectionError(
                f"artifact descriptor is closed: {artifact.path}", INTERNAL_NEXT_STEP
            )
        os.fstat(artifact.handle.fileno())
    return _verification_step("held_open_descriptors", len(opened))


def _reread_and_rehash(
    opened: Sequence[OpenArtifact],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    artifacts: list[dict[str, Any]] = []
    for artifact in opened:
        record, fingerprint = _artifact_from_open_file(artifact.path, artifact.handle)
        if record != artifact.record:
            raise ArtifactCollectionError(
                f"artifact changed during verification: {artifact.path}",
                STABLE_FILE_NEXT_STEP,
            )
        artifact.record = record
        artifact.fingerprint = fingerprint
        artifacts.append(record)
    return artifacts, _verification_step(
        "reread_and_rehash_after_collection", len(artifacts)
    )


def _assert_artifacts_stable(opened: Sequence[OpenArtifact]) -> dict[str, Any]:
    for artifact in opened:
        exists = os.path.exists(artifact.path)
        if not exists:
            raise ArtifactCollectionError(
                f"artifact disappeared before manifest write: {artifact.path}",
                STABLE_FILE_NEXT_STEP,
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
                f"artifact changed before manifest write: {artifact.path}",
                STABLE_FILE_NEXT_STEP,
            )
    return _verification_step("descriptor_and_path_fingerprint_sweep", len(opened))


def _same_filesystem_commit_method(temp_path: Path, output: Path) -> str:
    if temp_path.parent != output.parent:
        raise ArtifactCollectionError(
            "manifest temp path is not beside output", OUTPUT_FILESYSTEM_NEXT_STEP
        )
    if os.stat(temp_path).st_dev != os.stat(output.parent).st_dev:
        raise ArtifactCollectionError(
            "manifest temp path is not on output filesystem", OUTPUT_FILESYSTEM_NEXT_STEP
        )
    return "same_filesystem_atomic_rename"


def _verification_guarantee(
    opened: Sequence[OpenArtifact],
    steps: Sequence[dict[str, Any]],
    commit_method: str,
) -> dict[str, Any]:
    expected_methods = (
        "held_open_descriptors",
        "reread_and_rehash_after_collection",
        "descriptor_and_path_fingerprint_sweep",
    )
    methods = tuple(step["method"] for step in steps)
    artifact_count = len(opened)
    if methods != expected_methods or any(
        step["artifact_count"] != artifact_count for step in steps
    ):
        raise ArtifactCollectionError(
            "manifest verification evidence is incomplete", INTERNAL_NEXT_STEP
        )

    residual_exists = steps[-1]["method"] != commit_method
    return {
        "tool_version": __version__,
        "artifact_count": artifact_count,
        "methods": list(steps),
        "certified_state": {
            "scope": "verified_window",
            "instantaneous": not residual_exists,
            "window_ends_after": steps[-1]["method"],
            "window_ends_before": commit_method,
        },
        "residual_window": {
            "exists": residual_exists,
            "between": [steps[-1]["method"], commit_method],
            "irreducible_without_filesystem_snapshot": residual_exists,
        },
    }


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
        held_descriptor_step = _held_descriptor_verification(opened)
        artifacts, reread_step = _reread_and_rehash(opened)
        fingerprint_step = _assert_artifacts_stable(opened)
        commit_method = _same_filesystem_commit_method(temp_path, output)
        verification = _verification_guarantee(
            opened,
            [held_descriptor_step, reread_step, fingerprint_step],
            commit_method,
        )

        manifest = {
            "generated_at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "tool_version": __version__,
            "artifacts": artifacts,
            "verification": verification,
        }
        handle.write(json.dumps(manifest, indent=2, sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
        handle.close()

        # Advisory locks cannot bind writers that ignore flock. A second full read plus
        # this final descriptor/path sweep closes detectable drift; strict simultaneity
        # across arbitrary files requires a filesystem snapshot.
        final_fingerprint_step = _assert_artifacts_stable(opened)
        final_verification = _verification_guarantee(
            opened,
            [held_descriptor_step, reread_step, final_fingerprint_step],
            commit_method,
        )
        if final_verification != verification:
            raise ArtifactCollectionError(
                "manifest verification evidence changed before commit", INTERNAL_NEXT_STEP
            )
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
        next_step = getattr(exc, "next_step", OS_ERROR_NEXT_STEP)
        print(f"ERROR: {exc}\nnext_step: {next_step}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
