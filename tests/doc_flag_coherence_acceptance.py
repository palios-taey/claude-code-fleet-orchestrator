#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify-doc-flag-coherence.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("verify_doc_flag_coherence", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_config(root: Path, names: list[str], defaults: dict[str, str] | None = None) -> None:
    docs = root / "docs"
    docs.mkdir(exist_ok=True)
    defaults = defaults or {}
    rows = [f"| `{name}` | {defaults.get(name, 'default')} | purpose |" for name in names]
    (docs / "CONFIGURATION.md").write_text(
        "# Configuration\n\n| Flag | Default | Purpose |\n|---|---|---|\n" + "\n".join(rows) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    gate = _load_gate()
    with tempfile.TemporaryDirectory(prefix="orch-doc-flag-coherence-") as tmp:
        root = Path(tmp)
        package = root / "fleet_orchestrator"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "flags.py").write_text(
            """
import os

def probe():
    os.environ.get("ORCH_CODE_FLAG")
    os.getenv("ORCH_GETENV_FLAG")
    if "ORCH_MEMBERSHIP_FLAG" in os.environ:
        pass
    _require_env("ORCH_REQUIRED_FLAG")
    default_on_feature_enabled("ORCH_FEATURE_FLAG", aliases=("ORCH_ALIAS_FLAG",))
    _optional_env("ORCH_OPTIONAL_FLAG")
""",
            encoding="utf-8",
        )
        (package / "config.py").write_text(
            'REQUIRED_ENV = ("ORCH_REQUIRED_FLAG",)\nOPTIONAL_ENV = ()\n',
            encoding="utf-8",
        )
        scripts = root / "scripts"
        scripts.mkdir()
        (scripts / "probe").write_text(
            '#!/usr/bin/env python3\nimport os\nos.environ.get("CF_SCRIPT_FLAG")\n',
            encoding="utf-8",
        )

        code_names = sorted(gate.code_env_reads(root))
        _write_config(root, [*code_names, "ORCH_TEST_NAMESPACE", "PATH"], {"ORCH_REQUIRED_FLAG": "(required)"})
        assert gate.check_repo(root) == []

        _write_config(root, [*code_names, "ORCH_TEST_NAMESPACE", "PATH"], {"ORCH_REQUIRED_FLAG": "127.0.0.1"})
        default_errors = gate.check_repo(root)
        assert any("code requires it" in item and "ORCH_REQUIRED_FLAG" in item for item in default_errors), default_errors

        _write_config(root, [*code_names, "ORCH_DOCS_ONLY"])
        docs_only_errors = gate.check_repo(root)
        assert any("documents env flag not read" in item and "ORCH_DOCS_ONLY" in item for item in docs_only_errors), docs_only_errors

        _write_config(root, [name for name in code_names if name != "ORCH_CODE_FLAG"])
        code_only_errors = gate.check_repo(root)
        assert any("code reads undocumented env flag" in item and "ORCH_CODE_FLAG" in item for item in code_only_errors), code_only_errors

    print("doc_flag_coherence_acceptance: PASS")


if __name__ == "__main__":
    main()
