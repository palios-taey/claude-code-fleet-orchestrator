"""CLI rendering/checks for task executor bindings."""
from __future__ import annotations

from typing import Any, Callable, Dict, Iterable, List

ApiCall = Callable[[str, str, Dict[str, Any] | None], Dict[str, Any]]


def _dedupe(values: Iterable[Any]) -> List[str]:
    seen: set[str] = set()
    result: List[str] = []
    for value in values:
        item = str(value or "").strip()
        if not item or item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result


def _candidate_sessions(task: Dict[str, Any]) -> List[str]:
    dispatched_to = str(task.get("dispatched_to") or "").strip()
    if dispatched_to:
        return [dispatched_to]
    return _dedupe((task.get("owner"),))


def executor_bindings(task: Dict[str, Any],
                      api_call: ApiCall) -> List[Dict[str, Any]]:
    task_id = str(task.get("id") or task.get("task_id") or "").strip()
    bindings: List[Dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for session in _candidate_sessions(task):
        result = api_call("GET", f"/api/sessions/{session}/current", None)
        current = result.get("current") or {}
        if str(current.get("top_task_id") or current.get("task_id") or "").strip() != task_id:
            continue
        liveness = result.get("liveness") or current.get("liveness") or {}
        key = (session, str(liveness.get("worker") or ""))
        if key in seen:
            continue
        seen.add(key)
        bindings.append({
            "session": session,
            "worker": str(liveness.get("worker") or session),
            "state": _binding_state(liveness),
            "summary": str(liveness.get("summary") or "").strip(),
        })
    return bindings


def _binding_state(liveness: Dict[str, Any]) -> str:
    if liveness.get("idle") is True:
        return "IDLE"
    return str(liveness.get("label") or liveness.get("state") or "WORKING").replace("_", " ").upper()


def format_binding(binding: Dict[str, Any]) -> str:
    session = str(binding.get("session") or "?")
    state = str(binding.get("state") or "WORKING")
    summary = str(binding.get("summary") or "").strip()
    suffix = f" ({summary})" if summary else ""
    worker = str(binding.get("worker") or "").strip()
    worker_suffix = f" via {worker}" if worker and worker != session else ""
    return f"{session}: {state}{suffix}{worker_suffix}"


def conflicting_binding(task: Dict[str, Any],
                        target_session: str,
                        api_call: ApiCall) -> Dict[str, Any] | None:
    target = str(target_session or "").strip()
    for binding in executor_bindings(task, api_call):
        session = str(binding.get("session") or "").strip()
        worker = str(binding.get("worker") or "").strip()
        if target not in {session, worker}:
            return binding
    return None
