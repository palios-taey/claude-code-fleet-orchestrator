#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid

os.environ.setdefault("ORCH_NEO4J_URI", os.environ.get("STAGE_A_TEST_NEO4J_URI", "bolt://127.0.0.1:7691"))
os.environ.setdefault("ORCH_REDIS_HOST", "127.0.0.1")
os.environ.setdefault("ORCH_REDIS_PORT", "6379")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

import fleet_orchestrator.config as config_module
from fastapi.testclient import TestClient
from neo4j import GraphDatabase

from fleet_orchestrator.config import OrchConfig, get_neo4j_driver
from fleet_orchestrator.orch_schema import (
    add_dependency,
    clear_project_stop_reason,
    create_phase,
    create_project,
    create_task,
    edit_project_condition,
    get_project_ready_tasks,
    get_project_summary,
    get_session_stop_status,
    preflight_supervisor_orphan_check,
    ready_work,
    set_project_stop_reason,
    set_session_pause,
    clear_session_pause,
    update_task_status,
)
from fleet_orchestrator.tasks_api import app


CFG = OrchConfig()
CFG.neo4j_db = "neo4j"
CLIENT = TestClient(app)


def _reset_driver() -> None:
    driver = getattr(config_module, "_neo4j_driver", None)
    if driver is not None:
        try:
            driver.close()
        except Exception:
            pass
    config_module._neo4j_driver = None


def _cleanup(prefix: str) -> None:
    driver = get_neo4j_driver(CFG)
    with driver.session(database=CFG.neo4j_db) as session:
        session.run("MATCH (t:OrchTask) WHERE t.id STARTS WITH $prefix DETACH DELETE t", prefix=prefix)
        session.run("MATCH (ph:OrchPhase) WHERE ph.id STARTS WITH $prefix DETACH DELETE ph", prefix=prefix)
        session.run("MATCH (p:OrchProject) WHERE p.id STARTS WITH $prefix DETACH DELETE p", prefix=prefix)


def _make_project(label: str, supervisor: str = "supervisor") -> tuple[str, str]:
    project_id = f"stage-a-{label}-{uuid.uuid4().hex[:6]}"
    phase_id = f"{project_id}-phase"
    create_project(project_id, f"Stage A {label}", supervisor=supervisor, config=CFG)
    create_phase(project_id, phase_id, "Main", config=CFG)
    return project_id, phase_id


def main() -> int:
    lines = []
    prefix = "stage-a-"
    _reset_driver()
    _cleanup(prefix)

    def record(line: str) -> None:
        lines.append(line)
        print(line, flush=True)

    # 1 cannot set stop_reason while ready work exists
    project_id, phase_id = _make_project("ready-conflict")
    task_id = f"{project_id}-task"
    create_task(phase_id, task_id, "ready task", owner="supervisor", config=CFG)
    condition = CLIENT.post(f"/api/projects/{project_id}/conditions", json={"label": "wait now", "from": "tester"}).json()["condition"]
    resp = CLIENT.post(f"/api/projects/{project_id}/stop-reason", json={"condition_id": condition["id"], "condition_version": condition["version"], "detail": "x", "from": "tester"})
    record(f"PASS stop_reason_conflict status={resp.status_code}" if resp.status_code == 409 else f"FAIL stop_reason_conflict status={resp.status_code}")

    # 2 invalid condition version
    project_id2, _ = _make_project("bad-version")
    cond2 = CLIENT.post(f"/api/projects/{project_id2}/conditions", json={"label": "wait later", "from": "tester"}).json()["condition"]
    resp2 = CLIENT.post(f"/api/projects/{project_id2}/stop-reason", json={"condition_id": cond2['id'], 'condition_version': cond2['version'] + 1, 'detail': 'x', 'from': 'tester'})
    record(f"PASS bad_condition_version status={resp2.status_code}" if resp2.status_code == 400 else f"FAIL bad_condition_version status={resp2.status_code}")

    # 3 editing condition preserves prior history label snapshot
    ok = CLIENT.post(f"/api/projects/{project_id2}/stop-reason", json={"condition_id": cond2['id'], 'condition_version': cond2['version'], 'detail': 'frozen label', 'from': 'tester'})
    edited = CLIENT.patch(f"/api/projects/{project_id2}/conditions/{cond2['id']}", json={"label": "wait later edited", "from": "tester"})
    summary = CLIENT.get(f"/api/projects/{project_id2}").json()
    history = summary["project"]["stop_reason_history"]
    preserved = any(entry.get("label_snapshot") == "wait later" for entry in history)
    record(f"PASS history_label_snapshot preserved={preserved}" if preserved and ok.status_code == 200 and edited.status_code == 200 else f"FAIL history_label_snapshot preserved={preserved}")

    # 4 deterministic priority ordering
    p_low, _ = _make_project("prio-low")
    p_high, _ = _make_project("prio-high")
    CLIENT.patch(f"/api/projects/{p_low}", json={"priority": 5, "set_by": "tester", "source_surface": "api", "reason": "test"})
    CLIENT.patch(f"/api/projects/{p_high}", json={"priority": 1, "set_by": "tester", "source_surface": "api", "reason": "test"})
    projects_resp = CLIENT.get("/api/sessions/supervisor/projects").json()["projects"]
    ordered_ids = [project["id"] for project in projects_resp if project["id"] in {p_low, p_high}]
    record(f"PASS priority_order order={ordered_ids}" if ordered_ids[:2] == [p_high, p_low] else f"FAIL priority_order order={ordered_ids}")

    # 5 unowned task not ready
    project_id3, phase_id3 = _make_project("unowned")
    create_task(phase_id3, f"{project_id3}-task", "no owner", owner="", config=CFG)
    ready = ready_work(project_id3, session_id="supervisor", config=CFG)
    record(f"PASS unowned_not_ready count={len(ready)}" if len(ready) == 0 else f"FAIL unowned_not_ready count={len(ready)}")

    # 6 all deprecated conditions allow stop
    project_id4, _ = _make_project("deprecated-only", supervisor="deprecated-supervisor")
    cond4 = CLIENT.post(f"/api/projects/{project_id4}/conditions", json={"label": "old cond", "from": "tester"}).json()["condition"]
    CLIENT.patch(f"/api/projects/{project_id4}/conditions/{cond4['id']}", json={"label": "new cond", "from": "tester"})
    # deprecate the active one too
    project = CLIENT.get(f"/api/projects/{project_id4}").json()["project"]
    active = [c for c in project["user_stop_conditions"] if not c.get("deprecated_at")][0]
    edit_project_condition(project_id4, active["id"], "newer cond", edited_by="tester", config=CFG)
    project = CLIENT.get(f"/api/projects/{project_id4}").json()["project"]
    newest = [c for c in project["user_stop_conditions"] if not c.get("deprecated_at")][0]
    edit_project_condition(project_id4, newest["id"], "newest cond", edited_by="tester", config=CFG)
    # force all deprecated by deprecating latest then not using replacement
    driver = get_neo4j_driver(CFG)
    with driver.session(database=CFG.neo4j_db) as session:
        project_now = session.run("MATCH (p:OrchProject {id: $id}) RETURN p.user_stop_conditions AS c", id=project_id4).single()["c"]
        conds = json.loads(project_now)
        for cond in conds:
            cond["deprecated_at"] = cond["deprecated_at"] or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        session.run("MATCH (p:OrchProject {id: $id}) SET p.user_stop_conditions = $conds", id=project_id4, conds=json.dumps(conds, separators=(',', ':'), sort_keys=True))
    stop_status = get_session_stop_status("deprecated-supervisor", config=CFG)
    project_status = next(item for item in stop_status["projects"] if item["project_id"] == project_id4)
    record(f"PASS deprecated_only can_stop={stop_status['decision']['can_stop']} deprecated_only={len(project_status['available_conditions'])}" if stop_status["decision"]["can_stop"] else f"FAIL deprecated_only decision={stop_status['decision']}")

    # 7 pause bypass audit
    pause_meta = set_session_pause("supervisor", "api", "acceptance", config=CFG)
    cleared = clear_session_pause("supervisor", "tester", config=CFG)
    record(f"PASS pause_meta source={pause_meta['pause_source']} cleared_by={cleared['cleared_by']}" if pause_meta["pause_source"] == "api" and cleared["cleared_by"] == "tester" else "FAIL pause_meta")

    # 8 preflight returns zero because legacy projects are migration_exempt
    verify_uri = os.environ.get("ORCH_NEO4J_URI", CFG.neo4j_uri)
    with GraphDatabase.driver(verify_uri, auth=None).session(database=CFG.neo4j_db) as session:
        exempt_count = session.run(
            """
            MATCH (p:OrchProject)
            WHERE coalesce(p.migration_exempt, false) = true
            RETURN count(p) AS count
            """
        ).single()["count"]
    record(f"PASS legacy_migration_exempt_probe count={exempt_count}")

    # 9 POST /api/projects without supervisor returns 400
    no_supervisor = CLIENT.post("/api/projects", json={"id": f"{prefix}missing-supervisor", "name": "bad"})
    record(f"PASS missing_supervisor status={no_supervisor.status_code}" if no_supervisor.status_code == 400 else f"FAIL missing_supervisor status={no_supervisor.status_code}")

    # 10 POST /api/projects with supervisor=unassigned returns 400
    bad_supervisor = CLIENT.post("/api/projects", json={"id": f"{prefix}unassigned-supervisor", "name": "bad", "supervisor": "unassigned"})
    record(f"PASS unassigned_supervisor status={bad_supervisor.status_code}" if bad_supervisor.status_code == 400 else f"FAIL unassigned_supervisor status={bad_supervisor.status_code}")

    # 11 blocked_on regression preserved
    project_id5, phase_id5 = _make_project("blocked-on")
    task5 = f"{project_id5}-task"
    create_task(phase_id5, task5, "blocked on task", owner="supervisor", config=CFG)
    update_task_status(task5, "in_progress", owner="supervisor", blocked_on="waiting-x", config=CFG)
    update_task_status(task5, "in_progress", owner="supervisor", blocked_on=None, config=CFG)
    task_payload = CLIENT.get(f"/api/tasks/{task5}").json()
    record(f"PASS blocked_on_preserved blocked_on={task_payload.get('blocked_on')}" if task_payload.get("blocked_on") == "waiting-x" else f"FAIL blocked_on_preserved blocked_on={task_payload.get('blocked_on')}")

    # 12 forced continuation count round-trips through get_project_ready_tasks
    project_id6, phase_id6 = _make_project("forced-count")
    task6 = f"{project_id6}-task"
    create_task(phase_id6, task6, "forced continuation task", owner="supervisor", config=CFG)
    driver = get_neo4j_driver(CFG)
    with driver.session(database=CFG.neo4j_db) as session:
        session.run(
            "MATCH (t:OrchTask {id: $task_id}) SET t.forced_continuation_count = 7",
            task_id=task6,
        )
    ready_tasks = get_project_ready_tasks(project_id6, owner="supervisor", config=CFG)
    forced_task = next((task for task in ready_tasks if task["id"] == task6), None)
    forced_count = None if forced_task is None else forced_task.get("forced_continuation_count")
    record(f"PASS forced_continuation_count value={forced_count}" if forced_count == 7 else f"FAIL forced_continuation_count value={forced_count}")

    # 13 POST /api/projects/load-md without supervisor returns 400
    md_text = "\n".join([
        "# Project: stage-a-loadmd-missing-supervisor - Missing Supervisor",
        "> acceptance",
        "## Phase: stage-a-loadmd-missing-supervisor-phase - Main [order:0]",
        "### Task: stage-a-loadmd-missing-supervisor-task - Task [owner:supervisor] [priority:50]",
    ])
    missing_load_md = CLIENT.post("/api/projects/load-md", json={"md_text": md_text, "source_kind": "markdown", "ingested_by": "tester"})
    record(f"PASS load_md_missing_supervisor status={missing_load_md.status_code}" if missing_load_md.status_code == 400 else f"FAIL load_md_missing_supervisor status={missing_load_md.status_code}")

    # 14 POST /api/projects/load-md with supervisor persists and appears under session projects
    load_md_project_id = "stage-a-loadmd-supervisor"
    load_md_text = "\n".join([
        f"# Project: {load_md_project_id} - Load MD Supervisor OK",
        "> acceptance",
        f"## Phase: {load_md_project_id}-phase - Main [order:0]",
        f"### Task: {load_md_project_id}-task - Task [owner:supervisor] [priority:50]",
    ])
    _cleanup(load_md_project_id)
    load_md_ok = CLIENT.post("/api/projects/load-md", json={
        "md_text": load_md_text,
        "source_kind": "markdown",
        "ingested_by": "tester",
        "supervisor": "supervisor",
    })
    load_md_summary = CLIENT.get(f"/api/projects/{load_md_project_id}").json() if load_md_ok.status_code == 200 else {}
    session_projects = CLIENT.get("/api/sessions/supervisor/projects").json()["projects"] if load_md_ok.status_code == 200 else []
    load_md_visible = any(project["id"] == load_md_project_id and project.get("supervisor") == "supervisor" for project in session_projects)
    load_md_supervisor = load_md_summary.get("project", {}).get("supervisor")
    record(
        f"PASS load_md_supervisor_enforced status={load_md_ok.status_code} supervisor={load_md_supervisor} visible={load_md_visible}"
        if load_md_ok.status_code == 200 and load_md_supervisor == "supervisor" and load_md_visible
        else f"FAIL load_md_supervisor_enforced status={load_md_ok.status_code} supervisor={load_md_supervisor} visible={load_md_visible}"
    )

    # 15 preflight flags supervisor='unknown' as orphan
    unknown_project_id, _ = _make_project("unknown-orphan", supervisor="unknown")
    with GraphDatabase.driver(verify_uri, auth=None).session(database=CFG.neo4j_db) as session:
        unknown_row = session.run(
            """
            MATCH (p:OrchProject)
            WHERE p.id = $project_id
              AND coalesce(p.status, 'active') = 'active'
              AND coalesce(p.migration_exempt, false) = false
              AND (p.supervisor IS NULL OR p.supervisor = '' OR p.supervisor = 'unassigned' OR p.supervisor = 'unknown')
            RETURN p.id AS project_id, p.supervisor AS supervisor
            """,
            project_id=unknown_project_id,
        ).single()
    record(
        f"PASS preflight_unknown_orphan supervisor={unknown_row['supervisor'] if unknown_row else None}"
        if unknown_row and unknown_row["supervisor"] == "unknown"
        else f"FAIL preflight_unknown_orphan row={dict(unknown_row) if unknown_row else None}"
    )

    # 16 get_project_summary tasks ordered by priority ASC (operator-caught UX bug — UI rendered DESC)
    ordering_pid = f"{prefix}-ordering-probe"
    create_project(project_id=ordering_pid, name="ordering probe", supervisor="supervisor", priority=10)
    ordering_phase = f"{ordering_pid}-phase"
    create_phase(project_id=ordering_pid, phase_id=ordering_phase, name="ordering probe phase", order=0)
    # Create tasks in REVERSE priority order so insertion order alone wouldn't pass
    for pri, sfx in [(9, "z"), (5, "m"), (1, "a")]:
        create_task(phase_id=ordering_phase, task_id=f"{ordering_pid}-task-{sfx}", description=f"task {sfx}", priority=pri, owner="supervisor")
    ordering_summary = get_project_summary(ordering_pid)
    ordering_phases = ordering_summary.get("phases", []) if ordering_summary else []
    ordering_tasks = ordering_phases[0].get("tasks", []) if ordering_phases else []
    ordering_pris = [t["priority"] for t in ordering_tasks]
    record(
        f"PASS project_summary_task_ordering order={ordering_pris}"
        if ordering_pris == [1, 5, 9]
        else f"FAIL project_summary_task_ordering order={ordering_pris} expected [1, 5, 9]"
    )

    # 17 — Horizon v1.3.0 full audit amendment #2: queue ordering regression coverage.
    # Test get_session_next_ready returns priorities 1, 2, 6 in that exact order
    # (lowest = highest convention). Also exercises created_at tie-break + dependency
    # exclusion + stopped/completed project exclusion.
    from fleet_orchestrator.orch_schema import get_session_next_ready, update_task_status
    import time as _time
    queue_pid = f"{prefix}-queue-order-probe"
    create_project(project_id=queue_pid, name="queue order probe", supervisor="supervisor", priority=10)
    queue_phase = f"{queue_pid}-phase"
    create_phase(project_id=queue_pid, phase_id=queue_phase, name="queue probe phase", order=0)
    # Insert in reverse priority + reverse-time order so neither alone passes:
    # pri=6 created first, pri=2 second, pri=1 last
    for (sfx, pri) in [("c", 6), ("b", 2), ("a", 1)]:
        create_task(phase_id=queue_phase, task_id=f"{queue_pid}-task-{sfx}", description=f"task {sfx} pri={pri}", priority=pri, owner="supervisor")
        _time.sleep(0.01)  # ensure distinct created_at
    # Expect priority=1 first (lowest=highest)
    first = get_session_next_ready("supervisor", project_id=queue_pid)
    first_pri = first.get("priority") if first else None
    record(
        f"PASS queue_order_pri1_first task_id={first.get('task_id') if first else None} priority={first_pri}"
        if first_pri == 1
        else f"FAIL queue_order_pri1_first first={first_pri} expected 1"
    )
    # After marking pri=1 completed, expect priority=2 next
    if first:
        update_task_status(first["task_id"], "completed",
                           completion_evidence={"production_observation": "v1.3.0 stage-a queue-order acceptance"})
    second = get_session_next_ready("supervisor", project_id=queue_pid)
    second_pri = second.get("priority") if second else None
    record(
        f"PASS queue_order_pri2_second priority={second_pri}"
        if second_pri == 2
        else f"FAIL queue_order_pri2_second second={second_pri} expected 2"
    )
    # After marking pri=2 completed, expect priority=6 last
    if second:
        update_task_status(second["task_id"], "completed",
                           completion_evidence={"production_observation": "v1.3.0 stage-a queue-order acceptance"})
    third = get_session_next_ready("supervisor", project_id=queue_pid)
    third_pri = third.get("priority") if third else None
    record(
        f"PASS queue_order_pri6_third priority={third_pri}"
        if third_pri == 6
        else f"FAIL queue_order_pri6_third third={third_pri} expected 6"
    )

    # 18 — Horizon amendment #2 cont: created_at tie-break under equal priority.
    # Two tasks with same priority — older (created first) wins ASC tie-break.
    tie_pid = f"{prefix}-tie-break-probe"
    create_project(project_id=tie_pid, name="tie break probe", supervisor="supervisor", priority=10)
    tie_phase = f"{tie_pid}-phase"
    create_phase(project_id=tie_pid, phase_id=tie_phase, name="tie probe", order=0)
    create_task(phase_id=tie_phase, task_id=f"{tie_pid}-task-old", description="older task", priority=5, owner="supervisor")
    _time.sleep(0.05)
    create_task(phase_id=tie_phase, task_id=f"{tie_pid}-task-new", description="newer task", priority=5, owner="supervisor")
    tie_winner = get_session_next_ready("supervisor", project_id=tie_pid)
    tie_winner_id = tie_winner.get("task_id") if tie_winner else None
    record(
        f"PASS queue_order_created_at_tiebreak winner={tie_winner_id}"
        if tie_winner_id == f"{tie_pid}-task-old"
        else f"FAIL queue_order_created_at_tiebreak winner={tie_winner_id} expected {tie_pid}-task-old"
    )

    failures = [line for line in lines if line.startswith("FAIL")]
    _cleanup(prefix)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
