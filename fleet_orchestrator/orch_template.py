from __future__ import annotations

import copy
import json
import os
from typing import Any, Dict, Iterable, List


GATE_PHASE_ID = "forced-subrole-gate"
GATE_TASKS = [
    ("gate-scout", "Scout", "scout", []),
    ("gate-code", "Code", "code", ["gate-scout"]),
    ("gate-audit", "Audit", "audit", ["gate-code"]),
    ("gate-review", "Review", "review", ["gate-audit"]),
    ("gate-approval", "Final approval", "approval", ["gate-review"]),
]
GATE_IDS = {tid for tid, _, _, _ in GATE_TASKS}


def _is_gate_task(task: Dict[str, Any]) -> bool:
    return (str(task.get("id")) in GATE_IDS
            or "forced-subrole-gate" in (task.get("tags") or []))


def apply_gate_template(plan_dict: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(plan_dict, dict):
        raise TypeError("plan_dict must be a dict")

    plan = copy.deepcopy(plan_dict)
    phases = plan.setdefault("phases", [])
    if not isinstance(phases, list):
        raise TypeError("plan_dict['phases'] must be a list")

    existing_tasks = _all_tasks(phases)
    existing_ids = {str(task.get("id")) for task in existing_tasks if task.get("id")}
    # Idempotency fix: compute roots/leaves over WORK tasks only (exclude the gate
    # scaffold). Re-applying must not treat gate-scout (depends:[]) as a work
    # root and add gate-code as its dependency -> that created a dep cycle.
    work_tasks = [t for t in existing_tasks if t.get("id") and not _is_gate_task(t)]
    work_ids = {str(t["id"]) for t in work_tasks}
    roots = _work_roots(work_tasks, work_ids)
    leaves = _work_leaves(work_tasks, work_ids)

    gate_phase = _ensure_gate_phase(phases)
    gate_phase["tasks"] = _merge_gate_tasks(gate_phase.get("tasks") or [], existing_ids)

    # Fail-OPEN fix: ALWAYS wire the gate around the work (no `if roots:` guard that
    # silently left the gate disconnected). Fail-safe fallback: if there is no clean
    # entry/exit (cyclic work graph), gate ALL work tasks rather than none.
    if work_ids:
        entry_targets = roots or sorted(work_ids)
        exit_targets = leaves or sorted(work_ids)
        _add_depends(work_tasks, entry_targets, "gate-code")
        _set_depends(gate_phase["tasks"], "gate-audit", exit_targets)

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
    owners = _gate_owner_mapping()
    for task_id, description, stage_key, depends in GATE_TASKS:
        if task_id in by_id:
            continue
        if task_id in existing_ids:
            raise ValueError(f"plan already contains non-gate task id {task_id}")
        merged.append({
            "id": task_id,
            "description": description,
            "priority": 50,
            "owner": owners.get(stage_key, stage_key),
            "tags": ["forced-subrole-gate"],
            "depends": list(depends),
            "refs": [],
            "body": [],
        })
    return merged


def _gate_owner_mapping() -> Dict[str, str]:
    raw = os.environ.get("ORCH_GATE_OWNERS", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return {
                str(key).strip(): str(value).strip()
                for key, value in parsed.items()
                if str(key).strip() and str(value).strip()
            }
    except (TypeError, ValueError):
        pass
    owners: Dict[str, str] = {}
    for pair in raw.split(","):
        if "=" not in pair:
            continue
        key, _, value = pair.partition("=")
        key = key.strip()
        value = value.strip()
        if key and value:
            owners[key] = value
    return owners


def _all_tasks(phases: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    tasks: List[Dict[str, Any]] = []
    for phase in phases:
        for task in phase.get("tasks") or []:
            if isinstance(task, dict):
                tasks.append(task)
    return tasks


def _work_roots(work_tasks: List[Dict[str, Any]], work_ids: set) -> List[str]:
    """Work tasks that are entry points — none of their deps is another WORK task
    (gate/external deps are ignored, so this stays correct on re-apply)."""
    roots = []
    for task in work_tasks:
        deps = {str(d) for d in task.get("depends") or []}
        if not (deps & work_ids):
            roots.append(str(task["id"]))
    return sorted(roots)


def _work_leaves(work_tasks: List[Dict[str, Any]], work_ids: set) -> List[str]:
    """Work tasks not depended on by any other WORK task (exit points)."""
    depended_on = set()
    for task in work_tasks:
        for dep in task.get("depends") or []:
            if str(dep) in work_ids:
                depended_on.add(str(dep))
    return sorted(work_ids - depended_on)


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
