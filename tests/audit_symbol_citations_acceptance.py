#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify-doc-cli-drift.py"
AUDIT = ROOT / "AUDIT.md"


def _load_gate():
    spec = importlib.util.spec_from_file_location("verify_doc_cli_drift", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def main() -> None:
    gate = _load_gate()
    errors = gate.check_doc_currency(ROOT, [AUDIT])
    assert errors == [], errors

    text = AUDIT.read_text(encoding="utf-8")
    c1 = re.search(r"- C1\..*?(?=\n- C2\.)", text, re.DOTALL)
    assert c1, "C1 block missing"
    c1_text = c1.group(0)
    for symbol in (
        "fleet_orchestrator/orch_schema.py:update_task_status",
        "fleet_orchestrator/orch_schema.py:_validate_terminal_status_write",
        "fleet_orchestrator/orch_schema.py:create_task",
        "fleet_orchestrator/orch_schema.py:complete_human_review_gate",
    ):
        assert f"`{symbol}`" in c1_text, symbol
    assert not re.search(r"`:\d+", c1_text), c1_text
    print("audit_symbol_citations_acceptance: PASS")


if __name__ == "__main__":
    main()
