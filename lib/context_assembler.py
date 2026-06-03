from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import uuid
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from lib.orch_schema import get_project_summary, get_session_next_ready, get_task_project


CORE_BUDGET_BYTES = 15 * 1024
DEFAULT_MAX_MEMORY = 4
DEFAULT_MAX_REFS_PER_TIER = 5
MEMORY_BASE = Path.home() / ".claude" / "projects"
SESSION_ROOTS = {
    "conductor": "/home/mira/the-conductor",
    "weaver": "/home/mira/isma",
    "infra": "/home/mira/infra-soul",
    "taeys-hands": "/home/mira/taeys-hands",
    "treasurer": "/home/mira/treasurer",
    "taey-ed": "/home/mira/taey-ed",
    "tutor": "/home/mira/tutor",
    "hunter": "/home/mira/hunter",
    "x-claude": "/home/mira/x-claude",
}
VALID_CLIS = {"claude", "codex", "gemini", "grok"}


def select_context(session: str, task_id: Optional[str] = None, cli: str = "claude",
                   max_memory: int = DEFAULT_MAX_MEMORY) -> Dict[str, Any]:
    session_key = _normalize_session(session)
    _load_session_env(session_key)
    work = _resolve_work(session_key, task_id)
    summary = get_project_summary(work["project_id"]) if work.get("project_id") else None

    task_text = _task_text(work, summary, task_id)
    refs = _select_refs(summary, work, task_id)
    memory_files = _read_memory_files(_memory_dirs(session_key, work, summary))
    selected_memory = _rank_memory(memory_files, task_text, max_memory=max_memory)
    rules = _select_rules(session_key, work, summary, task_text)

    context = {
        "overall_refs": refs["overall"],
        "supervisor_refs": refs["supervisor"],
        "project_refs": refs["project"],
        "phase_refs": refs["phase"],
        "task_refs": refs["task"],
        "memory": selected_memory,
        "rules": rules,
        "budget_used": 0,
    }
    context["budget_used"] = _estimate_tokens(json.dumps(context, sort_keys=True))
    return context


def assemble(packet: Dict[str, Any], cli: str, budget_bytes: int = CORE_BUDGET_BYTES,
             max_refs_per_tier: int = DEFAULT_MAX_REFS_PER_TIER,
             max_memory: Optional[int] = None) -> str:
    cli_key = cli.lower().strip()
    if cli_key not in VALID_CLIS:
        raise ValueError(f"unsupported cli: {cli}")

    normalized = _packet_with_provenance(packet)
    context = normalized.setdefault("context", {})
    if max_memory is not None:
        context["memory"] = list(context.get("memory") or [])[:max_memory]

    rendered = _render_packet(normalized, cli_key, max_refs_per_tier=max_refs_per_tier)
    if len(rendered.encode("utf-8")) <= budget_bytes:
        context["budget_used"] = _estimate_tokens(rendered)
        return rendered

    trimmed = _trim_packet(normalized, cli_key, budget_bytes, max_refs_per_tier)
    _packet_with_provenance(trimmed)
    trimmed["context"]["budget_used"] = _estimate_tokens(
        _render_packet(trimmed, cli_key, max_refs_per_tier=max_refs_per_tier)
    )
    packet.clear()
    packet.update(trimmed)
    return _render_packet(trimmed, cli_key, max_refs_per_tier=max_refs_per_tier)


def build_packet(session: str, context: Dict[str, Any]) -> Dict[str, Any]:
    packet = {
        "packet_id": str(uuid.uuid4()),
        "generated_for": _normalize_session(session),
        "generated_at_commit": _git_head(),
        "provenance_hash": "",
        "context": context,
        "cycle": {
            "cycle_n": None,
            "last_cycle_outcomes": [],
            "caps_vs_limits": {},
            "hold_list": [],
            "queued_packets": [],
            "inherited_blocked_on": None,
        },
        "human": {
            "replies_since_last": [],
            "open_questions": [],
        },
        "stop": {
            "blocked_on": None,
            "permanent": False,
            "next_contract": None,
        },
    }
    return _packet_with_provenance(packet)


def size_report(text: str, packet: Dict[str, Any], budget_bytes: int = CORE_BUDGET_BYTES) -> Dict[str, Any]:
    used = len(text.encode("utf-8"))
    context = packet.get("context", {})
    return {
        "bytes": used,
        "budget_bytes": budget_bytes,
        "under_budget": used <= budget_bytes,
        "estimated_tokens": _estimate_tokens(text),
        "memory_count": len(context.get("memory") or []),
        "rules_count": len(context.get("rules") or []),
        "provenance_hash": packet.get("provenance_hash", ""),
    }


def _normalize_session(session: str) -> str:
    value = (session or "").strip()
    for suffix in ("-codex", "-gemini", "-grok"):
        if value.endswith(suffix):
            return value[: -len(suffix)]
    return value


def _resolve_work(session: str, task_id: Optional[str]) -> Dict[str, Any]:
    if task_id:
        task_project = get_task_project(task_id)
        if not task_project:
            raise ValueError(f"task not found: {task_id}")
        summary = get_project_summary(task_project["project_id"])
        if not summary:
            raise ValueError(f"project not found for task: {task_id}")
        for phase_item in summary.get("phases", []):
            for task in phase_item.get("tasks", []):
                if task.get("id") == task_id:
                    return {
                        "task_id": task_id,
                        "description": task.get("description", ""),
                        "phase_id": phase_item.get("phase", {}).get("id"),
                        "phase_name": phase_item.get("phase", {}).get("name"),
                        "project_id": task_project["project_id"],
                        "project_name": task_project.get("project_name", ""),
                        "project_source_path": summary.get("project", {}).get("source_path", ""),
                    }
        raise ValueError(f"task missing from project summary: {task_id}")

    next_ready = get_session_next_ready(session)
    if next_ready:
        return dict(next_ready)
    return {"project_id": None, "description": "", "task_id": None}


def _load_session_env(session: str) -> None:
    root = SESSION_ROOTS.get(session)
    if not root:
        return
    env_path = Path(root) / ".env"
    if not env_path.is_file():
        return
    for raw_line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.replace("export ", "").strip()
        if key and key.startswith("ORCH_"):
            os.environ.setdefault(key, value.strip())


def _task_text(work: Dict[str, Any], summary: Optional[Dict[str, Any]], task_id: Optional[str]) -> str:
    parts = [
        str(work.get("task_id") or task_id or ""),
        str(work.get("description") or work.get("top_task_desc") or ""),
        str(work.get("project_name") or ""),
        str(work.get("phase_name") or ""),
    ]
    if summary:
        project = summary.get("project", {})
        parts.extend([str(project.get("name") or ""), str(project.get("description") or "")])
    return " ".join(part for part in parts if part).strip()


def _select_refs(summary: Optional[Dict[str, Any]], work: Dict[str, Any],
                 task_id: Optional[str]) -> Dict[str, List[Dict[str, Any]]]:
    tiers = {"overall": [], "supervisor": [], "project": [], "phase": [], "task": []}
    if summary:
        ref_tiers = summary.get("ref_tiers") or {}
        tiers["overall"] = _ref_context_entries(ref_tiers.get("overall"))
        tiers["supervisor"] = _ref_context_entries(ref_tiers.get("supervisor"))
        tiers["project"] = _ref_context_entries(ref_tiers.get("project"))

        phase_id = work.get("phase_id")
        for phase in ref_tiers.get("phases") or []:
            if phase_id and phase.get("id") == phase_id:
                tiers["phase"] = _ref_context_entries(phase)
                break

        target_task_id = task_id or work.get("task_id")
        for task in ref_tiers.get("tasks") or []:
            if target_task_id and task.get("id") == target_task_id:
                tiers["task"] = _ref_context_entries(task)
                break

    if not tiers["project"] and work.get("project_ref_context"):
        tiers["project"] = _ref_context_entries({"ref_context": work.get("project_ref_context")})
    if not tiers["phase"] and work.get("phase_ref_context"):
        tiers["phase"] = _ref_context_entries({"ref_context": work.get("phase_ref_context")})
    if not tiers["task"] and work.get("task_ref_context"):
        tiers["task"] = _ref_context_entries({"ref_context": work.get("task_ref_context")})
    return tiers


def _ref_context_entries(tier: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    if not tier:
        return []
    refs = ((tier.get("ref_context") or {}).get("refs") or [])
    return [dict(ref) for ref in refs if isinstance(ref, dict)]


def _memory_dirs(session: str, work: Dict[str, Any], summary: Optional[Dict[str, Any]]) -> List[Path]:
    candidates: List[str] = []
    root = SESSION_ROOTS.get(session)
    if root:
        candidates.append(root)
    for source in (
        work.get("project_source_path"),
        (summary or {}).get("project", {}).get("source_path") if summary else "",
    ):
        if source:
            candidates.append(str(Path(source).expanduser().resolve(strict=False).parent))

    dirs: List[Path] = []
    for candidate in candidates:
        mangled = _mangle_project_path(candidate)
        memory_dir = MEMORY_BASE / mangled / "memory"
        if memory_dir.is_dir() and memory_dir not in dirs:
            dirs.append(memory_dir)
    direct = MEMORY_BASE / session / "memory"
    if direct.is_dir() and direct not in dirs:
        dirs.append(direct)
    return dirs


def _mangle_project_path(path: str) -> str:
    resolved = str(Path(path).expanduser().resolve(strict=False))
    return resolved.replace("/", "-")


def _read_memory_files(memory_dirs: Iterable[Path]) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    seen: set[Path] = set()
    for memory_dir in memory_dirs:
        for path in sorted(memory_dir.glob("*.md")):
            if path in seen:
                continue
            seen.add(path)
            text = _read_text(path)
            frontmatter, body = _parse_frontmatter(text)
            metadata = frontmatter.get("metadata") if isinstance(frontmatter.get("metadata"), dict) else {}
            name = str(frontmatter.get("name") or path.stem)
            item_type = str(frontmatter.get("type") or metadata.get("type") or "reference")
            description = str(frontmatter.get("description") or "")
            stat = path.stat()
            items.append({
                "name": name,
                "type": item_type,
                "description": description,
                "content": body.strip(),
                "path": str(path),
                "sha256": _sha256_text(text),
                "mtime_ns": stat.st_mtime_ns,
            })
    return items


def _parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---", 4)
    if end < 0:
        return {}, text
    raw = text[4:end].strip()
    body = text[text.find("\n", end + 4) + 1:]
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


def _rank_memory(items: List[Dict[str, Any]], task_text: str, max_memory: int) -> List[Dict[str, Any]]:
    task_terms = _terms(task_text)
    ranked = []
    for item in items:
        description_terms = _terms(f"{item.get('name', '')} {item.get('description', '')}")
        score = len(task_terms & description_terms)
        if item.get("name") == "MEMORY":
            score += 1
        if score <= 0 and task_terms:
            continue
        ranked.append((score, item.get("name", ""), item))
    ranked.sort(key=lambda row: (-row[0], row[1]))
    selected = [row[2] for row in ranked[:max(0, max_memory)]]
    return [_public_memory_item(item) for item in selected]


def _public_memory_item(item: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "name": item.get("name", ""),
        "type": item.get("type", "reference"),
        "description": item.get("description", ""),
        "content": item.get("content", ""),
        "path": item.get("path", ""),
        "sha256": item.get("sha256", ""),
        "mtime_ns": item.get("mtime_ns", 0),
    }


def _select_rules(session: str, work: Dict[str, Any], summary: Optional[Dict[str, Any]],
                  task_text: str) -> List[Dict[str, Any]]:
    roots = []
    session_root = SESSION_ROOTS.get(session)
    if session_root:
        roots.append(Path(session_root))
    source = work.get("project_source_path") or (summary or {}).get("project", {}).get("source_path")
    if source:
        roots.append(Path(source).expanduser().resolve(strict=False).parent)

    rules: List[Dict[str, Any]] = []
    seen: set[Path] = set()
    for scope, names in (
        ("supervisor", ("RULES.md", "100_TIMES.md")),
        ("project", ("PROJECT_RULES.md", "RULES.md", "100_TIMES.md")),
    ):
        for root in roots:
            for name in names:
                path = root / name
                if not path.is_file() or path in seen:
                    continue
                seen.add(path)
                text = _read_text(path).strip()
                if text:
                    rules.append({
                        "scope": scope,
                        "text": text,
                        "path": str(path),
                        "sha256": _sha256_text(text),
                        "mtime_ns": path.stat().st_mtime_ns,
                    })
    return _rank_rules(rules, task_text)


def _rank_rules(rules: List[Dict[str, Any]], task_text: str) -> List[Dict[str, Any]]:
    terms = _terms(task_text)
    scored = []
    for rule in rules:
        score = len(terms & _terms(rule.get("text", "")[:2000]))
        scored.append((0 if rule.get("scope") == "supervisor" else 1, -score, rule.get("path", ""), rule))
    scored.sort()
    return [row[3] for row in scored]


def _render_packet(packet: Dict[str, Any], cli: str, max_refs_per_tier: int) -> str:
    heading = {
        "claude": "# Wake State Packet",
        "codex": "# AGENTS.md Dynamic Context",
        "gemini": "# GEMINI.md Dynamic Context",
        "grok": "# Grok Dynamic Context",
    }[cli]
    context = packet.get("context", {})
    lines = [
        heading,
        "",
        "## Provenance",
        f"- packet_id: {packet.get('packet_id', '')}",
        f"- generated_for: {packet.get('generated_for', '')}",
        f"- generated_at_commit: {packet.get('generated_at_commit', '')}",
        f"- provenance_hash: {packet.get('provenance_hash', '')}",
        "",
        "## Context Refs",
    ]
    for tier in ("overall", "supervisor", "project", "phase", "task"):
        lines.extend(_render_refs(tier, context.get(f"{tier}_refs") or [], max_refs_per_tier))
    lines.extend(["", "## Memory"])
    for item in context.get("memory") or []:
        lines.append(f"### {item.get('name', '')} [{item.get('type', 'reference')}]")
        if item.get("description"):
            lines.append(str(item["description"]))
        if item.get("content"):
            lines.append(str(item["content"]))
        lines.append("")
    if not context.get("memory"):
        lines.append("- none selected")
    lines.extend(["", "## Rules"])
    for rule in context.get("rules") or []:
        lines.append(f"### {rule.get('scope', 'project')}")
        lines.append(str(rule.get("text", "")))
        lines.append("")
    if not context.get("rules"):
        lines.append("- none selected")
    lines.extend([
        "",
        "## Cycle",
        json.dumps(packet.get("cycle", {}), sort_keys=True),
        "",
        "## Human",
        json.dumps(packet.get("human", {}), sort_keys=True),
        "",
        "## Stop",
        json.dumps(packet.get("stop", {}), sort_keys=True),
        "",
    ])
    return "\n".join(lines)


def _render_refs(tier: str, refs: List[Dict[str, Any]], max_refs: int) -> List[str]:
    lines = [f"### {tier}"]
    if not refs:
        return lines + ["- none"]
    for ref in refs[:max_refs]:
        label = f" ({ref.get('label')})" if ref.get("label") else ""
        lines.append(f"- {ref.get('path', '')}{label}")
        warning = ref.get("warning")
        if warning:
            lines.append(f"  warning: {warning}")
        content = ref.get("content")
        if content:
            lines.append("```")
            lines.append(str(content).strip())
            lines.append("```")
        for section in ref.get("sections") or []:
            if section.get("content") and section.get("content") != content:
                lines.append(f"  lines {section.get('l_start')}-{section.get('l_end')}:")
                lines.append("```")
                lines.append(str(section["content"]).strip())
                lines.append("```")
    return lines


def _trim_packet(packet: Dict[str, Any], cli: str, budget_bytes: int, max_refs_per_tier: int) -> Dict[str, Any]:
    trimmed = json.loads(json.dumps(packet))
    context = trimmed.setdefault("context", {})
    while len(_render_packet(trimmed, cli, max_refs_per_tier).encode("utf-8")) > budget_bytes:
        memory = context.get("memory") or []
        if memory:
            memory[-1]["content"] = _halve(memory[-1].get("content", ""))
            if len(memory[-1].get("content", "")) < 400:
                memory.pop()
            context["memory"] = memory
            continue
        shortened = False
        for tier in ("task", "phase", "project", "supervisor", "overall"):
            refs = context.get(f"{tier}_refs") or []
            for ref in reversed(refs):
                if ref.get("content"):
                    ref["content"] = _halve(str(ref["content"]))
                    shortened = True
                    break
            if shortened:
                break
        if shortened:
            continue
        for rule in reversed(context.get("rules") or []):
            if rule.get("text"):
                rule["text"] = _halve(str(rule["text"]))
                shortened = True
                break
        if not shortened:
            break
    return trimmed


def _halve(text: str) -> str:
    if len(text) <= 400:
        return ""
    return text[: max(0, len(text) // 2)].rstrip() + "\n[truncated]"


def _packet_with_provenance(packet: Dict[str, Any]) -> Dict[str, Any]:
    if not packet.get("generated_at_commit"):
        packet["generated_at_commit"] = _git_head()
    if not packet.get("packet_id"):
        packet["packet_id"] = str(uuid.uuid4())
    packet["provenance_hash"] = _provenance_hash(packet)
    return packet


def _provenance_hash(packet: Dict[str, Any]) -> str:
    context = packet.get("context", {})
    observed: List[Dict[str, Any]] = [{"kind": "git_head", "value": packet.get("generated_at_commit", "")}]
    for tier in ("overall", "supervisor", "project", "phase", "task"):
        for ref in context.get(f"{tier}_refs") or []:
            observed.append({
                "kind": "ref",
                "tier": tier,
                "path": ref.get("path", ""),
                "provenance_hash": ref.get("provenance_hash", ""),
            })
    for item in context.get("memory") or []:
        observed.append({
            "kind": "memory",
            "path": item.get("path", ""),
            "sha256": item.get("sha256", ""),
            "mtime_ns": item.get("mtime_ns", 0),
        })
    for rule in context.get("rules") or []:
        observed.append({
            "kind": "rule",
            "path": rule.get("path", ""),
            "sha256": rule.get("sha256", ""),
            "mtime_ns": rule.get("mtime_ns", 0),
        })
    raw = json.dumps(observed, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _git_head() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return "unknown"


def _terms(text: str) -> set[str]:
    return {term for term in re.findall(r"[a-z0-9][a-z0-9_-]{2,}", text.lower()) if term not in _STOPWORDS}


def _read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="replace")


def _sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _estimate_tokens(text: str) -> int:
    return max(1, len(text.encode("utf-8")) // 4)


_STOPWORDS = {
    "and", "for", "the", "with", "from", "that", "this", "into", "only",
    "task", "project", "session", "context", "build", "implement", "cli",
}


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Assemble dynamic wake context for a CLI session.")
    parser.add_argument("session")
    parser.add_argument("task_id", nargs="?")
    parser.add_argument("--cli", default="claude", choices=sorted(VALID_CLIS))
    parser.add_argument("--budget-bytes", type=int, default=CORE_BUDGET_BYTES)
    parser.add_argument("--max-memory", type=int, default=DEFAULT_MAX_MEMORY)
    parser.add_argument("--max-refs-per-tier", type=int, default=DEFAULT_MAX_REFS_PER_TIER)
    args = parser.parse_args(argv)

    context = select_context(args.session, args.task_id, cli=args.cli, max_memory=args.max_memory)
    packet = build_packet(args.session, context)
    rendered = assemble(
        packet,
        args.cli,
        budget_bytes=args.budget_bytes,
        max_refs_per_tier=args.max_refs_per_tier,
        max_memory=args.max_memory,
    )
    print(rendered)
    print("\n--- size report ---")
    print(json.dumps(size_report(rendered, packet, budget_bytes=args.budget_bytes), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
