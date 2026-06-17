"""Ship-gate e2e — requested plan ingest creates the forced sub-role gate by default."""
from __future__ import annotations

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet_orchestrator.config import OrchConfig, get_neo4j_driver  # noqa: E402
from fleet_orchestrator.orch_schema import init_schema  # noqa: E402
from fleet_orchestrator.plan_loader import load_plan_from_text  # noqa: E402


CFG = OrchConfig()
PFX = f"tmpl-{uuid.uuid4().hex[:8]}"
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _cleanup() -> None:
    driver = get_neo4j_driver(CFG)
    with driver.session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE (n:OrchProject OR n:OrchPhase OR n:OrchTask) "
                    "AND n.id STARTS WITH $p DETACH DELETE n", p=PFX)


def _plan(project_id: str) -> str:
    return f"""# Project: {project_id} - Template Acceptance [template: forced-subrole-gate]
> verifies forced gate templating

## Phase: build - Build
### Task: scout - Scout [owner: worker-a]
- first work task
### Task: ship - Ship [owner: worker-b] [depends: scout]
- final work task
"""


def _tasks(project_id: str) -> dict[str, dict]:
    driver = get_neo4j_driver(CFG)
    with driver.session(database=CFG.neo4j_db) as session:
        rows = session.run("""
            MATCH (:OrchProject {id: $project_id})-[:HAS_PHASE]->(ph:OrchPhase)-[:HAS_TASK]->(t:OrchTask)
            OPTIONAL MATCH (t)-[:DEPENDS_ON]->(dep:OrchTask)
            RETURN t.id AS id,
                   t.owner AS owner,
                   ph.id AS phase_id,
                   collect(dep.id) AS depends_on
        """, project_id=project_id)
        return {
            row["id"]: {
                "owner": row["owner"],
                "phase_id": row["phase_id"],
                "depends_on": sorted([dep for dep in row["depends_on"] if dep]),
            }
            for row in rows
        }


def _ingest(project_id: str) -> dict:
    return load_plan_from_text(
        _plan(project_id),
        source_path=f"/tmp/{project_id}.md",
        source_kind="markdown",
        ingested_by="template-test",
        supervisor="supervisor",
        priority=10,
        config=CFG,
    )


def main() -> int:
    init_schema(config=CFG)
    _cleanup()
    old_enabled = os.environ.get("ORCH_GATE_TEMPLATE_ENABLED")
    old_gate_owners = os.environ.get("ORCH_GATE_OWNERS")
    try:
        off_project = f"{PFX}-off"
        os.environ["ORCH_GATE_TEMPLATE_ENABLED"] = "0"
        os.environ.pop("ORCH_GATE_OWNERS", None)
        off = _ingest(off_project)
        off_tasks = _tasks(off_project)
        _check("explicitly disabled template leaves requested plan untemplated", off["tasks_created"] == 2 and not any("gate-" in task_id for task_id in off_tasks), off_tasks)

        on_project = f"{PFX}-on"
        os.environ.pop("ORCH_GATE_TEMPLATE_ENABLED", None)
        on = _ingest(on_project)
        tasks = _tasks(on_project)
        gate = lambda bare: f"{on_project}::{bare}"
        work = lambda bare: f"{on_project}::{bare}"

        expected_gate_ids = {
            gate("gate-scout"),
            gate("gate-code"),
            gate("gate-audit"),
            gate("gate-review"),
            gate("gate-approval"),
        }
        _check("requested template defaults on and creates original + five gate tasks", on["tasks_created"] == 7 and expected_gate_ids.issubset(tasks), {"result": on, "tasks": sorted(tasks)})
        _check("gate tasks are in scoped gate phase", all(tasks[task_id]["phase_id"] == gate("forced-subrole-gate") for task_id in expected_gate_ids), tasks)
        _check("unset gate owners are generic stage placeholders", {
            tasks[gate("gate-scout")]["owner"],
            tasks[gate("gate-code")]["owner"],
            tasks[gate("gate-audit")]["owner"],
            tasks[gate("gate-review")]["owner"],
            tasks[gate("gate-approval")]["owner"],
        } == {"scout", "code", "audit", "review", "approval"}, tasks)
        _check("code depends on scout", tasks[gate("gate-code")]["depends_on"] == [gate("gate-scout")], tasks[gate("gate-code")])
        _check("work root depends on code", gate("gate-code") in tasks[work("scout")]["depends_on"], tasks[work("scout")])
        _check("work chain remains intact", work("scout") in tasks[work("ship")]["depends_on"], tasks[work("ship")])
        _check("audit depends on work leaf", work("ship") in tasks[gate("gate-audit")]["depends_on"], tasks[gate("gate-audit")])
        _check("review/approval chain is intact", tasks[gate("gate-review")]["depends_on"] == [gate("gate-audit")] and tasks[gate("gate-approval")]["depends_on"] == [gate("gate-review")], tasks)

        mapped_project = f"{PFX}-mapped"
        os.environ["ORCH_GATE_OWNERS"] = "scout=scout-worker,code=code-worker,audit=audit-worker,review=review-worker,approval=approval-worker"
        mapped = _ingest(mapped_project)
        mapped_tasks = _tasks(mapped_project)
        mapped_gate = lambda bare: f"{mapped_project}::{bare}"
        _check("env gate owners override generic placeholders", {
            mapped_tasks[mapped_gate("gate-scout")]["owner"],
            mapped_tasks[mapped_gate("gate-code")]["owner"],
            mapped_tasks[mapped_gate("gate-audit")]["owner"],
            mapped_tasks[mapped_gate("gate-review")]["owner"],
            mapped_tasks[mapped_gate("gate-approval")]["owner"],
        } == {"scout-worker", "code-worker", "audit-worker", "review-worker", "approval-worker"}, mapped_tasks)
        _check("mapped ingest still creates five gate tasks", mapped["tasks_created"] == 7, mapped)
    finally:
        if old_enabled is None:
            os.environ.pop("ORCH_GATE_TEMPLATE_ENABLED", None)
        else:
            os.environ["ORCH_GATE_TEMPLATE_ENABLED"] = old_enabled
        if old_gate_owners is None:
            os.environ.pop("ORCH_GATE_OWNERS", None)
        else:
            os.environ["ORCH_GATE_OWNERS"] = old_gate_owners
        _cleanup()

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - flagged plan ingest creates forced sub-role gate tasks and dependencies.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
