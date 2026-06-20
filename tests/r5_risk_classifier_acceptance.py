#!/usr/bin/env python3
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "r5-risk-classifier"


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, check=False)


def _must_run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    result = _run(cmd, cwd)
    if result.returncode != 0:
        raise AssertionError(
            f"command failed: {' '.join(cmd)}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")


def _commit(repo: Path, message: str) -> str:
    _must_run(["git", "add", "."], repo)
    _must_run(["git", "commit", "-m", message], repo)
    return _must_run(["git", "rev-parse", "HEAD"], repo).stdout.strip()


def _classifier(repo: Path, base: str, head: str) -> subprocess.CompletedProcess[str]:
    return _must_run(
        [
            sys.executable,
            str(SCRIPT),
            base,
            head,
            "--risk-paths",
            ".github/r5-risky-paths",
        ],
        repo,
    )


def _base_repo() -> tuple[Path, str]:
    repo = Path(tempfile.mkdtemp(prefix="r5-risk-classifier-"))
    _must_run(["git", "init"], repo)
    _must_run(["git", "config", "user.email", "r5@example.invalid"], repo)
    _must_run(["git", "config", "user.name", "R5 Probe"], repo)
    _write(
        repo / ".github/r5-risky-paths",
        """
        # normal risky paths
        fleet_orchestrator/*.py
        .github/workflows/*.yml
        .github/r5-risky-paths
        scripts/r5-risk-classifier
        """,
    )
    _write(
        repo / "docs/ai_native_surface_audit.md",
        """
        # AI-Native Surface Audit

        <!-- ai-native-surfaces:start -->
        | File | Function | Kind | Ordinal | Line Hint | Fingerprint | Classification | Teaching Evidence | Rationale | Review |
        | --- | --- | --- | ---: | ---: | --- | --- | --- | --- | --- |
        | fleet_orchestrator/foo.py | one | cli_failure_message | 1 | 10 | abc | needs-fix |  | Baseline debt. | baseline |
        <!-- ai-native-surfaces:end -->
        """,
    )
    _write(repo / "docs/notes.md", "# Notes\n")
    base = _commit(repo, "base")
    return repo, base


def main() -> int:
    failures: list[str] = []

    def check(label: str, condition: bool, detail: str = "") -> None:
        print(("PASS " if condition else "FAIL ") + label + ("" if condition else f" -> {detail}"))
        if not condition:
            failures.append(label)

    repo, base = _base_repo()
    try:
        _write(
            repo / "docs/ai_native_surface_audit.md",
            """
            # AI-Native Surface Audit

            <!-- ai-native-surfaces:start -->
            | File | Function | Kind | Ordinal | Line Hint | Fingerprint | Classification | Teaching Evidence | Rationale | Review |
            | --- | --- | --- | ---: | ---: | --- | --- | --- | --- | --- |
            | fleet_orchestrator/foo.py | one | cli_failure_message | 1 | 10 | abc | needs-fix |  | Baseline debt. | baseline |
            | fleet_orchestrator/foo.py | two | cli_failure_message | 2 | 20 | def | exempt |  | Reviewed override. | reviewer |
            <!-- ai-native-surfaces:end -->
            """,
        )
        head = _commit(repo, "add exempt row")
        result = _classifier(repo, base, head)
        check(
            "added exempt registry row requires R5",
            "RISKY=1" in result.stdout and "gate-override registry" in result.stdout,
            result.stdout,
        )

        _must_run(["git", "reset", "--hard", base], repo)
        text = (repo / "docs/ai_native_surface_audit.md").read_text(encoding="utf-8")
        (repo / "docs/ai_native_surface_audit.md").write_text(
            text.replace("| abc | needs-fix |", "| abc | exempt |"),
            encoding="utf-8",
        )
        head = _commit(repo, "modify row to exempt")
        result = _classifier(repo, base, head)
        check("modified row to exempt requires R5", "RISKY=1" in result.stdout, result.stdout)

        _must_run(["git", "reset", "--hard", base], repo)
        _write(repo / "docs/notes.md", "# Notes\n\nOnly prose changed.\n")
        head = _commit(repo, "docs only")
        result = _classifier(repo, base, head)
        check("non-registry docs-only change still auto-skips", "RISKY=0" in result.stdout, result.stdout)

        _must_run(["git", "reset", "--hard", base], repo)
        _write(repo / "fleet_orchestrator" / "changed.py", "VALUE = 1\n")
        head = _commit(repo, "risky code")
        result = _classifier(repo, base, head)
        check("existing risky path globs still require R5", "RISKY=1" in result.stdout, result.stdout)

        if failures:
            print(f"\nFAIL - {len(failures)} assertion(s): {failures}")
            return 1
        print("\nPASS - R5 risk classifier gates registry exemption overrides without over-gating ordinary docs")
        return 0
    finally:
        shutil.rmtree(repo, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
