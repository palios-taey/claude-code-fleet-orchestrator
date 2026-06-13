#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import os
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


def _prepend_path(path: Path) -> str:
    old_path = os.environ.get("PATH", "")
    os.environ["PATH"] = f"{path}{os.pathsep}{old_path}"
    return old_path


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

        path_bin = root / "bin"
        path_bin.mkdir()
        _write_demo_cli(path_bin / "taey-installed")
        old_path = _prepend_path(path_bin)
        try:
            external_docs = root / "external-docs"
            external_docs.mkdir()
            (external_docs / "SUPERVISOR_ONBOARDING.md").write_text(
                "Original bug: `taey-installed run --blocked-on live-task`.\n",
                encoding="utf-8",
            )
            path_errors = gate.check_repo(
                root,
                doc_paths=[external_docs],
                include_path=True,
                path_cli_names=["taey-installed"],
            )
        finally:
            os.environ["PATH"] = old_path
        assert any("does not expose option `--blocked-on`" in item for item in path_errors), path_errors

        package = root / "fleet_orchestrator"
        package.mkdir()
        (package / "__init__.py").write_text("__version__ = '0.test'\n", encoding="utf-8")
        (package / "demo.py").write_text("app = object()\nORCH_KNOWN_ENV = 'set'\n", encoding="utf-8")
        (package / "dispatch.py").write_text("def bind_current_task():\n    pass\n", encoding="utf-8")
        (scripts / "good-tool").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        (root / "tests").mkdir()
        (root / "tests" / "good_acceptance.py").write_text("print('ok')\n", encoding="utf-8")
        (docs / "PLAN_FORMAT.md").write_text("# Plan\n", encoding="utf-8")
        (docs / "REAL_BARE.md").write_text("# Real\n", encoding="utf-8")
        (root / "config").mkdir()
        (root / "config" / "real.yaml").write_text("ok: true\n", encoding="utf-8")
        (docs / "currency-good.md").write_text(
            "\n".join(
                [
                    "[Plan](PLAN_FORMAT.md)",
                    "[Plan Ref][plan-ref]",
                    "[plan-ref]: PLAN_FORMAT.md",
                    '<a href="PLAN_FORMAT.md">Plan HTML</a>',
                    "`scripts/good-tool`",
                    "`tests/good_acceptance.py`",
                    "`REAL_BARE.md`",
                    "`config/real.yaml`",
                    "`fleet_orchestrator.demo:app`",
                    "`ORCH_KNOWN_ENV`",
                    "`bind_current_task()`",
                    "Plain prose taey-demo run --count 2 is valid.",
                ]
            ),
            encoding="utf-8",
        )
        assert not gate.check_doc_currency(root, [docs / "currency-good.md"])

        (docs / "currency-bad.md").write_text(
            "\n".join(
                [
                    "[Missing](MISSING.md)",
                    "[Ghost Ref][ghost-ref]",
                    "[ghost-ref]: MISSING_REF.md",
                    '<a href="MISSING_HTML.md">ghost html</a>',
                    "Plain prose taey-bogus-plain should fail.",
                    "`scripts/missing-tool`",
                    "`tests/missing_acceptance.py`",
                    "`MISSING_BARE.md`",
                    "`config/secret.yaml`",
                    "`fleet_orchestrator.demo:missing_symbol`",
                    "`fleet_orchestrator.missing:app`",
                    "`ORCH_DOCS_ONLY`",
                    "`_bind_orch_task_if_ready()`",
                ]
            ),
            encoding="utf-8",
        )
        currency_errors = gate.check_doc_currency(root, [docs / "currency-bad.md"])
        assert any("markdown link target does not exist" in item and "MISSING.md" in item for item in currency_errors), currency_errors
        assert any("reference-style link target does not exist" in item and "MISSING_REF.md" in item for item in currency_errors), currency_errors
        assert any("HTML link target does not exist" in item and "MISSING_HTML.md" in item for item in currency_errors), currency_errors
        assert any("documented repo path does not exist" in item and "scripts/missing-tool" in item for item in currency_errors), currency_errors
        assert any("documented repo path does not exist" in item and "tests/missing_acceptance.py" in item for item in currency_errors), currency_errors
        assert any("documented repo-shaped file does not exist" in item and "MISSING_BARE.md" in item for item in currency_errors), currency_errors
        assert any("documented repo-shaped file does not exist" in item and "config/secret.yaml" in item for item in currency_errors), currency_errors
        assert any("documented Python entrypoint does not exist" in item and "fleet_orchestrator.demo:missing_symbol" in item for item in currency_errors), currency_errors
        assert any("documented Python entrypoint does not exist" in item and "fleet_orchestrator.missing:app" in item for item in currency_errors), currency_errors
        assert any("documented env var is not referenced by repo code/config" in item and "ORCH_DOCS_ONLY" in item for item in currency_errors), currency_errors
        assert any("documented function call does not exist" in item and "_bind_orch_task_if_ready()" in item for item in currency_errors), currency_errors

        command_errors = gate.check_repo(root, doc_paths=[docs / "currency-bad.md"])
        assert any("unknown documented CLI `taey-bogus-plain`" in item for item in command_errors), command_errors

    print("doc_cli_drift_acceptance: PASS")


if __name__ == "__main__":
    main()
