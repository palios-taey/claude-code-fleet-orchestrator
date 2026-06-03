"""Engine-level Shippability Gate (rp0).

A project is SHIPPABLE only when every ship-gate task is completed with evidence
on record. The ship transition is REFUSED unless the gates are closed — there is
no human-approval override, the process is the authority. Fail-closed: a project
with NO matching gate tasks is NOT shippable, so declaring ship-gates is not
optional — a plan that ships must include them.

WHICH tasks count as ship-gates is CONFIGURABLE per user/deployment, NOT baked
in. Set ``ORCH_SHIP_GATES`` to a comma-separated list of task-id SUFFIXES your
standard requires. The default below ("-prodtest,-audit") is just the reference
operator's standard (real production test + full-code audit) — an EXAMPLE, not a
mandate. e.g. ``ORCH_SHIP_GATES=-ci,-review1,-review2`` for a different shop.
See docs/SHIPPABILITY.md.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from lib.config import OrchConfig
from lib.orch_schema import get_project_summary

DEFAULT_SHIP_GATES = "-prodtest,-audit"  # reference operator's standard (example, not mandated)


def _gate_suffixes() -> Tuple[str, ...]:
    raw = (os.environ.get("ORCH_SHIP_GATES") or DEFAULT_SHIP_GATES).strip()
    return tuple(s.strip() for s in raw.split(",") if s.strip())


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
    suffixes = _gate_suffixes()
    gates = [t for t in tasks if str(t.get("id") or "").endswith(suffixes)]
    if not gates:
        return {"project": project_id, "shippable": False,
                "reason": f"no ship-gate tasks declared (fail-closed); configured gates={list(suffixes)}",
                "gate_tasks": 0, "incomplete_gates": [], "configured_gates": list(suffixes)}
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
