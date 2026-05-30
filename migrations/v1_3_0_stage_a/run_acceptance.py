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

from fastapi.testclient import TestClient

from lib.config import OrchConfig, get_neo4j_driver
from lib.orch_schema import (
    add_dependency,
    clear_project_stop_reason,
    create_phase,
    create_project,
    create_task,
    edit_project_condition,
    get_project_ready_tasks,
    get_session_stop_status,
    preflight_supervisor_orphan_check,
    ready_work,
    set_project_stop_reason,
    set_session_pause,
    clear_session_pause,
    update_task_status,
)
from lib.tasks_api import app


CFG = OrchConfig()
CFG.neo4j_db = "neo4j"
CLIENT = TestClient(app)


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


def main() -> int:
    lines = []
    prefix = "stage-a-"
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
    preflight_raw = subprocess.check_output(
        [
            sys.executable,
            "-c",
            (
                "import json; "
                "from lib.orch_schema import preflight_supervisor_orphan_check; "
                "print(json.dumps(preflight_supervisor_orphan_check(), sort_keys=True))"
            ),
        ],
        text=True,
        env=os.environ.copy(),
        cwd=os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    ).strip()
    preflight = json.loads(preflight_raw)
    record(f"PASS preflight_zero count={preflight['count']}" if preflight["count"] == 0 and preflight["ok"] is True else f"FAIL preflight_zero count={preflight['count']} ok={preflight['ok']}")

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
    failures = [line for line in lines if line.startswith("FAIL")]
    _cleanup(prefix)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
