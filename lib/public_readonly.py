from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from lib.config import OrchConfig
from lib.easy_setup import package_version
from lib.orch_schema import (
    get_neo4j_driver,
    get_project_summary,
    get_ready_tasks,
    get_session_current_work,
    get_session_next_ready,
    get_session_supervised_projects,
    list_sessions,
)
from lib.paths import data_dir, repo_root

_UI_ROOT = Path(__file__).resolve().parent.parent / "ui"
_PUBLIC_INDEX = _UI_ROOT / "public_index.html"
_PUBLIC_CSS = _UI_ROOT / "static" / "app.css"
_PUBLIC_JS = _UI_ROOT / "static" / "public-app.js"
# Cross-platform operator-path redaction (gemini p0-foundation R2 #1): the old regex was Linux-only
# (`/home/...`), so on macOS (`/Users/...`) or Windows (`C:\Users\...`) the public dashboard leaked the
# operator's username + dir layout — directly contradicting the "runs on ANY machine" goal of this PR.
_HOME_PATH_RE = re.compile(
    r"(?:/home/[^/\s:]+|/Users/[^/\s:]+|/root|[A-Za-z]:\\Users\\[^\\\s:]+)"
    r"(?:[/\\][^\s,;)\]}\"']*)?"
)
_IPV4_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6_RE = re.compile(r"\b(?:[0-9A-Fa-f]{1,4}:){2,}[0-9A-Fa-f]{0,4}\b")
_SECRET_TOKEN_RE = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|gh[pousr]_[A-Za-z0-9_]{16,}|"
    r"[A-Za-z0-9_-]*(?:token|secret|apikey|api_key|password)[A-Za-z0-9_-]*=[^\s,;]+)",
    re.IGNORECASE,
)

app = FastAPI(
    title="Fleet Orchestrator Public Readonly",
    version=package_version(),
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


def _cfg() -> OrchConfig:
    return OrchConfig()


def _all_sessions() -> List[str]:
    """The session universe for the public view — data-derived, never hardcoded."""
    return list_sessions(_cfg())


def _hidden_sessions() -> set[str]:
    raw = os.environ.get("ORCH_PUBLIC_HIDE_SESSIONS")
    # Default: hide nothing. An operator opts specific sessions out via the env var.
    values = [] if raw is None else [item.strip() for item in raw.replace(";", ",").split(",")]
    return {item for item in values if item}


def _shown_sessions() -> set[str]:
    raw = os.environ.get("ORCH_PUBLIC_SHOW_SESSIONS")
    # FAIL-CLOSED for a PUBLIC surface (gatekeeper/grok p0-foundation audit): show NOTHING unless the
    # operator explicitly opts sessions in. A public dashboard must not leak data-derived session names
    # by default — an allowlist, never a show-all denylist.
    if raw is None:
        return set()
    values = [item.strip() for item in raw.replace(";", ",").split(",")]
    return {item for item in values if item} - _hidden_sessions()


def _hidden_project_ids() -> set[str]:
    raw = os.environ.get("ORCH_PUBLIC_HIDE_PROJECT_IDS")
    if raw is None:
        values: List[str] = []
    else:
        values = [item.strip() for item in raw.replace(";", ",").split(",")]
    return {item for item in values if item}


def _public_sessions() -> List[str]:
    shown = _shown_sessions()
    if not shown:
        return []  # fail-closed: nothing opted in -> nothing to query or expose
    try:
        universe = _all_sessions()
    except Exception:
        # Public render must not 500 / leak a stack trace if Neo4j is unreachable (grok F4).
        return []
    return [session_id for session_id in universe if session_id in shown]


def _require_visible_session(session_id: str) -> None:
    if session_id not in _shown_sessions():
        raise HTTPException(status_code=404, detail="session not found")


def _normalize_value(value: Any) -> Any:
    iso = getattr(value, "iso_format", None)
    if callable(iso):
        return iso()
    if isinstance(value, list):
        return [_normalize_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _normalize_value(item) for key, item in value.items()}
    return value


def _serialize_node(node: Any) -> Dict[str, Any]:
    return {key: _normalize_value(value) for key, value in dict(node).items()}


def _decode_json(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    if isinstance(raw, str):
        return json.loads(raw)
    return raw


def _operator_path_prefixes() -> List[str]:
    """The ACTUAL resolved operator-specific path prefixes to redact — derived from this machine's
    environment (home / data dir / install root), so redaction is cross-platform and exact rather than
    a hardcoded OS literal. Longest-first so the most specific prefix wins."""
    prefixes: set[str] = set()
    for candidate in (Path.home(), data_dir(), repo_root()):
        try:
            s = str(candidate)
        except Exception:
            continue
        if s and s not in ("/", "\\"):
            prefixes.add(s)
    return sorted(prefixes, key=len, reverse=True)


def _scrub_public_text(s: Any) -> str:
    text = "" if s is None else str(s)
    # 1. dynamic: redact the actual resolved operator prefixes (cross-platform, exact)
    for prefix in _operator_path_prefixes():
        if prefix in text:
            text = text.replace(prefix, "[path]")
    # 2. generic cross-platform home-path fallback (Linux/macOS/Windows/root)
    text = _HOME_PATH_RE.sub("[path]", text)
    text = _IPV4_RE.sub("[host]", text)
    text = _IPV6_RE.sub("[host]", text)
    return _SECRET_TOKEN_RE.sub("[secret]", text)


def _project_counts(project_id: str) -> Dict[str, int]:
    summary = get_project_summary(project_id, config=_cfg())
    phases = summary.get("phases", []) if summary else []
    counts = {"phase_count": len(phases), "task_total": 0, "pending": 0, "in_progress": 0, "completed": 0, "failed": 0}
    for phase in phases:
        task_counts = phase.get("task_counts", {})
        counts["task_total"] += int(task_counts.get("total", 0) or 0)
        counts["pending"] += int(task_counts.get("pending", 0) or 0)
        counts["in_progress"] += int(task_counts.get("in_progress", 0) or 0)
        counts["completed"] += int(task_counts.get("completed", 0) or 0)
        counts["failed"] += int(task_counts.get("failed", 0) or 0)
    return counts


def _project_row(project: Dict[str, Any]) -> Dict[str, Any]:
    counts = _project_counts(project["id"])
    return {
        "id": project.get("id"),
        "name": _scrub_public_text(project.get("name")),
        "description": _scrub_public_text(project.get("description")),
        "status": project.get("status"),
        "supervisor": project.get("supervisor"),
        "priority": project.get("priority"),
        "migration_exempt": bool(project.get("migration_exempt")),
        "stop_reason_orphaned": bool(project.get("stop_reason_orphaned")),
        "user_stop_conditions": _public_stop_conditions(project.get("user_stop_conditions", [])),
        **counts,
    }


def _all_project_rows() -> List[Dict[str, Any]]:
    cfg = _cfg()
    driver = get_neo4j_driver(cfg)
    shown = _shown_sessions()
    hidden_project_ids = _hidden_project_ids()
    with driver.session(database=cfg.neo4j_db) as session:
        result = session.run("MATCH (p:OrchProject) RETURN p ORDER BY p.id")
        projects = []
        for record in result:
            project = _serialize_node(record["p"])
            project["user_stop_conditions"] = _decode_json(project.get("user_stop_conditions"), [])
            project["stop_reason_current"] = _decode_json(project.get("stop_reason_current"), None)
            project["stop_reason_history"] = _decode_json(project.get("stop_reason_history"), [])
            project["priority_history"] = _decode_json(project.get("priority_history"), [])
            project["stop_reason_orphaned"] = False
            if str(project.get("id") or "") in hidden_project_ids:
                continue
            if str(project.get("supervisor") or "") not in shown:
                continue
            projects.append(_project_row(project))
    return projects


def _pointer(ref: Dict[str, Any]) -> str:
    basename = os.path.basename(str(ref.get("path") or ""))
    return f"{basename}:L{ref.get('l_start', '?')}-L{ref.get('l_end', '?')}"


def _public_ref_pointers(record: Dict[str, Any], key: str = "refs") -> List[str]:
    refs = record.get(key) or []
    if not isinstance(refs, list):
        return []
    return [_pointer(ref) for ref in refs if isinstance(ref, dict)]


def _public_stop_conditions(raw_conditions: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw_conditions, list):
        return []
    conditions = []
    for condition in raw_conditions:
        if isinstance(condition, dict):
            conditions.append({
                "id": condition.get("id"),
                "label": _scrub_public_text(condition.get("label") or condition.get("id")),
            })
        else:
            conditions.append({"id": None, "label": _scrub_public_text(condition)})
    return conditions


def _public_project(project: Dict[str, Any], counts: Optional[Dict[str, int]] = None) -> Dict[str, Any]:
    public_project = {
        "id": project.get("id"),
        "name": _scrub_public_text(project.get("name")),
        "description": _scrub_public_text(project.get("description")),
        "status": project.get("status"),
        "supervisor": project.get("supervisor"),
        "priority": project.get("priority"),
        "migration_exempt": bool(project.get("migration_exempt")),
        "stop_reason_orphaned": bool(project.get("stop_reason_orphaned")),
        "user_stop_conditions": _public_stop_conditions(project.get("user_stop_conditions", [])),
    }
    if counts:
        public_project.update(counts)
    return public_project


def _public_phase(phase: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": phase.get("id"),
        "name": _scrub_public_text(phase.get("name")),
        "order": phase.get("order"),
    }


def _public_task(task: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "id": task.get("id"),
        "description": _scrub_public_text(task.get("description")),
        "status": task.get("status"),
        "owner": task.get("owner"),
        "priority": task.get("priority"),
        "refs": _public_ref_pointers(task),
        "is_blocked": bool(task.get("blocked_on")),
    }


def _public_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
    phases = []
    counts = {"phase_count": 0, "task_total": 0, "pending": 0, "in_progress": 0, "completed": 0, "failed": 0}
    for item in summary.get("phases", []):
        if not isinstance(item, dict):
            continue
        task_counts = dict(item.get("task_counts", {}))
        counts["phase_count"] += 1
        counts["task_total"] += int(task_counts.get("total", 0) or 0)
        counts["pending"] += int(task_counts.get("pending", 0) or 0)
        counts["in_progress"] += int(task_counts.get("in_progress", 0) or 0)
        counts["completed"] += int(task_counts.get("completed", 0) or 0)
        counts["failed"] += int(task_counts.get("failed", 0) or 0)
        phases.append({
            "phase": _public_phase(item.get("phase", {})),
            "task_counts": task_counts,
            "tasks": [_public_task(task) for task in item.get("tasks", []) if isinstance(task, dict)],
        })
    project = _public_project(summary.get("project", {}), counts)
    return {"project": project, "phases": phases}


def _public_current_work(work: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "project_id": work.get("project_id"),
        "project_name": _scrub_public_text(work.get("project_name")),
        "project_refs": _public_ref_pointers(work, "project_refs"),
        "phase_id": work.get("phase_id"),
        "phase_name": _scrub_public_text(work.get("phase_name")),
        "phase_refs": _public_ref_pointers(work, "phase_refs"),
        "top_task_id": work.get("top_task_id"),
        "top_task_desc": _scrub_public_text(work.get("top_task_desc")),
        "task_refs": _public_ref_pointers(work, "task_refs"),
    }


def _public_next_work(work: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "project_id": work.get("project_id"),
        "project_name": _scrub_public_text(work.get("project_name")),
        "project_refs": _public_ref_pointers(work, "project_refs"),
        "phase_id": work.get("phase_id"),
        "phase_name": _scrub_public_text(work.get("phase_name")),
        "phase_refs": _public_ref_pointers(work, "phase_refs"),
        "task_id": work.get("task_id"),
        "description": _scrub_public_text(work.get("description")),
        "priority": work.get("priority"),
        "owner": work.get("owner"),
        "task_refs": _public_ref_pointers(work, "task_refs"),
        "is_blocked": bool(work.get("blocked_on")),
    }


def _project_visible(project_id: str) -> bool:
    for project in _all_project_rows():
        if project.get("id") == project_id:
            return True
    return False


def _public_summary_or_404(project_id: str) -> Dict[str, Any]:
    if not _project_visible(project_id):
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    summary = get_project_summary(project_id, config=_cfg())
    if not summary:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    return _public_summary(summary)


def _current_visible(session_id: str) -> Dict[str, Any]:
    _require_visible_session(session_id)
    work = get_session_current_work(session_id, config=_cfg())
    if not work:
        return {"session": session_id, "current": None}
    if not _project_visible(str(work.get("project_id") or "")):
        return {"session": session_id, "current": None}
    return {"session": session_id, "current": _public_current_work(work)}


def _next_visible(session_id: str) -> Dict[str, Any]:
    _require_visible_session(session_id)
    work = get_session_next_ready(session_id, config=_cfg())
    if not work:
        return {"session": session_id, "next": None}
    if not _project_visible(str(work.get("project_id") or "")):
        return {"session": session_id, "next": None}
    return {"session": session_id, "next": _public_next_work(work)}


def _session_projects_visible(session_id: str) -> Dict[str, Any]:
    _require_visible_session(session_id)
    visible_project_ids = {str(project.get("id") or "") for project in _all_project_rows()}
    projects = [
        _project_row(project)
        for project in get_session_supervised_projects(session_id, config=_cfg())
        if str(project.get("id") or "") in visible_project_ids
    ]
    return {"session": session_id, "projects": projects}


def _script_safe_json(obj: Any) -> str:
    """JSON safe to embed inside an inline <script>. json.dumps escapes quotes/backslashes but NOT
    ``<`` ``>`` ``&`` — so (ensure_ascii also escapes U+2028/U+2029) a data-derived value containing
    ``</script>`` would break out of the inline script and execute (stored XSS; gatekeeper + grok
    p0-foundation BLOCK, both with executed PoCs). Neutralize those so the value can never close the
    script element or terminate a JS string."""
    return (
        json.dumps(obj)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def _public_index_html() -> str:
    template = _PUBLIC_INDEX.read_text(encoding="utf-8")
    template = template.replace("__PUBLIC_SESSIONS__", _script_safe_json(_public_sessions()))
    return template


@app.get("/health")
def health() -> Dict[str, Any]:
    try:
        _ = get_ready_tasks(_cfg())
        return {"ok": True}
    except Exception:
        return JSONResponse(status_code=503, content={"ok": False})


@app.get("/api/projects")
def list_projects() -> Dict[str, Any]:
    return {"projects": _all_project_rows()}


@app.get("/api/projects/{project_id}")
def get_project(project_id: str) -> Dict[str, Any]:
    return _public_summary_or_404(project_id)


@app.get("/api/sessions/{session_id}/projects")
def session_projects(session_id: str) -> Dict[str, Any]:
    return _session_projects_visible(session_id)


@app.get("/api/sessions/{session_id}/current")
def session_current(session_id: str) -> Dict[str, Any]:
    return _current_visible(session_id)


@app.get("/api/sessions/{session_id}/next-ready")
def session_next_ready(session_id: str) -> Dict[str, Any]:
    return _next_visible(session_id)


@app.get("/", include_in_schema=False)
def root_redirect() -> RedirectResponse:
    return RedirectResponse(url="/ui/", status_code=302)


@app.get("/ui/", response_class=HTMLResponse, include_in_schema=False)
def public_ui() -> str:
    return _public_index_html()


@app.get("/ui/static/app.css", include_in_schema=False)
def public_ui_css() -> FileResponse:
    return FileResponse(_PUBLIC_CSS)


@app.get("/ui/static/public-app.js", include_in_schema=False)
def public_ui_js() -> FileResponse:
    return FileResponse(_PUBLIC_JS, media_type="application/javascript")
