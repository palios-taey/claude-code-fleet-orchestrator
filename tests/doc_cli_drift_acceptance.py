#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import stat
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify-doc-cli-drift.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("verify_doc_cli_drift", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_demo_cli(path: Path) -> None:
    path.write_text(
        """#!/usr/bin/env python3
import argparse

parser = argparse.ArgumentParser(prog="taey-demo")
sub = parser.add_subparsers(dest="command", required=True)
run = sub.add_parser("run")
run.add_argument("--count")
sub.add_parser("status")
parser.parse_args()
""",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def main() -> None:
    gate = _load_gate()
    with tempfile.TemporaryDirectory(prefix="orch-doc-cli-drift-") as tmp:
        root = Path(tmp)
        scripts = root / "scripts"
        docs = root / "docs"
        scripts.mkdir()
        docs.mkdir()
        (root / "README.md").write_text("Run `taey-demo run --count 2`.\n", encoding="utf-8")
        _write_demo_cli(scripts / "taey-demo")

        assert gate.check_repo(root) == []

        (docs / "bad.md").write_text(
            "\n".join(
                [
                    "Unknown command: `taey-missing run`.",
                    "Unknown subcommand: `taey-demo launch`.",
                    "Unknown flag: `taey-demo run --missing`.",
                ]
            ),
            encoding="utf-8",
        )
        errors = gate.check_repo(root)
        assert any("unknown documented CLI `taey-missing`" in item for item in errors), errors
        assert any("does not expose subcommand `launch`" in item for item in errors), errors
        assert any("does not expose option `--missing`" in item for item in errors), errors

    print("doc_cli_drift_acceptance: PASS")


if __name__ == "__main__":
    main()
