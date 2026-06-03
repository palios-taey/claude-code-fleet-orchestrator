"""Engine-level Shippability Gate (rp0).

A project is SHIPPABLE only when every ship-gate task (its `-prodtest` and
`-audit` gates) is completed with evidence on record. This is the engine
enforcement behind docs/SHIPPABILITY.md: the ship transition is REFUSED unless
the gates are closed — there is no human-approval override, the process is the
authority. Fail-closed: a project with no gate tasks is NOT shippable.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from lib.config import OrchConfig
from lib.orch_schema import get_project_summary

GATE_SUFFIXES = ("-prodtest", "-audit")


def _all_tasks(summary: Dict[str, Any]) -> List[Dict[str, Any]]:
    return [t for ph in (summary.get("phases") or []) for t in (ph.get("tasks") or [])]


def evaluate_shippability(project_id: str, config: Optional[OrchConfig] = None) -> Dict[str, Any]:
    """Return a structured shippability verdict. shippable=True ONLY when every
    -prodtest and -audit gate task is completed."""
    summary = get_project_summary(project_id, config)
    if not summary:
        return {"project": project_id, "shippable": False, "reason": "project not found",
                "gate_tasks": 0, "incomplete_gates": []}
    tasks = _all_tasks(summary)
    gates = [t for t in tasks if str(t.get("id") or "").endswith(GATE_SUFFIXES)]
    if not gates:
        return {"project": project_id, "shippable": False,
                "reason": "no ship-gate tasks declared (fail-closed)",
                "gate_tasks": 0, "incomplete_gates": []}
    incomplete = [
        {"id": t.get("id"), "status": t.get("status"), "blocked_on": t.get("blocked_on")}
        for t in gates if t.get("status") != "completed"
    ]
    shippable = not incomplete
    return {
        "project": project_id,
        "shippable": shippable,
        "gate_tasks": len(gates),
        "completed_gates": len(gates) - len(incomplete),
        "incomplete_gates": incomplete,
        "reason": "all ship-gates completed with evidence" if shippable
                  else f"{len(incomplete)}/{len(gates)} ship-gates not completed",
    }
