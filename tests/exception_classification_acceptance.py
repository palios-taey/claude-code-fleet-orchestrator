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


RegistryRow = tuple[str, str, str, int, int, str, str, str]


def _write_registry(path: Path, rows: list[RegistryRow]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "| File | Function | Exception | Ordinal | Line Hint | Category | Rationale | Remediation |",
        "| --- | --- | --- | ---: | ---: | --- | --- | --- |",
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
        critical = temp_root / "fleet_orchestrator" / "orch_schema.py"
        critical.parent.mkdir(parents=True)
        critical.write_text(
            "\n".join(
                [
                    "# harmless line shift before the handler",
                    "def known():",
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
                    "fleet_orchestrator/orch_schema.py",
                    "known",
                    "Exception",
                    1,
                    4,
                    "harmless-best-effort",
                    "line hint intentionally predates inserted comment",
                    "no behavior change",
                )
            ],
        )
        errors = gate.check(
            temp_root,
            critical_files=("fleet_orchestrator/orch_schema.py",),
            registry_path=registry,
        )
        assert errors == [
            "fleet_orchestrator/orch_schema.py:4 known except Exception#1 "
            "line hint does not match AST handler line 5"
        ], errors
        _write_registry(
            registry,
            [
                (
                    "fleet_orchestrator/orch_schema.py",
                    "known",
                    "Exception",
                    1,
                    5,
                    "harmless-best-effort",
                    "line hint matches the AST handler",
                    "no behavior change",
                )
            ],
        )
        assert gate.check(
            temp_root,
            critical_files=("fleet_orchestrator/orch_schema.py",),
            registry_path=registry,
        ) == []

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
                    "known",
                    "Exception",
                    1,
                    4,
                    "harmless-best-effort",
                    "teeth fixture classified row",
                    "no behavior change",
                ),
                (
                    "fleet_orchestrator/critical.py",
                    "logged_defect",
                    "Exception",
                    1,
                    16,
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
                    "defect_swallow",
                    "Exception",
                    1,
                    4,
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

    with tempfile.TemporaryDirectory() as tmp:
        temp_root = Path(tmp)
        critical = temp_root / "fleet_orchestrator" / "critical.py"
        critical.parent.mkdir(parents=True)
        critical.write_text(
            "\n".join(
                [
                    "def still_present():",
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
                    "still_present",
                    "Exception",
                    1,
                    4,
                    "harmless-best-effort",
                    "teeth fixture classified row",
                    "no behavior change",
                ),
                (
                    "fleet_orchestrator/critical.py",
                    "removed_handler",
                    "Exception",
                    1,
                    10,
                    "harmless-best-effort",
                    "stale row must fail",
                    "remove stale row",
                ),
            ],
        )
        errors = gate.check(
            temp_root,
            critical_files=("fleet_orchestrator/critical.py",),
            registry_path=registry,
        )
        assert any("removed_handler" in error and "no matching handler exists" in error for error in errors), errors

    with tempfile.TemporaryDirectory() as tmp:
        temp_root = Path(tmp)
        critical = temp_root / "fleet_orchestrator" / "critical.py"
        critical.parent.mkdir(parents=True)
        critical.write_text(
            "\n".join(
                [
                    "def grouped(value):",
                    "    try:",
                    "        return value[0]",
                    "    except Exception:",
                    "        return None",
                    "    try:",
                    "        return value[1]",
                    "    except Exception:",
                    "        return None",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        registry = temp_root / "docs" / "EXCEPTION_HANDLERS.md"
        grouped_rows: list[RegistryRow] = [
            (
                "fleet_orchestrator/critical.py",
                "grouped",
                "Exception",
                1,
                4,
                "harmless-best-effort",
                "first grouped handler classified independently",
                "no behavior change",
            ),
            (
                "fleet_orchestrator/critical.py",
                "grouped",
                "Exception",
                2,
                8,
                "harmless-best-effort",
                "second grouped handler classified independently",
                "no behavior change",
            ),
        ]
        _write_registry(registry, grouped_rows)
        errors = gate.check(
            temp_root,
            critical_files=("fleet_orchestrator/critical.py",),
            registry_path=registry,
        )
        assert errors == [], errors

        critical.write_text(
            "\n".join(
                [
                    "def grouped(value):",
                    "    try:",
                    "        return value[1]",
                    "    except Exception:",
                    "        return None",
                ]
            )
            + "\n",
            encoding="utf-8",
        )
        errors = gate.check(
            temp_root,
            critical_files=("fleet_orchestrator/critical.py",),
            registry_path=registry,
        )
        assert any("grouped except Exception#2" in error and "no matching handler exists" in error for error in errors), errors

    print("exception_classification_acceptance: PASS")


if __name__ == "__main__":
    main()
