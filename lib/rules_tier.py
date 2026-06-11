from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional


RULES_ROOT = Path(__file__).resolve().parent.parent / "rules"


def get_rules(session: str, project: Optional[str] = None, rules_root: Optional[Path] = None) -> List[Dict[str, Any]]:
    root = Path(rules_root) if rules_root is not None else RULES_ROOT
    entries: List[Dict[str, Any]] = []
    seen: set[Path] = set()

    for scope, key in (("supervisor", session), ("project", project)):
        if not key:
            continue
        for path in _candidate_paths(root, scope, str(key)):
            resolved = path.resolve(strict=False)
            if resolved in seen or not path.is_file():
                continue
            seen.add(resolved)
            text = path.read_text(encoding="utf-8", errors="replace").strip()
            if not text:
                continue
            stat = path.stat()
            entries.append({
                "scope": scope,
                "key": str(key),
                "text": text,
                "path": str(path),
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "mtime_ns": stat.st_mtime_ns,
                "size": stat.st_size,
            })
    return entries


def write_supervisor_rules(session: str, text: str, rules_root: Optional[Path] = None) -> Path:
    return _write_rules("supervisor", session, text, rules_root)


def write_project_rules(project: str, text: str, rules_root: Optional[Path] = None) -> Path:
    return _write_rules("project", project, text, rules_root)


def read_supervisor_rules(session: str, rules_root: Optional[Path] = None) -> str:
    return _read_first("supervisor", session, rules_root)


def read_project_rules(project: str, rules_root: Optional[Path] = None) -> str:
    return _read_first("project", project, rules_root)


def _candidate_paths(root: Path, scope: str, key: str) -> List[Path]:
    safe_key = _safe_key(key)
    plural = f"{scope}s"
    return [
        root / plural / f"{safe_key}.md",
        root / scope / f"{safe_key}.md",
        root / f"{scope}-{safe_key}.md",
    ]


def _write_rules(scope: str, key: str, text: str, rules_root: Optional[Path]) -> Path:
    if not key or not str(key).strip():
        raise ValueError(f"{scope} key is required")
    root = Path(rules_root) if rules_root is not None else RULES_ROOT
    path = root / f"{scope}s" / f"{_safe_key(key)}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(str(text).strip() + "\n", encoding="utf-8")
    return path


def _read_first(scope: str, key: str, rules_root: Optional[Path]) -> str:
    root = Path(rules_root) if rules_root is not None else RULES_ROOT
    for path in _candidate_paths(root, scope, key):
        if path.is_file():
            return path.read_text(encoding="utf-8", errors="replace")
    return ""


def _safe_key(value: str) -> str:
    safe = []
    for char in str(value).strip():
        if char.isalnum() or char in ("-", "_", "."):
            safe.append(char)
        else:
            safe.append("-")
    result = "".join(safe).strip(".-")
    if not result:
        raise ValueError("rule key contains no usable path characters")
    if len(result) > 120:
        raise ValueError("rule key is too long")
    return result
