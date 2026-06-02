from __future__ import annotations

import json
import os
import time
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
)

_UI_ROOT = Path(__file__).resolve().parent.parent / "ui"
_PUBLIC_INDEX = _UI_ROOT / "public_index.html"
_PUBLIC_CSS = _UI_ROOT / "static" / "app.css"
_PUBLIC_JS = _UI_ROOT / "static" / "public-app.js"
_DEFAULT_HIDE_SESSIONS = ("taeys-hands", "x-claude", "treasurer")
_UI_SESSIONS = (
    "conductor",
    "weaver",
    "tutor",
    "infra",
    "taeys-hands",
    "treasurer",
    "hunter",
    "taey-ed",
    "x-claude",
)

app = FastAPI(title="Fleet Orchestrator Public Readonly", version=package_version())


def _cfg() -> OrchConfig:
    return OrchConfig()


def _hidden_sessions() -> set[str]:
    raw = os.environ.get("ORCH_PUBLIC_HIDE_SESSIONS")
    if raw is None:
        values = list(_DEFAULT_HIDE_SESSIONS)
    else:
        values = [item.strip() for item in raw.replace(";", ",").split(",")]
    return {item for item in values if item}


def _public_sessions() -> List[str]:
    hidden = _hidden_sessions()
    return [session_id for session_id in _UI_SESSIONS if session_id not in hidden]


def _require_visible_session(session_id: str) -> None:
    if session_id in _hidden_sessions():
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
        "name": project.get("name"),
        "description": project.get("description"),
        "status": project.get("status"),
        "source_path": project.get("source_path"),
        "source_kind": project.get("source_kind"),
        "source_sha256": project.get("source_sha256"),
        "user_stop_conditions": project.get("user_stop_conditions", []),
        "supervisor": project.get("supervisor"),
        "priority": project.get("priority"),
        "migration_exempt": bool(project.get("migration_exempt")),
        "stop_reason_current": project.get("stop_reason_current"),
        "stop_reason_history": project.get("stop_reason_history", []),
        "priority_history": project.get("priority_history", []),
        "stop_reason_orphaned": bool(project.get("stop_reason_orphaned")),
        **counts,
    }


def _all_project_rows() -> List[Dict[str, Any]]:
    cfg = _cfg()
    driver = get_neo4j_driver(cfg)
    hidden = _hidden_sessions()
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
            if str(project.get("supervisor") or "") in hidden:
                continue
            projects.append(_project_row(project))
    return projects


def _pointer(ref: Dict[str, Any]) -> str:
    return f"{ref.get('path', '')}:{ref.get('l_start', '?')}-{ref.get('l_end', '?')}"


def _strip_ref_content(value: Any) -> Any:
    if isinstance(value, list):
        return [_strip_ref_content(item) for item in value]
    if not isinstance(value, dict):
        return value
    scrubbed: Dict[str, Any] = {}
    for key, item in value.items():
        if key in {"content", "resolved_path"}:
            continue
        scrubbed[key] = _strip_ref_content(item)
    if {"path", "l_start", "l_end"}.issubset(scrubbed.keys()):
        scrubbed["pointer"] = _pointer(scrubbed)
    return scrubbed


def _project_visible(project_id: str) -> bool:
    for project in _all_project_rows():
        if project.get("id") == project_id:
            return True
    return False


def _public_summary_or_404(project_id: str) -> Dict[str, Any]:
    summary = get_project_summary(project_id, config=_cfg())
    if not summary:
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    if not _project_visible(project_id):
        raise HTTPException(status_code=404, detail=f"Project {project_id} not found")
    return _strip_ref_content(summary)


def _current_visible(session_id: str) -> Dict[str, Any]:
    _require_visible_session(session_id)
    work = get_session_current_work(session_id, config=_cfg())
    if not work:
        return {"session": session_id, "current": None}
    if not _project_visible(str(work.get("project_id") or "")):
        return {"session": session_id, "current": None}
    return {"session": session_id, "current": _strip_ref_content(work)}


def _next_visible(session_id: str) -> Dict[str, Any]:
    _require_visible_session(session_id)
    work = get_session_next_ready(session_id, config=_cfg())
    if not work:
        return {"session": session_id, "next": None}
    if not _project_visible(str(work.get("project_id") or "")):
        return {"session": session_id, "next": None}
    return {"session": session_id, "next": _strip_ref_content(work)}


def _session_projects_visible(session_id: str) -> Dict[str, Any]:
    _require_visible_session(session_id)
    projects = [_project_row(project) for project in get_session_supervised_projects(session_id, config=_cfg())]
    return {"session": session_id, "projects": [_strip_ref_content(project) for project in projects]}


def _public_index_html() -> str:
    template = _PUBLIC_INDEX.read_text(encoding="utf-8")
    template = template.replace("__PUBLIC_SESSIONS__", json.dumps(_public_sessions()))
    return template


@app.get("/health")
def health() -> Dict[str, Any]:
    try:
        _ = get_ready_tasks(_cfg())
        return {
            "ok": True,
            "service": "fleet-orchestrator-public-readonly",
            "version": package_version(),
            "ts": time.time(),
        }
    except Exception as exc:
        return JSONResponse(status_code=503, content={"ok": False, "error": str(exc)})


@app.get("/api/projects")
def list_projects() -> Dict[str, Any]:
    return {"projects": [_strip_ref_content(project) for project in _all_project_rows()]}


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
