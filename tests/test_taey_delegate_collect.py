"""Behavioural tests for `taey-delegate collect` hardening (collect-hardening-001).

Each test drives the real argument parser and the real handler, so it exercises the same
path the installed console script takes rather than a reimplementation of it.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from fleet_orchestrator.cli_taey_delegate import (
    ArtifactCollectionError,
    build_parser,
)


def run_collect(*argv: str) -> int:
    """Invoke `collect` exactly as the CLI does."""
    args = build_parser().parse_args(["collect", *argv])
    return args.handler(args)


def write(path: Path, body: str) -> Path:
    path.write_text(body, encoding="utf-8")
    return path


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------------------- happy path


def test_happy_path_manifest_content_is_correct(tmp_path: Path) -> None:
    first = write(tmp_path / "one.txt", "first artifact\n")
    second = write(tmp_path / "two.txt", "second artifact body\n")
    output = tmp_path / "artifacts.json"

    assert run_collect(str(first), str(second), "-o", str(output)) == 0

    manifest = json.loads(output.read_text(encoding="utf-8"))
    artifacts = manifest["artifacts"]
    assert len(artifacts) == 2

    by_path = {entry["path"]: entry for entry in artifacts}
    for source in (first, second):
        entry = by_path[os.path.realpath(source)]
        assert entry["exists"] is True
        # The manifest's numbers must come from the bytes on disk, not from the caller.
        assert entry["bytes"] == source.stat().st_size
        assert entry["sha256"] == sha256_of(source)

    verification = manifest["verification"]
    assert verification["artifact_count"] == 2
    assert [step["method"] for step in verification["methods"]] == [
        "held_open_descriptors",
        "reread_and_rehash_after_collection",
        "descriptor_and_path_fingerprint_sweep",
    ]
    assert (
        verification["certified_state"]["window_ends_before"]
        == "same_filesystem_atomic_rename_with_directory_fsync"
    )


# ------------------------------------------------------------------------ symlink refusal


def test_symlink_at_output_is_refused(tmp_path: Path) -> None:
    artifact = write(tmp_path / "artifact.txt", "payload\n")
    victim = write(tmp_path / "victim.json", "do not overwrite me\n")
    link = tmp_path / "manifest.json"
    link.symlink_to(victim)

    with pytest.raises(ArtifactCollectionError) as excinfo:
        run_collect(str(artifact), "-o", str(link))

    assert "symlink" in str(excinfo.value)
    # The link and the file it points at are both untouched.
    assert link.is_symlink()
    assert victim.read_text(encoding="utf-8") == "do not overwrite me\n"


def test_symlink_at_output_is_refused_even_with_force(tmp_path: Path) -> None:
    """--force permits replacing a regular file only; a symlink is never written through."""
    artifact = write(tmp_path / "artifact.txt", "payload\n")
    victim = write(tmp_path / "victim.json", "do not overwrite me\n")
    link = tmp_path / "manifest.json"
    link.symlink_to(victim)

    with pytest.raises(ArtifactCollectionError) as excinfo:
        run_collect(str(artifact), "-o", str(link), "--force")

    assert "symlink" in str(excinfo.value)
    assert link.is_symlink()
    assert victim.read_text(encoding="utf-8") == "do not overwrite me\n"


# ----------------------------------------------------------------------- no-clobber refusal


def test_existing_regular_file_is_refused_by_default(tmp_path: Path) -> None:
    artifact = write(tmp_path / "artifact.txt", "payload\n")
    output = write(tmp_path / "artifacts.json", "previous manifest\n")

    with pytest.raises(ArtifactCollectionError) as excinfo:
        run_collect(str(artifact), "-o", str(output))

    assert "already exists" in str(excinfo.value)
    assert output.read_text(encoding="utf-8") == "previous manifest\n"


def test_existing_directory_at_output_is_refused_even_with_force(tmp_path: Path) -> None:
    artifact = write(tmp_path / "artifact.txt", "payload\n")
    output = tmp_path / "artifacts.json"
    output.mkdir()

    with pytest.raises(ArtifactCollectionError) as excinfo:
        run_collect(str(artifact), "-o", str(output), "--force")

    assert "not a regular file" in str(excinfo.value)
    assert output.is_dir()


# ------------------------------------------------------------------------------ force path


def test_force_replaces_an_existing_regular_file(tmp_path: Path) -> None:
    artifact = write(tmp_path / "artifact.txt", "payload\n")
    output = write(tmp_path / "artifacts.json", "previous manifest\n")

    assert run_collect(str(artifact), "-o", str(output), "--force") == 0

    manifest = json.loads(output.read_text(encoding="utf-8"))
    assert [entry["path"] for entry in manifest["artifacts"]] == [
        os.path.realpath(artifact)
    ]
    assert manifest["artifacts"][0]["sha256"] == sha256_of(artifact)


def test_force_leaves_no_staging_file_behind(tmp_path: Path) -> None:
    artifact = write(tmp_path / "artifact.txt", "payload\n")
    output = write(tmp_path / "artifacts.json", "previous manifest\n")

    assert run_collect(str(artifact), "-o", str(output), "--force") == 0

    leftovers = [entry.name for entry in tmp_path.iterdir() if entry.name.endswith(".tmp")]
    assert leftovers == []


# ------------------------------------------------------------------------- duplicate inputs


def test_duplicate_input_paths_are_a_hard_error(tmp_path: Path) -> None:
    artifact = write(tmp_path / "artifact.txt", "payload\n")
    output = tmp_path / "artifacts.json"

    with pytest.raises(ArtifactCollectionError) as excinfo:
        run_collect(str(artifact), str(artifact), "-o", str(output))

    message = str(excinfo.value)
    assert "duplicate artifact paths" in message
    # The error names the offending path rather than silently deduplicating it.
    assert os.path.realpath(artifact) in message
    assert not output.exists()


def test_duplicate_via_symlink_is_detected_and_names_both_declarations(
    tmp_path: Path,
) -> None:
    artifact = write(tmp_path / "artifact.txt", "payload\n")
    alias = tmp_path / "alias.txt"
    alias.symlink_to(artifact)
    output = tmp_path / "artifacts.json"

    with pytest.raises(ArtifactCollectionError) as excinfo:
        run_collect(str(artifact), str(alias), "-o", str(output))

    message = str(excinfo.value)
    assert "duplicate artifact paths" in message
    assert str(alias) in message
    assert str(artifact) in message
    assert not output.exists()


# ------------------------------------------- behaviour 1: the held directory descriptor
# Beyond the order's literal test list; behaviour 1 would otherwise ship untested.


def test_commit_uses_the_held_directory_descriptor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The rename must resolve through held descriptors, not through a pathname."""
    artifact = write(tmp_path / "artifact.txt", "payload\n")
    output = tmp_path / "artifacts.json"

    real_replace = os.replace
    seen: list[dict[str, object]] = []

    def recording_replace(src, dst, *args, **kwargs):
        seen.append(
            {
                "src": src,
                "dst": dst,
                "src_dir_fd": kwargs.get("src_dir_fd"),
                "dst_dir_fd": kwargs.get("dst_dir_fd"),
            }
        )
        return real_replace(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, "replace", recording_replace)
    assert run_collect(str(artifact), "-o", str(output)) == 0

    assert len(seen) == 1
    call = seen[0]
    assert call["src_dir_fd"] is not None, "rename source was resolved by pathname"
    assert call["dst_dir_fd"] is not None, "rename target was resolved by pathname"
    assert call["src_dir_fd"] == call["dst_dir_fd"], "staging and target must share one fd"
    # Both operands are bare names resolved relative to the held descriptor.
    assert os.path.dirname(str(call["src"])) == ""
    assert os.path.dirname(str(call["dst"])) == ""
    assert str(call["dst"]) == output.name


def test_positive_control_the_recording_hook_would_notice_a_pathname_rename(
    tmp_path: Path,
) -> None:
    """Guards the test above: a pathname rename really does record dir_fd as None.

    Without this, `src_dir_fd is not None` could pass for a reason unrelated to the code
    under test, and the assertion would prove nothing.
    """
    seen: list[object] = []
    real_replace = os.replace

    def recording_replace(src, dst, *args, **kwargs):
        seen.append(kwargs.get("src_dir_fd"))
        return real_replace(src, dst, *args, **kwargs)

    source = write(tmp_path / "a.txt", "x\n")
    target = tmp_path / "b.txt"
    recording_replace(str(source), str(target))

    assert seen == [None]
    assert target.exists()
