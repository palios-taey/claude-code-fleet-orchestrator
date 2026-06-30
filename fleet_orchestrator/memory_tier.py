from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple


MEMORY_ROOT = Path(__file__).resolve().parent.parent / "memory"
DEFAULT_MAX_MEMORY_ITEMS = 4
DEFAULT_MAX_MEMORY_ITEM_BYTES = 3 * 1024
DEFAULT_MAX_MEMORY_TOTAL_BYTES = 6 * 1024
_SCOPE_RANK = {"project": 0, "supervisor": 1, "global": 2}


def get_memory(
    session: str,
    project: Optional[str] = None,
    memory_root: Optional[Path] = None,
    max_items: int = DEFAULT_MAX_MEMORY_ITEMS,
    max_item_bytes: int = DEFAULT_MAX_MEMORY_ITEM_BYTES,
    max_total_bytes: int = DEFAULT_MAX_MEMORY_TOTAL_BYTES,
) -> List[Dict[str, Any]]:
    root = Path(memory_root) if memory_root is not None else MEMORY_ROOT
    entries: List[Dict[str, Any]] = []
    seen: set[Path] = set()

    _append_memory_entry(entries, seen, root / "global.md", "global", "global", max_item_bytes)
    for scope, key in (("supervisor", session), ("project", project)):
        if not key:
            continue
        for path in _candidate_paths(root, scope, str(key)):
            _append_memory_entry(entries, seen, path, scope, str(key), max_item_bytes)

    entries.sort(key=lambda item: (_SCOPE_RANK.get(str(item.get("scope") or ""), 9), item.get("path", "")))
    return _budgeted(entries, max_items=max_items, max_total_bytes=max_total_bytes)


def _candidate_paths(root: Path, scope: str, key: str) -> List[Path]:
    safe_key = _safe_key(key)
    plural = f"{scope}s"
    return [
        root / plural / f"{safe_key}.md",
        root / scope / f"{safe_key}.md",
        root / f"{scope}-{safe_key}.md",
    ]


def _append_memory_entry(
    entries: List[Dict[str, Any]],
    seen: set[Path],
    path: Path,
    scope: str,
    key: str,
    max_item_bytes: int,
) -> None:
    resolved = path.resolve(strict=False)
    if resolved in seen or not path.is_file():
        return
    seen.add(resolved)
    text = path.read_text(encoding="utf-8", errors="replace")
    frontmatter, body = _parse_frontmatter(text)
    metadata = frontmatter.get("metadata") if isinstance(frontmatter.get("metadata"), dict) else {}
    description = str(frontmatter.get("description") or "")
    content = _cap_text(body.strip(), max_item_bytes)
    if not content and not description:
        return
    stat = path.stat()
    entries.append({
        "name": str(frontmatter.get("name") or path.stem),
        "type": str(frontmatter.get("type") or metadata.get("type") or "reference"),
        "description": description,
        "content": content,
        "scope": scope,
        "key": key,
        "path": str(path),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "mtime_ns": stat.st_mtime_ns,
        "size": stat.st_size,
    })


def _parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end < 0:
        return {}, text
    raw = text[4:end].strip()
    body_start = text.find("\n", end + 4)
    body = text[body_start + 1:] if body_start >= 0 else ""
    return _parse_simple_yaml(raw), body


def _parse_simple_yaml(raw: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {}
    stack: List[Tuple[int, Dict[str, Any]]] = [(-1, result)]
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#") or ":" not in line:
            continue
        indent = len(line) - len(line.lstrip(" "))
        key, value = line.strip().split(":", 1)
        while stack and indent <= stack[-1][0]:
            stack.pop()
        target = stack[-1][1]
        value = value.strip()
        if value == "":
            nested: Dict[str, Any] = {}
            target[key.strip()] = nested
            stack.append((indent, nested))
        else:
            target[key.strip()] = _unquote(value)
    return result


def _unquote(value: str) -> str:
    if (value.startswith('"') and value.endswith('"')) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    return value


def _cap_text(text: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    raw = text.encode("utf-8")
    if len(raw) <= max_bytes:
        return text
    capped = raw[:max_bytes].decode("utf-8", errors="ignore").rstrip()
    return capped + "\n\n[truncated: memory item exceeded per-item budget]"


def _budgeted(entries: List[Dict[str, Any]], max_items: int, max_total_bytes: int) -> List[Dict[str, Any]]:
    selected: List[Dict[str, Any]] = []
    used = 0
    for entry in entries:
        if len(selected) >= max(0, max_items):
            break
        size = _rendered_size(entry)
        if max_total_bytes >= 0 and used + size > max_total_bytes:
            continue
        selected.append(entry)
        used += size
    return selected


def _rendered_size(entry: Dict[str, Any]) -> int:
    return sum(
        len(str(entry.get(key) or "").encode("utf-8"))
        for key in ("name", "type", "description", "content")
    )


def _safe_key(value: str) -> str:
    safe = []
    for char in str(value).strip():
        if char.isalnum() or char in ("-", "_", "."):
            safe.append(char)
        else:
            safe.append("-")
    result = "".join(safe).strip(".-")
    if not result:
        raise ValueError("memory key contains no usable path characters")
    if len(result) > 120:
        raise ValueError("memory key is too long")
    return result
