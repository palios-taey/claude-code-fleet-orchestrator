#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify-exception-classification.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("verify_exception_classification", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _write_registry(path: Path, rows: list[tuple[str, int, str, str, str, str]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "| File | Line | Function | Category | Rationale | Remediation |",
        "| --- | ---: | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(value) for value in row) + " |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    gate = _load_gate()
    errors = gate.check(ROOT)
    assert errors == [], "\n".join(errors)

    with tempfile.TemporaryDirectory() as tmp:
        temp_root = Path(tmp)
        critical = temp_root / "fleet_orchestrator" / "critical.py"
        critical.parent.mkdir(parents=True)
        critical.write_text(
            "\n".join(
                [
                    "def known():",
                    "    try:",
                    "        return 1",
                    "    except Exception:",
                    "        return None",
                    "",
                    "def new_unclassified():",
                    "    try:",
                    "        return 2",
                    "    except Exception:",
                    "        return None",
                    "",
                    "def logged_defect():",
                    "    try:",
                    "        return 3",
                    "    except Exception as exc:",
                    "        LOGGER.exception('visible')",
                    "        return None",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        registry = temp_root / "docs" / "EXCEPTION_HANDLERS.md"
        _write_registry(
            registry,
            [
                (
                    "fleet_orchestrator/critical.py",
                    4,
                    "known",
                    "harmless-best-effort",
                    "teeth fixture classified row",
                    "no behavior change",
                ),
                (
                    "fleet_orchestrator/critical.py",
                    16,
                    "logged_defect",
                    "defect",
                    "teeth fixture defect row",
                    "logged",
                ),
            ],
        )
        errors = gate.check(
            temp_root,
            critical_files=("fleet_orchestrator/critical.py",),
            registry_path=registry,
        )
        assert any("new_unclassified" in error and "unclassified" in error for error in errors), errors

    with tempfile.TemporaryDirectory() as tmp:
        temp_root = Path(tmp)
        critical = temp_root / "fleet_orchestrator" / "critical.py"
        critical.parent.mkdir(parents=True)
        critical.write_text(
            "\n".join(
                [
                    "def defect_swallow():",
                    "    try:",
                    "        return 1",
                    "    except Exception:",
                    "        return None",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        registry = temp_root / "docs" / "EXCEPTION_HANDLERS.md"
        _write_registry(
            registry,
            [
                (
                    "fleet_orchestrator/critical.py",
                    4,
                    "defect_swallow",
                    "defect",
                    "teeth fixture defect row",
                    "missing log must fail",
                )
            ],
        )
        errors = gate.check(
            temp_root,
            critical_files=("fleet_orchestrator/critical.py",),
            registry_path=registry,
        )
        assert any("defect handler lacks observable logging" in error for error in errors), errors

    print("exception_classification_acceptance: PASS")


if __name__ == "__main__":
    main()
