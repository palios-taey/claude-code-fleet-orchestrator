from __future__ import annotations

import copy
from typing import Any, Dict, Iterable, List


GATE_PHASE_ID = "forced-subrole-gate"
GATE_TASKS = [
    ("gate-gemini-scout", "Gemini scout", "gemini", []),
    ("gate-codex-code", "Codex code", "codex", ["gate-gemini-scout"]),
    ("gate-grok-audit", "Grok audit", "grok", ["gate-codex-code"]),
    ("gate-conductor-verify", "Conductor verify", "conductor", ["gate-grok-audit"]),
    ("gate-family", "Family review", "family", ["gate-conductor-verify"]),
]


def apply_gate_template(plan_dict: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(plan_dict, dict):
        raise TypeError("plan_dict must be a dict")

    plan = copy.deepcopy(plan_dict)
    phases = plan.setdefault("phases", [])
    if not isinstance(phases, list):
        raise TypeError("plan_dict['phases'] must be a list")

    existing_tasks = _all_tasks(phases)
    existing_ids = {str(task.get("id")) for task in existing_tasks if task.get("id")}
    root_task_ids = [
        str(task["id"])
        for task in existing_tasks
        if task.get("id") and not task.get("depends")
    ]
    leaf_task_ids = _leaf_task_ids(existing_tasks)

    gate_phase = _ensure_gate_phase(phases)
    gate_phase["tasks"] = _merge_gate_tasks(gate_phase.get("tasks") or [], existing_ids)

    if root_task_ids:
        _add_depends(existing_tasks, root_task_ids, "gate-codex-code")
        _set_depends(gate_phase["tasks"], "gate-grok-audit", leaf_task_ids)

    return plan


def _ensure_gate_phase(phases: List[Dict[str, Any]]) -> Dict[str, Any]:
    for phase in phases:
        if phase.get("id") == GATE_PHASE_ID:
            return phase
    phase = {
        "id": GATE_PHASE_ID,
        "name": "Forced Sub-role Gate",
        "order": -100,
        "refs": [],
        "tasks": [],
    }
    phases.insert(0, phase)
    return phase


def _merge_gate_tasks(existing_gate_tasks: List[Dict[str, Any]], existing_ids: set[str]) -> List[Dict[str, Any]]:
    by_id = {
        str(task.get("id")): task
        for task in existing_gate_tasks
        if isinstance(task, dict) and task.get("id")
    }
    merged = list(existing_gate_tasks)
    for task_id, description, owner, depends in GATE_TASKS:
        if task_id in by_id:
            continue
        if task_id in existing_ids:
            raise ValueError(f"plan already contains non-gate task id {task_id}")
        merged.append({
            "id": task_id,
            "description": description,
            "priority": 50,
            "owner": owner,
            "tags": ["forced-subrole-gate"],
            "depends": list(depends),
            "refs": [],
            "body": [],
        })
    return merged


def _all_tasks(phases: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    tasks: List[Dict[str, Any]] = []
    for phase in phases:
        for task in phase.get("tasks") or []:
            if isinstance(task, dict):
                tasks.append(task)
    return tasks


def _leaf_task_ids(tasks: List[Dict[str, Any]]) -> List[str]:
    task_ids = {str(task.get("id")) for task in tasks if task.get("id")}
    depended_on = set()
    for task in tasks:
        for dep in task.get("depends") or []:
            depended_on.add(str(dep))
    return sorted(task_id for task_id in task_ids if task_id not in depended_on)


def _add_depends(tasks: List[Dict[str, Any]], task_ids: List[str], dependency: str) -> None:
    targets = set(task_ids)
    for task in tasks:
        if str(task.get("id")) not in targets:
            continue
        depends = [str(dep) for dep in task.get("depends") or []]
        if dependency not in depends:
            depends.append(dependency)
        task["depends"] = depends


def _set_depends(tasks: List[Dict[str, Any]], task_id: str, dependencies: List[str]) -> None:
    for task in tasks:
        if task.get("id") != task_id:
            continue
        existing = [str(dep) for dep in task.get("depends") or []]
        for dependency in dependencies:
            if dependency not in existing:
                existing.append(dependency)
        task["depends"] = existing
        return
    raise ValueError(f"gate task missing: {task_id}")
