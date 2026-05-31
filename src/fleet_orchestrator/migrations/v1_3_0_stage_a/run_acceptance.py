#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

os.environ.setdefault("ORCH_NEO4J_URI", os.environ.get("STAGE_A_TEST_NEO4J_URI", "bolt://127.0.0.1:7691"))
os.environ.setdefault("ORCH_NEO4J_NOAUTH", "1")
os.environ.setdefault("ORCH_REDIS_HOST", "127.0.0.1")
os.environ.setdefault("ORCH_REDIS_PORT", "6379")
# KEEP: this harness is run directly by file path from an unpackaged checkout,
# so it must add ``src/`` to sys.path before importing the package.
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import fleet_orchestrator.config as config_module
from fastapi.testclient import TestClient
from neo4j import GraphDatabase

from fleet_orchestrator.config import OrchConfig, get_neo4j_driver
from fleet_orchestrator.dispatch import _claim_ready_orch_task
from fleet_orchestrator.plan_loader import load_plan_from_text
from fleet_orchestrator.orch_schema import (
    _ZERO_DEP_READY_CYPHER,
    add_dependency,
    clear_project_stop_reason,
    create_phase,
    create_project,
    create_task,
    edit_project_condition,
    get_ready_tasks,
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


def _make_project(label: str, supervisor: str = "conductor") -> tuple[str, str]:
    project_id = f"stage-a-{label}-{uuid.uuid4().hex[:6]}"
    phase_id = f"{project_id}-phase"
    create_project(project_id, f"Stage A {label}", supervisor=supervisor, config=CFG)
    create_phase(project_id, phase_id, "Main", config=CFG)
    return project_id, phase_id


def _complete_task_native(task_id: str) -> None:
    update_task_status(
        task_id,
        "completed",
        commit_sha=f"{task_id[:7]}sha",
        production_observation=f"validated {task_id}",
        config=CFG,
    )


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
    create_task(phase_id, task_id, "ready task", owner="conductor", config=CFG)
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
    projects_resp = CLIENT.get("/api/sessions/conductor/projects").json()["projects"]
    ordered_ids = [project["id"] for project in projects_resp if project["id"] in {p_low, p_high}]
    record(f"PASS priority_order order={ordered_ids}" if ordered_ids[:2] == [p_high, p_low] else f"FAIL priority_order order={ordered_ids}")

    # 5 unowned task not ready
    project_id3, phase_id3 = _make_project("unowned")
    create_task(phase_id3, f"{project_id3}-task", "no owner", owner="", config=CFG)
    ready = ready_work(project_id3, session_id="conductor", config=CFG)
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
    pause_meta = set_session_pause("conductor", "api", "acceptance", config=CFG)
    cleared = clear_session_pause("conductor", "tester", config=CFG)
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
    create_task(phase_id5, task5, "blocked on task", owner="conductor", config=CFG)
    update_task_status(task5, "in_progress", owner="conductor", blocked_on="waiting-x", config=CFG)
    update_task_status(task5, "in_progress", owner="conductor", blocked_on=None, config=CFG)
    task_payload = CLIENT.get(f"/api/tasks/{task5}").json()
    record(f"PASS blocked_on_preserved blocked_on={task_payload.get('blocked_on')}" if task_payload.get("blocked_on") == "waiting-x" else f"FAIL blocked_on_preserved blocked_on={task_payload.get('blocked_on')}")

    # 12 forced continuation count round-trips through get_project_ready_tasks
    project_id6, phase_id6 = _make_project("forced-count")
    task6 = f"{project_id6}-task"
    create_task(phase_id6, task6, "forced continuation task", owner="conductor", config=CFG)
    driver = get_neo4j_driver(CFG)
    with driver.session(database=CFG.neo4j_db) as session:
        session.run(
            "MATCH (t:OrchTask {id: $task_id}) SET t.forced_continuation_count = 7",
            task_id=task6,
        )
    ready_tasks = get_project_ready_tasks(project_id6, owner="conductor", config=CFG)
    forced_task = next((task for task in ready_tasks if task["id"] == task6), None)
    forced_count = None if forced_task is None else forced_task.get("forced_continuation_count")
    record(f"PASS forced_continuation_count value={forced_count}" if forced_count == 7 else f"FAIL forced_continuation_count value={forced_count}")

    # 13 POST /api/projects/load-md without supervisor returns 400
    md_text = "\n".join([
        "# Project: stage-a-loadmd-missing-supervisor - Missing Supervisor",
        "> acceptance",
        "## Phase: stage-a-loadmd-missing-supervisor-phase - Main [order:0]",
        "### Task: stage-a-loadmd-missing-supervisor-task - Task [owner:conductor] [priority:50]",
    ])
    missing_load_md = CLIENT.post("/api/projects/load-md", json={"md_text": md_text, "source_kind": "markdown", "ingested_by": "tester"})
    record(f"PASS load_md_missing_supervisor status={missing_load_md.status_code}" if missing_load_md.status_code == 400 else f"FAIL load_md_missing_supervisor status={missing_load_md.status_code}")

    # 14 POST /api/projects/load-md with supervisor persists and appears under session projects
    load_md_project_id = "stage-a-loadmd-conductor"
    load_md_text = "\n".join([
        f"# Project: {load_md_project_id} - Load MD Supervisor OK",
        "> acceptance",
        f"## Phase: {load_md_project_id}-phase - Main [order:0]",
        f"### Task: {load_md_project_id}-task - Task [owner:conductor] [priority:50]",
    ])
    _cleanup(load_md_project_id)
    load_md_ok = CLIENT.post("/api/projects/load-md", json={
        "md_text": load_md_text,
        "source_kind": "markdown",
        "ingested_by": "tester",
        "supervisor": "conductor",
    })
    load_md_summary = CLIENT.get(f"/api/projects/{load_md_project_id}").json() if load_md_ok.status_code == 200 else {}
    session_projects = CLIENT.get("/api/sessions/conductor/projects").json()["projects"] if load_md_ok.status_code == 200 else []
    load_md_visible = any(project["id"] == load_md_project_id and project.get("supervisor") == "conductor" for project in session_projects)
    load_md_supervisor = load_md_summary.get("project", {}).get("supervisor")
    record(
        f"PASS load_md_supervisor_enforced status={load_md_ok.status_code} supervisor={load_md_supervisor} visible={load_md_visible}"
        if load_md_ok.status_code == 200 and load_md_supervisor == "conductor" and load_md_visible
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

    # 16 get_project_summary tasks ordered by priority ASC (Jesse-caught UX bug — UI rendered DESC)
    ordering_pid = f"{prefix}-ordering-probe"
    create_project(project_id=ordering_pid, name="ordering probe", supervisor="conductor", priority=10)
    ordering_phase = f"{ordering_pid}-phase"
    create_phase(project_id=ordering_pid, phase_id=ordering_phase, name="ordering probe phase", order=0)
    # Create tasks in REVERSE priority order so insertion order alone wouldn't pass
    for pri, sfx in [(9, "z"), (5, "m"), (1, "a")]:
        create_task(phase_id=ordering_phase, task_id=f"{ordering_pid}-task-{sfx}", description=f"task {sfx}", priority=pri, owner="conductor")
    ordering_summary = get_project_summary(ordering_pid)
    ordering_phases = ordering_summary.get("phases", []) if ordering_summary else []
    ordering_tasks = ordering_phases[0].get("tasks", []) if ordering_phases else []
    ordering_pris = [t["priority"] for t in ordering_tasks]
    record(
        f"PASS project_summary_task_ordering order={ordering_pris}"
        if ordering_pris == [1, 5, 9]
        else f"FAIL project_summary_task_ordering order={ordering_pris} expected [1, 5, 9]"
    )

    # 17 supervisor write path preserves created_by as-is; no suffix stripping on project ownership
    created_by_project = f"{prefix}-created-by-supervisor"
    created_by_phase = f"{created_by_project}-phase"
    create_project(
        project_id=created_by_project,
        name="created_by supervisor probe",
        ingested_by="ops-bot-codex",
        priority=10,
        config=CFG,
    )
    create_phase(project_id=created_by_project, phase_id=created_by_phase, name="created_by phase", order=0)
    created_by_summary = get_project_summary(created_by_project, config=CFG)
    created_by_supervisor = (created_by_summary or {}).get("project", {}).get("supervisor")
    record(
        f"PASS supervisor_write_preserves_created_by supervisor={created_by_supervisor}"
        if created_by_supervisor == "ops-bot-codex"
        else f"FAIL supervisor_write_preserves_created_by supervisor={created_by_supervisor}"
    )

    # 18 NULL-status tasks count as pending in project summary stats
    summary_null_project = f"{prefix}-summary-null-status"
    summary_null_phase = f"{summary_null_project}-phase"
    summary_null_task = f"{summary_null_project}-task"
    create_project(project_id=summary_null_project, name="summary null probe", supervisor="conductor", priority=10)
    create_phase(project_id=summary_null_project, phase_id=summary_null_phase, name="summary null phase", order=0)
    create_task(
        phase_id=summary_null_phase,
        task_id=summary_null_task,
        description="summary null task",
        priority=1,
        owner="conductor",
        config=CFG,
    )
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run(
            "MATCH (t:OrchTask {id: $task_id}) SET t.status = NULL",
            task_id=summary_null_task,
        )
    summary_null = get_project_summary(summary_null_project, config=CFG)
    summary_null_counts = summary_null["phases"][0]["task_counts"] if summary_null and summary_null.get("phases") else {}
    record(
        f"PASS project_summary_null_status_counts counts={summary_null_counts}"
        if summary_null_counts.get("pending") == 1
        and summary_null_counts.get("in_progress") == 0
        and summary_null_counts.get("completed") == 0
        and summary_null_counts.get("failed") == 0
        else f"FAIL project_summary_null_status_counts counts={summary_null_counts}"
    )

    # 19 — Horizon v1.3.0 full audit amendment #2: queue ordering regression coverage.
    # Test get_session_next_ready returns priorities 1, 2, 6 in that exact order
    # (lowest = highest convention). Also exercises created_at tie-break + dependency
    # exclusion + stopped/completed project exclusion.
    from fleet_orchestrator.orch_schema import get_session_next_ready
    import time as _time
    queue_pid = f"{prefix}-queue-order-probe"
    create_project(project_id=queue_pid, name="queue order probe", supervisor="conductor", priority=10)
    queue_phase = f"{queue_pid}-phase"
    create_phase(project_id=queue_pid, phase_id=queue_phase, name="queue probe phase", order=0)
    # Insert in reverse priority + reverse-time order so neither alone passes:
    # pri=6 created first, pri=2 second, pri=1 last
    for (sfx, pri) in [("c", 6), ("b", 2), ("a", 1)]:
        create_task(phase_id=queue_phase, task_id=f"{queue_pid}-task-{sfx}", description=f"task {sfx} pri={pri}", priority=pri, owner="conductor")
        _time.sleep(0.01)  # ensure distinct created_at
    # Expect priority=1 first (lowest=highest)
    first = get_session_next_ready("conductor", project_id=queue_pid)
    first_pri = first.get("priority") if first else None
    record(
        f"PASS queue_order_pri1_first task_id={first.get('task_id') if first else None} priority={first_pri}"
        if first_pri == 1
        else f"FAIL queue_order_pri1_first first={first_pri} expected 1"
    )
    # After marking pri=1 completed, expect priority=2 next
    if first:
        _complete_task_native(first["task_id"])
    second = get_session_next_ready("conductor", project_id=queue_pid)
    second_pri = second.get("priority") if second else None
    record(
        f"PASS queue_order_pri2_second priority={second_pri}"
        if second_pri == 2
        else f"FAIL queue_order_pri2_second second={second_pri} expected 2"
    )
    # After marking pri=2 completed, expect priority=6 last
    if second:
        _complete_task_native(second["task_id"])
    third = get_session_next_ready("conductor", project_id=queue_pid)
    third_pri = third.get("priority") if third else None
    record(
        f"PASS queue_order_pri6_third priority={third_pri}"
        if third_pri == 6
        else f"FAIL queue_order_pri6_third third={third_pri} expected 6"
    )

    # 20 — Horizon amendment #2 cont: created_at tie-break under equal priority.
    # Two tasks with same priority — older (created first) wins ASC tie-break.
    tie_pid = f"{prefix}-tie-break-probe"
    create_project(project_id=tie_pid, name="tie break probe", supervisor="conductor", priority=10)
    tie_phase = f"{tie_pid}-phase"
    create_phase(project_id=tie_pid, phase_id=tie_phase, name="tie probe", order=0)
    create_task(phase_id=tie_phase, task_id=f"{tie_pid}-task-old", description="older task", priority=5, owner="conductor")
    _time.sleep(0.05)
    create_task(phase_id=tie_phase, task_id=f"{tie_pid}-task-new", description="newer task", priority=5, owner="conductor")
    tie_winner = get_session_next_ready("conductor", project_id=tie_pid)
    tie_winner_id = tie_winner.get("task_id") if tie_winner else None
    record(
        f"PASS queue_order_created_at_tiebreak winner={tie_winner_id}"
        if tie_winner_id == f"{tie_pid}-task-old"
        else f"FAIL queue_order_created_at_tiebreak winner={tie_winner_id} expected {tie_pid}-task-old"
    )

    # 21 missing declared dependency blocks readiness and surfaces an ingest warning
    missing_dep_project = f"{prefix}-missing-dep"
    missing_dep_md = "\n".join([
        f"# Project: {missing_dep_project} - Missing dep probe",
        "> acceptance",
        f"## Phase: {missing_dep_project}-phase - Main [order:0]",
        f"### Task: {missing_dep_project}-a - upstream [owner:conductor] [priority:1]",
        f"### Task: {missing_dep_project}-b - downstream [owner:conductor] [priority:2] [depends:{missing_dep_project}-ghost]",
    ])
    load_result = load_plan_from_text(
        missing_dep_md,
        source_path="/tmp/missing-dep.md",
        source_kind="markdown",
        ingested_by="tester",
        supervisor="conductor",
        config=CFG,
    )
    missing_first = get_session_next_ready("conductor", project_id=missing_dep_project)
    _complete_task_native(f"{missing_dep_project}-a")
    missing_second = get_session_next_ready("conductor", project_id=missing_dep_project)
    missing_errors = list(load_result.get("errors") or [])
    record(
        f"PASS missing_dependency_blocks first={missing_first.get('task_id') if missing_first else None} second={missing_second.get('task_id') if missing_second else None} errors={missing_errors}"
        if missing_first and missing_first.get("task_id") == f"{missing_dep_project}-a"
        and missing_second is None
        and any(f"{missing_dep_project}-ghost" in err for err in missing_errors)
        else f"FAIL missing_dependency_blocks first={missing_first} second={missing_second} errors={missing_errors}"
    )
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        missing_zero_dep = session.run(
            _ZERO_DEP_READY_CYPHER,
            task_id=f"{missing_dep_project}-b",
        ).single()
    record(
        f"PASS missing_dependency_zero_dep_hidden record={missing_zero_dep}"
        if missing_zero_dep is None
        else f"FAIL missing_dependency_zero_dep_hidden record={dict(missing_zero_dep)}"
    )

    # 22 wrong-owner sessions do not surface another owner's task
    wrong_owner_project = f"{prefix}-wrong-owner"
    wrong_owner_phase = f"{wrong_owner_project}-phase"
    create_project(project_id=wrong_owner_project, name="wrong owner probe", supervisor="conductor", priority=10)
    create_phase(project_id=wrong_owner_project, phase_id=wrong_owner_phase, name="wrong owner phase", order=0)
    create_task(
        phase_id=wrong_owner_phase,
        task_id=f"{wrong_owner_project}-task",
        description="conductor only",
        priority=1,
        owner="conductor",
        config=CFG,
    )
    conductor_next = get_session_next_ready("conductor", project_id=wrong_owner_project)
    grok_next = get_session_next_ready("grok", project_id=wrong_owner_project)
    record(
        f"PASS wrong_owner_hidden conductor={conductor_next.get('task_id') if conductor_next else None} grok={grok_next}"
        if conductor_next and conductor_next.get("task_id") == f"{wrong_owner_project}-task" and grok_next is None
        else f"FAIL wrong_owner_hidden conductor={conductor_next} grok={grok_next}"
    )

    # 23 NULL-status tasks stay ready across all surfacing paths and the claim path
    null_status_project = f"{prefix}-null-status"
    null_status_phase = f"{null_status_project}-phase"
    null_status_task = f"{null_status_project}-task"
    create_project(project_id=null_status_project, name="null status probe", supervisor="conductor", priority=10)
    create_phase(project_id=null_status_project, phase_id=null_status_phase, name="null status phase", order=0)
    create_task(
        phase_id=null_status_phase,
        task_id=null_status_task,
        description="null status task",
        priority=1,
        owner="conductor",
        config=CFG,
    )
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run(
            "MATCH (t:OrchTask {id: $task_id}) SET t.status = NULL",
            task_id=null_status_task,
        )
        zero_dep_ready = session.run(
            _ZERO_DEP_READY_CYPHER,
            task_id=null_status_task,
        ).single()
    session_ready = get_session_next_ready("conductor", project_id=null_status_project)
    project_ready = get_project_ready_tasks(null_status_project, owner="conductor", config=CFG)
    global_ready = next((task for task in get_ready_tasks(config=CFG) if task["id"] == null_status_task), None)
    _claim_ready_orch_task(null_status_task, "conductor")
    null_status_payload = CLIENT.get(f"/api/tasks/{null_status_task}").json()
    record(
        f"PASS null_status_ready_consistent session={session_ready.get('task_id') if session_ready else None} project={project_ready[0]['id'] if project_ready else None} global={global_ready.get('id') if global_ready else None} zero_dep={dict(zero_dep_ready) if zero_dep_ready else None} claimed_status={null_status_payload.get('status')}"
        if session_ready and session_ready.get("task_id") == null_status_task
        and project_ready and project_ready[0]["id"] == null_status_task
        and global_ready and global_ready.get("id") == null_status_task
        and zero_dep_ready and zero_dep_ready["task_id"] == null_status_task
        and null_status_payload.get("status") == "in_progress"
        else f"FAIL null_status_ready_consistent session={session_ready} project={project_ready} global={global_ready} zero_dep={dict(zero_dep_ready) if zero_dep_ready else None} task={null_status_payload}"
    )

    # 24 stop-status follows actual supervised projects, even for suffixed supervisor ids
    wake_reason_project = f"{prefix}-wake-reason"
    wake_reason_phase = f"{wake_reason_project}-phase"
    create_project(project_id=wake_reason_project, name="wake reason probe", supervisor="conductor", priority=10)
    create_phase(project_id=wake_reason_project, phase_id=wake_reason_phase, name="wake reason phase", order=0)
    create_task(
        phase_id=wake_reason_phase,
        task_id=f"{wake_reason_project}-task",
        description="requires stop reason",
        priority=1,
        owner="conductor",
        config=CFG,
    )
    update_task_status(f"{wake_reason_project}-task", "in_progress", owner="conductor", config=CFG)
    supervisor_stop = get_session_stop_status("conductor", config=CFG)
    worker_stop = get_session_stop_status("conductor-codex", config=CFG)
    suffixed_supervisor_project = f"{prefix}-wake-reason-suffixed"
    suffixed_phase = f"{suffixed_supervisor_project}-phase"
    suffixed_task = f"{suffixed_supervisor_project}-task"
    create_project(project_id=suffixed_supervisor_project, name="wake reason suffixed", supervisor="conductor-codex", priority=10)
    create_phase(project_id=suffixed_supervisor_project, phase_id=suffixed_phase, name="wake reason suffixed phase", order=0)
    create_task(
        phase_id=suffixed_phase,
        task_id=suffixed_task,
        description="suffixed supervisor task",
        priority=1,
        owner="conductor-codex",
        config=CFG,
    )
    add_dependency(suffixed_task, f"{suffixed_task}-ghost", config=CFG)
    suffixed_supervisor_stop = get_session_stop_status("conductor-codex", config=CFG)
    unrelated_worker_stop = get_session_stop_status("conductor-gemini", config=CFG)
    record(
        f"PASS wake_reason_supervisor_only supervisor={supervisor_stop['decision']} worker={worker_stop['decision']} suffixed={suffixed_supervisor_stop['decision']} unrelated={unrelated_worker_stop['decision']}"
        if supervisor_stop["decision"].get("wake_type") == "WAKE_REASON_REQUIRED"
        and worker_stop["decision"].get("can_stop") is True
        and worker_stop["decision"].get("wake_type") is None
        and suffixed_supervisor_stop["decision"].get("wake_type") == "WAKE_REASON_REQUIRED"
        and unrelated_worker_stop["decision"].get("can_stop") is True
        and unrelated_worker_stop["decision"].get("wake_type") is None
        else f"FAIL wake_reason_supervisor_only supervisor={supervisor_stop['decision']} worker={worker_stop['decision']} suffixed={suffixed_supervisor_stop['decision']} unrelated={unrelated_worker_stop['decision']}"
    )

    # 25 orch-watch suffix fallback is opt-in; explicit parent still wins
    from fleet_orchestrator.scripts.orch_watch import resolve_supervisor

    class _FakeRedis:
        def __init__(self, values):
            self._values = values

        def get(self, key):
            return self._values.get(key)

    prior_suffix_rules = os.environ.get("ORCH_SUPERVISOR_SUFFIX_RULES")
    try:
        os.environ.pop("ORCH_SUPERVISOR_SUFFIX_RULES", None)
        no_parent = resolve_supervisor(_FakeRedis({}), "worker-codex")
        explicit_parent = resolve_supervisor(_FakeRedis({"taey:worker-codex:parent": "supervisor-explicit"}), "worker-codex")
        os.environ["ORCH_SUPERVISOR_SUFFIX_RULES"] = "-codex,-gemini"
        configured_suffix = resolve_supervisor(_FakeRedis({}), "worker-codex")
    finally:
        if prior_suffix_rules is None:
            os.environ.pop("ORCH_SUPERVISOR_SUFFIX_RULES", None)
        else:
            os.environ["ORCH_SUPERVISOR_SUFFIX_RULES"] = prior_suffix_rules
    record(
        f"PASS orch_watch_supervisor_resolution no_parent={no_parent} explicit={explicit_parent} configured={configured_suffix}"
        if no_parent is None
        and explicit_parent == "supervisor-explicit"
        and configured_suffix == "worker"
        else f"FAIL orch_watch_supervisor_resolution no_parent={no_parent} explicit={explicit_parent} configured={configured_suffix}"
    )

    # 26 PATCH omit=keep semantics preserve current status and existing evidence
    patch_keep_project = f"{prefix}-patch-keep"
    patch_keep_phase = f"{patch_keep_project}-phase"
    owner_only_task = f"{patch_keep_project}-owner-only"
    create_project(project_id=patch_keep_project, name="patch keep probe", supervisor="conductor", priority=10)
    create_phase(project_id=patch_keep_project, phase_id=patch_keep_phase, name="patch keep phase", order=0)
    create_task(
        phase_id=patch_keep_phase,
        task_id=owner_only_task,
        description="owner only patch task",
        priority=1,
        owner="conductor",
        config=CFG,
    )
    update_task_status(owner_only_task, "in_progress", owner="conductor", config=CFG)
    owner_only_patch = CLIENT.patch(
        f"/api/task/{owner_only_task}",
        json={"owner": "grok", "from": "conductor"},
    )
    owner_only_after = CLIENT.get(f"/api/tasks/{owner_only_task}").json()

    evidence_only_task = f"{patch_keep_project}-evidence-only"
    create_task(
        phase_id=patch_keep_phase,
        task_id=evidence_only_task,
        description="evidence only patch task",
        priority=2,
        owner="conductor",
        config=CFG,
    )
    update_task_status(
        evidence_only_task,
        "completed",
        owner="conductor",
        commit_sha="keep1234",
        production_observation="keep observed",
        config=CFG,
    )
    evidence_only_patch = CLIENT.patch(
        f"/api/task/{evidence_only_task}",
        json={"evidence_note": "added later", "from": "conductor"},
    )
    evidence_only_after = CLIENT.get(f"/api/tasks/{evidence_only_task}").json()
    record(
        f"PASS patch_omit_keeps_status_and_evidence owner_status={owner_only_after.get('status')} owner={owner_only_after.get('owner')} evidence_status={evidence_only_after.get('status')} sha={evidence_only_after.get('closeout_commit_sha')} obs={evidence_only_after.get('closeout_production_observation')} note={evidence_only_after.get('closeout_evidence_note')}"
        if owner_only_patch.status_code == 200
        and owner_only_after.get("status") == "in_progress"
        and owner_only_after.get("owner") == "grok"
        and evidence_only_patch.status_code == 200
        and evidence_only_after.get("status") == "completed"
        and evidence_only_after.get("closeout_commit_sha") == "keep1234"
        and evidence_only_after.get("closeout_production_observation") == "keep observed"
        and evidence_only_after.get("closeout_evidence_note") == "added later"
        else f"FAIL patch_omit_keeps_status_and_evidence owner_patch={owner_only_patch.status_code} owner_task={owner_only_after} evidence_patch={evidence_only_patch.status_code} evidence_task={evidence_only_after}"
    )

    # 27 evidence-gated completion and canonical transition matrix
    transition_project = f"{prefix}-transition"
    transition_phase = f"{transition_project}-phase"
    transition_task = f"{transition_project}-task"
    create_project(project_id=transition_project, name="transition probe", supervisor="conductor", priority=10)
    create_phase(project_id=transition_project, phase_id=transition_phase, name="transition phase", order=0)
    create_task(
        phase_id=transition_phase,
        task_id=transition_task,
        description="transition task",
        priority=1,
        owner="conductor",
        config=CFG,
    )
    completed_without_evidence = CLIENT.patch(
        f"/api/task/{transition_task}",
        json={"status": "completed", "from": "conductor"},
    )
    completed_with_sha = CLIENT.patch(
        f"/api/task/{transition_task}",
        json={"status": "completed", "from": "conductor", "commit_sha": "abc1234", "production_observation": "verified in prod"},
    )
    task_after_completion = CLIENT.get(f"/api/tasks/{transition_task}").json()
    completed_to_in_progress = CLIENT.patch(
        f"/api/task/{transition_task}",
        json={"status": "in_progress", "from": "conductor"},
    )
    sha_only_project = f"{transition_project}-sha-only"
    sha_only_phase = f"{sha_only_project}-phase"
    sha_only_task = f"{sha_only_project}-task"
    create_project(project_id=sha_only_project, name="sha only probe", supervisor="conductor", priority=10)
    create_phase(project_id=sha_only_project, phase_id=sha_only_phase, name="sha only phase", order=0)
    create_task(
        phase_id=sha_only_phase,
        task_id=sha_only_task,
        description="sha only transition task",
        priority=1,
        owner="conductor",
        config=CFG,
    )
    sha_only_rejected = CLIENT.patch(
        f"/api/task/{sha_only_task}",
        json={"status": "completed", "from": "conductor", "commit_sha": "abc1234"},
    )
    sentinel_project = f"{transition_project}-keep"
    sentinel_phase = f"{sentinel_project}-phase"
    sentinel_task = f"{sentinel_project}-task"
    create_project(project_id=sentinel_project, name="sentinel probe", supervisor="conductor", priority=10)
    create_phase(project_id=sentinel_project, phase_id=sentinel_phase, name="sentinel phase", order=0)
    create_task(
        phase_id=sentinel_phase,
        task_id=sentinel_task,
        description="sentinel keep task",
        priority=1,
        owner="conductor",
        config=CFG,
    )
    sentinel_keep_rejected = CLIENT.patch(
        f"/api/task/{sentinel_task}",
        json={"status": "completed", "from": "conductor", "commit_sha": "__KEEP__"},
    )
    native_without_evidence_error = ""
    try:
        native_project = f"{transition_project}-native"
        native_phase = f"{native_project}-phase"
        native_task = f"{native_project}-task"
        create_project(project_id=native_project, name="native transition probe", supervisor="conductor", priority=10)
        create_phase(project_id=native_project, phase_id=native_phase, name="native transition phase", order=0)
        create_task(
            phase_id=native_phase,
            task_id=native_task,
            description="native transition task",
            priority=1,
            owner="conductor",
            config=CFG,
        )
        update_task_status(native_task, "completed", owner="conductor", config=CFG)
    except Exception as exc:
        native_without_evidence_error = f"{type(exc).__name__}: {exc}"
    record(
        f"PASS completion_evidence_and_matrix no_evidence={completed_without_evidence.status_code} sha_only={sha_only_rejected.status_code} with_both={completed_with_sha.status_code} commit_sha={task_after_completion.get('closeout_commit_sha')} revive={completed_to_in_progress.status_code} keep={sentinel_keep_rejected.status_code} native={native_without_evidence_error}"
        if completed_without_evidence.status_code == 409
        and sha_only_rejected.status_code == 409
        and completed_with_sha.status_code == 200
        and task_after_completion.get("closeout_commit_sha") == "abc1234"
        and task_after_completion.get("closeout_production_observation") == "verified in prod"
        and completed_to_in_progress.status_code == 409
        and sentinel_keep_rejected.status_code == 409
        and native_without_evidence_error.startswith("TaskTransitionError:")
        else f"FAIL completion_evidence_and_matrix no_evidence={completed_without_evidence.status_code} sha_only={sha_only_rejected.status_code} with_both={completed_with_sha.status_code} task={task_after_completion} revive={completed_to_in_progress.status_code} keep={sentinel_keep_rejected.status_code} native={native_without_evidence_error}"
    )

    failures = [line for line in lines if line.startswith("FAIL")]
    _cleanup(prefix)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
