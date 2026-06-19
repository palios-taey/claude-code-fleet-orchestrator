#!/usr/bin/env python3
from __future__ import annotations

import importlib.util
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "verify-ai-native-coherence.py"


def _load_gate():
    spec = importlib.util.spec_from_file_location("verify_ai_native_coherence", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _init_fixture_root(root: Path) -> None:
    (root / "fleet_orchestrator").mkdir(parents=True, exist_ok=True)
    (root / "fleet_orchestrator" / "__init__.py").write_text("", encoding="utf-8")
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    for command in ("taey-plan", "taey-task", "taey-receipts"):
        path = root / "scripts" / command
        path.write_text("#!/bin/sh\n", encoding="utf-8")
        path.chmod(0o755)
    (root / "setup.py").write_text(
        "setup(entry_points={'console_scripts':['taey-plan=fleet_orchestrator.cli_taey_plan:main','taey-task=fleet_orchestrator.cli_taey_task:main']})\n",
        encoding="utf-8",
    )


def _write_registry(path: Path, rows: list[tuple[object, ...]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        "# AI-Native Surface Audit",
        "",
        "<!-- ai-native-surfaces:start -->",
        "| File | Function | Kind | Ordinal | Line Hint | Fingerprint | Classification | Teaching Evidence | Rationale | Review |",
        "| --- | --- | --- | ---: | ---: | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        lines.append("| " + " | ".join(str(value) for value in row) + " |")
    lines.extend(["<!-- ai-native-surfaces:end -->", ""])
    path.write_text("\n".join(lines), encoding="utf-8")


def _row(surface, classification: str = "teaches", evidence: str = "", *, line_hint: int | None = None, fingerprint: str | None = None):
    return (
        surface.file,
        surface.function,
        surface.kind,
        surface.ordinal,
        surface.line if line_hint is None else line_hint,
        surface.fingerprint if fingerprint is None else fingerprint,
        classification,
        evidence,
        "fixture rationale",
        "fixture-review",
    )


def _write_api(path: Path, body: str) -> None:
    path.write_text(
        "\n".join(
            [
                "from fastapi import APIRouter, HTTPException",
                "router = APIRouter(prefix='/api/tasks')",
                "@router.get('/{task_id}')",
                "def get_task(task_id: str):",
                "    return {'id': task_id}",
                body,
            ]
        )
        + "\n",
        encoding="utf-8",
    )


def _check(gate, root: Path, registry: Path, *, in_scope=("fleet_orchestrator/api.py",), baseline=frozenset()) -> list[str]:
    return gate.check(
        root,
        registry_path=registry,
        in_scope_api_modules=in_scope,
        cli_files=(),
        baseline_needs_fix_keys=baseline,
    )


def main() -> None:
    gate = _load_gate()
    assert gate.check(ROOT) == []

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _init_fixture_root(root)
        api = root / "fleet_orchestrator" / "api.py"
        _write_api(
            api,
            "def known():\n"
            "    raise HTTPException(status_code=400, detail='Use `taey-task status <task-id>` or GET /api/tasks/<task-id>.')",
        )
        surfaces = gate.discover_surfaces(root, in_scope_api_modules=("fleet_orchestrator/api.py",), cli_files=())
        registry = root / "docs" / "ai_native_surface_audit.md"
        _write_registry(registry, [_row(surfaces[0], line_hint=1)])
        assert _check(gate, root, registry) == []

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _init_fixture_root(root)
        _write_api(
            root / "fleet_orchestrator" / "api.py",
            "def known():\n"
            "    raise HTTPException(status_code=400, detail='Use `taey-task status <task-id>`.')\n"
            "def new_surface():\n"
            "    raise HTTPException(status_code=400, detail='Use `taey-plan next session-1`.')",
        )
        surfaces = gate.discover_surfaces(root, in_scope_api_modules=("fleet_orchestrator/api.py",), cli_files=())
        registry = root / "docs" / "ai_native_surface_audit.md"
        _write_registry(registry, [_row(surfaces[0])])
        errors = _check(gate, root, registry)
        assert any("new_surface" in error and "missing from AI-native registry" in error for error in errors), errors

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _init_fixture_root(root)
        _write_api(
            root / "fleet_orchestrator" / "api.py",
            "def known():\n"
            "    raise HTTPException(status_code=400, detail='Use `taey-task status <task-id>`.')",
        )
        surfaces = gate.discover_surfaces(root, in_scope_api_modules=("fleet_orchestrator/api.py",), cli_files=())
        stale = list(_row(surfaces[0]))
        stale[1] = "removed"
        registry = root / "docs" / "ai_native_surface_audit.md"
        _write_registry(registry, [_row(surfaces[0]), tuple(stale)])
        errors = _check(gate, root, registry)
        assert any("removed" in error and "no matching surface exists" in error for error in errors), errors

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _init_fixture_root(root)
        _write_api(
            root / "fleet_orchestrator" / "api.py",
            "def bad_cli():\n"
            "    raise HTTPException(status_code=400, detail='Run taey-fake now.')",
        )
        surfaces = gate.discover_surfaces(root, in_scope_api_modules=("fleet_orchestrator/api.py",), cli_files=())
        registry = root / "docs" / "ai_native_surface_audit.md"
        _write_registry(registry, [_row(surfaces[0])])
        errors = _check(gate, root, registry)
        assert any("unknown CLI command 'taey-fake'" in error for error in errors), errors

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _init_fixture_root(root)
        _write_api(
            root / "fleet_orchestrator" / "api.py",
            "def bad_endpoint():\n"
            "    raise HTTPException(status_code=400, detail='Retry with POST /api/does-not-exist.')",
        )
        surfaces = gate.discover_surfaces(root, in_scope_api_modules=("fleet_orchestrator/api.py",), cli_files=())
        registry = root / "docs" / "ai_native_surface_audit.md"
        _write_registry(registry, [_row(surfaces[0])])
        errors = _check(gate, root, registry)
        assert any("unknown API endpoint POST '/api/does-not-exist" in error for error in errors), errors

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _init_fixture_root(root)
        _write_api(
            root / "fleet_orchestrator" / "api.py",
            "def bad_structured():\n"
            "    raise HTTPException(status_code=400, detail={'next_step': 'taey-fake now'})",
        )
        surfaces = gate.discover_surfaces(root, in_scope_api_modules=("fleet_orchestrator/api.py",), cli_files=())
        registry = root / "docs" / "ai_native_surface_audit.md"
        _write_registry(registry, [_row(surfaces[0])])
        errors = _check(gate, root, registry)
        assert any("unknown CLI command 'taey-fake'" in error for error in errors), errors

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _init_fixture_root(root)
        (root / "fleet_orchestrator" / "new_router.py").write_text(
            "from fastapi import APIRouter, HTTPException\n"
            "router = APIRouter(prefix='/api/new')\n"
            "@router.post('/thing')\n"
            "def thing():\n"
            "    raise HTTPException(status_code=400, detail='Use taey-task list')\n",
            encoding="utf-8",
        )
        registry = root / "docs" / "ai_native_surface_audit.md"
        _write_registry(registry, [])
        errors = _check(gate, root, registry, in_scope=())
        assert any("new_router.py" in error and "not classified" in error for error in errors), errors

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _init_fixture_root(root)
        (root / "fleet_orchestrator" / "orch_schema.py").write_text(
            "class CompletionEvidenceError(Exception):\n"
            "    pass\n"
            "def bad_raise():\n"
            "    raise CompletionEvidenceError('bad')\n",
            encoding="utf-8",
        )
        surfaces = gate.discover_surfaces(root, in_scope_api_modules=(), cli_files=())
        registry = root / "docs" / "ai_native_surface_audit.md"
        _write_registry(registry, [_row(surfaces[0])])
        errors = _check(gate, root, registry, in_scope=())
        assert any("classified teaches" in error and "no real CLI" in error for error in errors), errors

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _init_fixture_root(root)
        (root / "fleet_orchestrator" / "orch_schema.py").write_text(
            "def bad_raise():\n"
            "    raise ValueError('bad')\n",
            encoding="utf-8",
        )
        surfaces = gate.discover_surfaces(root, in_scope_api_modules=(), cli_files=())
        registry = root / "docs" / "ai_native_surface_audit.md"
        _write_registry(registry, [_row(surfaces[0], classification="needs-fix")])
        errors = _check(gate, root, registry, in_scope=())
        assert any("adds non-baseline needs-fix debt" in error for error in errors), errors

    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        _init_fixture_root(root)
        api = root / "fleet_orchestrator" / "api.py"
        _write_api(
            api,
            "def grouped():\n"
            "    raise HTTPException(status_code=400, detail='Use taey-task list')\n"
            "    raise HTTPException(status_code=400, detail='Use taey-plan next session-1')",
        )
        surfaces = gate.discover_surfaces(root, in_scope_api_modules=("fleet_orchestrator/api.py",), cli_files=())
        registry = root / "docs" / "ai_native_surface_audit.md"
        _write_registry(registry, [_row(surface) for surface in surfaces])
        _write_api(
            api,
            "def grouped():\n"
            "    raise HTTPException(status_code=400, detail='Use taey-plan next session-1')\n"
            "    raise HTTPException(status_code=400, detail='Use taey-task list')",
        )
        errors = _check(gate, root, registry)
        assert any("fingerprint mismatch" in error for error in errors), errors


if __name__ == "__main__":
    main()
