#!/usr/bin/env bash
# Clean-room production gate (Phase 0).
#
# Runs on a FRESH checkout (CI runner, or a clone on an empty box). Stands up
# isolated Redis+Neo4j, installs per README, starts the real API, and EXERCISES
# the real feature end-to-end asserting real outputs. Exits non-zero on any failed
# assertion. Writes an evidence log under evidence/<sha>/cleanroom.log.
#
# This is the oracle. No unit test, no 200-only check, no author summary stands in
# for it. Each feature phase APPENDS its own real assertions to phase_assertions().
#
# Usage:  scripts/cleanroom_gate.sh            (run against the current checkout)
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
SHA="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
EVID_DIR="$REPO_ROOT/evidence/$SHA"
mkdir -p "$EVID_DIR"
LOG="$EVID_DIR/cleanroom.log"
PORT="${ORCH_GATE_PORT:-5099}"
COMPOSE_PROJECT="orch-gate-${SHA}-$$"
COMPOSE_FILE="$(mktemp /tmp/orch-gate-compose.XXXXXX.yml)"
FAILS=0

log()  { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$LOG"; }
ok()   { log "PASS  $*"; }
bad()  { log "FAIL  $*"; FAILS=$((FAILS+1)); }
check(){ # check "name" "expected" "actual"
  if [ "$2" = "$3" ]; then ok "$1 ($3)"; else bad "$1 (expected '$2' got '$3')"; fi
}

: > "$LOG"
log "=== clean-room gate @ $SHA on $(hostname) ==="

free_port() {
  python3 - <<'PY'
import socket

with socket.socket() as sock:
    sock.bind(("127.0.0.1", 0))
    print(sock.getsockname()[1])
PY
}

REDIS_PORT="$(free_port)"
NEO4J_HTTP_PORT="$(free_port)"
NEO4J_BOLT_PORT="$(free_port)"
cat > "$COMPOSE_FILE" <<EOF
services:
  redis:
    image: redis:7.4.0
    command: ["redis-server", "--appendonly", "yes"]
    ports:
      - "127.0.0.1:${REDIS_PORT}:6379"
    volumes:
      - orch_redis_data:/data
    healthcheck:
      test: ["CMD", "redis-cli", "ping"]
      interval: 5s
      timeout: 3s
      retries: 20
  neo4j:
    image: neo4j:5.21.0-community
    environment:
      NEO4J_AUTH: "none"
    ports:
      - "127.0.0.1:${NEO4J_HTTP_PORT}:7474"
      - "127.0.0.1:${NEO4J_BOLT_PORT}:7687"
    volumes:
      - orch_neo4j_data:/data
    healthcheck:
      test: ["CMD-SHELL", "cypher-shell -a bolt://127.0.0.1:7687 'RETURN 1;' >/dev/null 2>&1"]
      interval: 10s
      timeout: 5s
      retries: 20

volumes:
  orch_redis_data:
  orch_neo4j_data:
EOF

cleanup() {
  log "=== teardown ==="
  pkill -f "uvicorn lib.tasks_api.*:$PORT" 2>/dev/null
  docker compose -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE" down -v >/dev/null 2>&1
  rm -f "$COMPOSE_FILE"
}
trap cleanup EXIT

# --- infra: isolated redis + neo4j via the repo's own compose ----------------
log "--- bring up isolated infra (docker compose) ---"
docker compose -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE" up -d >>"$LOG" 2>&1
healthy=0
for i in $(seq 1 24); do
  sleep 5
  st="$(docker compose -p "$COMPOSE_PROJECT" -f "$COMPOSE_FILE" ps --format '{{.Service}}:{{.Health}}' 2>/dev/null | tr '\n' ' ')"
  if echo "$st" | grep -q "redis:healthy" && echo "$st" | grep -q "neo4j:healthy"; then healthy=1; break; fi
done
check "infra-healthy" "1" "$healthy"
[ "$healthy" = "1" ] || { log "infra never healthy; aborting"; exit 1; }

# --- config + install per README (fresh venv) --------------------------------
log "--- configure .env + install (fresh venv) ---"
cat > .env <<ENVEOF
ORCH_REDIS_HOST=127.0.0.1
ORCH_REDIS_PORT=$REDIS_PORT
ORCH_NEO4J_URI=bolt://127.0.0.1:$NEO4J_BOLT_PORT
ORCH_NEO4J_DB=neo4j
ORCH_DASHBOARD_URL=http://127.0.0.1:$PORT
ORCH_NOTIFY_LIB_ROOT=/home/mira/claude-code-fleet-notify
PYTHONPATH=/home/mira/claude-code-fleet-notify
CF_STOP_INPROGRESS=1
CF_STOP_INPROGRESS_SESSIONS=gate-stop-codex
ENVEOF
python3 -m venv .venv
# shellcheck disable=SC1091
. .venv/bin/activate
set -a; . ./.env; set +a
python3 scripts/install --skip-compose >>"$LOG" 2>&1
# install rc is allowed to be non-zero only for the health check (API not up yet);
# the real gate is the assertions below, run against a started API.

# --- start the real API ------------------------------------------------------
log "--- start API on :$PORT ---"
pkill -f "uvicorn lib.tasks_api" 2>/dev/null; sleep 1
nohup python3 -m uvicorn lib.tasks_api:app --host 127.0.0.1 --port "$PORT" >>"$LOG" 2>&1 &
up=0
for i in $(seq 1 20); do
  sleep 1
  c="$(curl -s -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/api/projects" 2>/dev/null)"
  [ "$c" = "200" ] && { up=1; break; }
done
check "api-up" "1" "$up"
[ "$up" = "1" ] || { log "API never came up; aborting"; exit 1; }

# --- BASELINE production assertions (v1.5.1 surface) --------------------------
# Each phase APPENDS real end-to-end assertions here. Baseline proves the gate
# actually exercises the product, so reviewers can judge whether it is fakeable.
phase_assertions() {
  local base="http://127.0.0.1:$PORT"

  # health reports the installed package version (not a hardcoded string)
  local ver; ver="$(curl -s "$base/health" | python3 -c 'import sys,json;print(json.load(sys.stdin).get("version",""))' 2>/dev/null)"
  local pkgver; pkgver="$(python3 -c 'import fleet_orchestrator;print(fleet_orchestrator.__version__)' 2>/dev/null)"
  check "health-version-matches-package" "$pkgver" "$ver"

  # plan ingest actually creates a project in Neo4j
  cat > /tmp/gate-plan.md <<'PLAN'
# Project: gate-project - Gate Project
> clean-room gate plan
## Phase: gate-phase - Phase One [order: 1]
### Task: gate-task - Verify [priority: 50] [owner: worker-a]
- Confirm ingest persists.
PLAN
  taey-plan ingest /tmp/gate-plan.md >>"$LOG" 2>&1
  local has_proj; has_proj="$(curl -s "$base/api/projects" | python3 -c 'import sys,json;print(any(p["id"]=="gate-project" for p in json.load(sys.stdin)["projects"]))' 2>/dev/null)"
  check "plan-ingest-persists-project" "True" "$has_proj"

  # task created via CLI is listed back
  taey-task create "gate smoke task" --from worker-a --priority 50 >>"$LOG" 2>&1
  local listed; listed="$(taey-task list 2>/dev/null | grep -c 'gate smoke task')"
  check "task-create-and-list" "1" "$listed"

  # completed requires structured evidence and persists it on the task.
  python3 - <<'PY' >>"$LOG" 2>&1
import sys
from pathlib import Path

repo_root = Path.cwd()
sys.path.insert(0, str(repo_root))

from lib.config import OrchConfig
from lib.orch_schema import create_phase, create_project, create_task

cfg = OrchConfig()
create_project("gate-evidence-project", "Gate Evidence Project", supervisor="gate-evidence", priority=1, config=cfg)
create_phase("gate-evidence-project", "gate-evidence-phase", "Main", order=1, config=cfg)
create_task(
    "gate-evidence-phase",
    "gate-evidence-task",
    "gate evidence completion task",
    owner="gate-evidence-owner",
    priority=5,
    wake_owner_if_ready=False,
    config=cfg,
)
PY
  local no_evidence_api; no_evidence_api="$(curl -s -o /tmp/gate-no-evidence.json -w '%{http_code}' -X PATCH "$base/api/task/gate-evidence-task" \
    -H 'content-type: application/json' \
    -d '{"status":"completed","from":"gate-evidence-codex"}')"
  printf 'no_evidence_api_body=%s\n' "$(cat /tmp/gate-no-evidence.json)" >>"$LOG"
  local no_evidence_rejected; no_evidence_rejected="$(python3 - <<'PY' "$no_evidence_api" /tmp/gate-no-evidence.json
import json
import sys

status = sys.argv[1]
with open(sys.argv[2], "r", encoding="utf-8") as handle:
    body = json.load(handle)
print(status == "400" and body.get("ok") is False and "requires evidence" in str(body.get("error", "")))
PY
)"
  check "task-complete-without-evidence-rejected" "True" "$no_evidence_rejected"
  local complete_with_evidence_rc=0
  TAEY_NODE_ID=gate-evidence-codex taey-task update gate-evidence-task completed --evidence '{"commit_sha":"'"$SHA"'","gate_run_id":"cleanroom-gate","production_observation":"verified in clean-room gate"}' >>"$LOG" 2>&1 || complete_with_evidence_rc=$?
  check "task-complete-with-evidence-accepted" "0" "$complete_with_evidence_rc"
  local task_evidence_ok; task_evidence_ok="$(curl -s "$base/api/tasks/gate-evidence-task" | GATE_SHA="$SHA" python3 -c 'import os,sys,json; t=json.load(sys.stdin); sha=os.environ["GATE_SHA"]; print(t.get("status")=="completed" and t.get("completed_by")=="gate-evidence-codex" and t.get("completion_evidence",{}).get("commit_sha")==sha and t.get("completion_evidence",{}).get("gate_run_id")=="cleanroom-gate" and t.get("completion_evidence",{}).get("production_observation")=="verified in clean-room gate")' 2>/dev/null)"
  check "task-completion-evidence-queryable" "True" "$task_evidence_ok"

  # recurring/new-cycle reclaim: the same OrchTask id can be dispatched again
  # only when the dispatcher explicitly asks for a reclaim.
  local reclaim_no_flag; reclaim_no_flag="$(python3 - <<'PY'
import sys
from pathlib import Path

repo_root = Path.cwd()
sys.path.insert(0, str(repo_root))

from lib.config import OrchConfig
from lib.dispatch import OrchTaskNotReady, dispatch
from lib.orch_schema import create_phase, create_project, create_task, update_task_status

cfg = OrchConfig()
create_project("gate-reclaim-project", "Gate Reclaim Project", supervisor="gate-reclaim", priority=1, config=cfg)
create_phase("gate-reclaim-project", "gate-reclaim-phase", "Main", order=1, config=cfg)
create_task("gate-reclaim-phase", "gate-reclaim-task", "gate reclaim task", owner="gate-reclaim-codex", priority=5, wake_owner_if_ready=False, config=cfg)
update_task_status("gate-reclaim-task", "completed", owner="gate-reclaim-codex", completion_evidence={"commit_sha": "seed"}, completed_by="gate-seed", config=cfg)
try:
    dispatch("gate-reclaim-codex", "gate-reclaim-task", "gate reclaim task", supervisor="gate-reclaim", allow_reclaim=False)
except OrchTaskNotReady:
    print("True")
else:
    print("False")
PY
)"
  check "dispatch-completed-without-reclaim-rejected" "True" "$reclaim_no_flag"
  local reclaim_yes_flag; reclaim_yes_flag="$(python3 - <<'PY'
import json
import sys
from pathlib import Path

repo_root = Path.cwd()
sys.path.insert(0, str(repo_root))

from lib.dispatch import clear_current_task, dispatch
from lib.orch_schema import get_task

dispatch("gate-reclaim-codex", "gate-reclaim-task", "gate reclaim task", supervisor="gate-reclaim", allow_reclaim=True)
task = get_task("gate-reclaim-task")
clear_current_task("gate-reclaim-codex")
print(
    task.get("status") == "in_progress"
    and task.get("last_claim_mode") == "reclaim"
    and task.get("last_claim_from_status") == "completed"
    and int(task.get("dispatch_cycle", 0) or 0) == 1
)
PY
)"
  check "dispatch-completed-reclaim-allowed" "True" "$reclaim_yes_flag"

  # stale ad-hoc default-project in_progress rows close themselves at the source
  # when there is no live current_task backing them.
  python3 - <<'PY' >>"$LOG" 2>&1
import sys
from pathlib import Path

repo_root = Path.cwd()
sys.path.insert(0, str(repo_root))

from lib.config import OrchConfig, get_redis_sync
from lib.orch_schema import ensure_default_project, create_task, update_task_status

cfg = OrchConfig()
phase_id = ensure_default_project(cfg)
create_task(phase_id, "gate-stale-current-1", "stale current one", owner="gate-current-codex", priority=5, wake_owner_if_ready=False, config=cfg)
create_task(phase_id, "gate-stale-current-2", "stale current two", owner="gate-current-codex", priority=6, wake_owner_if_ready=False, config=cfg)
update_task_status("gate-stale-current-1", "in_progress", owner="gate-current-codex", config=cfg)
update_task_status("gate-stale-current-2", "in_progress", owner="gate-current-codex", config=cfg)
r = get_redis_sync(cfg)
r.delete("taey:gate-current-codex:current_task")
r.delete("taey:gate-current-codex:last_outcome")
PY
  local current_none; current_none="$(curl -s "$base/api/sessions/gate-current-codex/current" | python3 -c 'import sys,json; print(json.load(sys.stdin).get("current") is None)' 2>/dev/null)"
  check "stale-ad-hoc-current-cleared" "True" "$current_none"
  local stale_statuses_ok; stale_statuses_ok="$(python3 - <<'PY' "$base"
import json
import sys
import urllib.request

base = sys.argv[1]
def fetch(task_id: str) -> dict:
    with urllib.request.urlopen(f"{base}/api/tasks/{task_id}") as response:
        return json.load(response)

one = fetch("gate-stale-current-1")
two = fetch("gate-stale-current-2")
print(one.get("status") == "interrupted" and two.get("status") == "interrupted")
PY
)"
  check "stale-ad-hoc-statuses-interrupted" "True" "$stale_statuses_ok"

  # dashboard UI actually serves HTML
  local ui; ui="$(curl -s -L -o /dev/null -w '%{http_code}' "$base/")"
  check "dashboard-ui-200" "200" "$ui"
  local title; title="$(curl -s -L "$base/" | grep -c 'Orchestrator Plan UI')"
  check "dashboard-ui-renders" "1" "$title"

  # convergence valve writes durable audit before forcing ALLOW_STOP on the
  # third repeated stop-hook block for the same in-progress task.
  local stop_task; stop_task="$(python3 - <<'PY'
import sys
from pathlib import Path

repo_root = Path.cwd()
sys.path.insert(0, str(repo_root))

from lib.config import OrchConfig
from lib.orch_schema import create_phase, create_project, create_task, update_task_status

cfg = OrchConfig()
create_project("gate-stop-project", "Gate Stop Project", supervisor="gate-stop", priority=1, config=cfg)
create_phase("gate-stop-project", "gate-stop-phase", "Main", order=1, config=cfg)
task_id = create_task(
    "gate-stop-phase",
    "gate-stop-task",
    "gate stop convergence task",
    owner="gate-stop-codex",
    priority=5,
    wake_owner_if_ready=False,
    config=cfg,
)
update_task_status(task_id, "in_progress", owner="gate-stop-codex", config=cfg)
print(task_id)
PY
)"
  printf 'seeded_stop_task=%s\n' "$stop_task" >>"$LOG"
  local convergence_json; convergence_json="$(python3 - <<'PY' "$base" "$LOG"
import json
import sys
import urllib.request

base = sys.argv[1]
log_path = sys.argv[2]

def fetch(url: str) -> dict:
    with urllib.request.urlopen(url) as response:
        return json.load(response)

results = [
    fetch(f"{base}/api/sessions/gate-stop-codex/stop-decision?stop_hook_active=true")
    for _ in range(3)
]
with open(log_path, "a", encoding="utf-8") as handle:
    handle.write("stop_decision_results=" + json.dumps(results, sort_keys=True) + "\n")
print(json.dumps(results[-1], sort_keys=True))
PY
)"
  local convergence_ok; convergence_ok="$(python3 - <<'PY' "$convergence_json"
import json
import sys

decision = json.loads(sys.argv[1])
print(
    decision.get("block") is False
    and decision.get("converged_allow") is True
    and decision.get("wake_type") == "ALLOW_STOP"
    and bool(decision.get("convergence_audit_id"))
)
PY
)"
  check "stop-convergence-force-allow" "True" "$convergence_ok"

  local audit_json; audit_json="$(python3 - <<'PY'
import json
import sys
from pathlib import Path

repo_root = Path.cwd()
sys.path.insert(0, str(repo_root))

from lib.config import OrchConfig, get_neo4j_driver

cfg = OrchConfig()
driver = get_neo4j_driver(cfg)
with driver.session(database=cfg.neo4j_db) as session:
    record = session.run(
        """
        MATCH (a:OrchStopConvergenceAudit {session_id: 'gate-stop-codex'})
        RETURN a
        ORDER BY a.created_at DESC
        LIMIT 1
        """
    ).single()
row = dict(record["a"]) if record else {}
normalized = {}
for key, value in row.items():
    iso = getattr(value, "iso_format", None)
    normalized[key] = iso() if callable(iso) else value
print(json.dumps(normalized, sort_keys=True))
PY
)"
  local audit_ok; audit_ok="$(python3 - <<'PY' "$convergence_json" "$audit_json" "$stop_task"
import json
import sys

decision = json.loads(sys.argv[1])
audit = json.loads(sys.argv[2])
task_id = sys.argv[3]
print(
    bool(audit)
    and audit.get("id") == decision.get("convergence_audit_id")
    and int(audit.get("convergence_count", 0) or 0) == 3
    and audit.get("event_type") == "stop_converged_allow"
    and audit.get("task_id") == task_id
)
PY
)"
  check "stop-convergence-audit-persisted" "True" "$audit_ok"
}
log "--- production assertions ---"
phase_assertions

# --- verdict -----------------------------------------------------------------
log "=== RESULT: $FAILS failure(s) @ $SHA ==="
[ "$FAILS" = "0" ] && log "GATE GREEN" || log "GATE RED"
exit "$FAILS"
