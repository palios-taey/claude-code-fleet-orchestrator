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
FAILS=0

log()  { echo "[$(date -u +%H:%M:%S)] $*" | tee -a "$LOG"; }
ok()   { log "PASS  $*"; }
bad()  { log "FAIL  $*"; FAILS=$((FAILS+1)); }
check(){ # check "name" "expected" "actual"
  if [ "$2" = "$3" ]; then ok "$1 ($3)"; else bad "$1 (expected '$2' got '$3')"; fi
}

: > "$LOG"
log "=== clean-room gate @ $SHA on $(hostname) ==="

cleanup() {
  log "=== teardown ==="
  pkill -f "uvicorn lib.tasks_api.*:$PORT" 2>/dev/null
  docker compose down -v >/dev/null 2>&1
}
trap cleanup EXIT

# --- infra: isolated redis + neo4j via the repo's own compose ----------------
log "--- bring up isolated infra (docker compose) ---"
docker compose up -d >>"$LOG" 2>&1
healthy=0
for i in $(seq 1 24); do
  sleep 5
  st="$(docker compose ps --format '{{.Service}}:{{.Health}}' 2>/dev/null | tr '\n' ' ')"
  if echo "$st" | grep -q "redis:healthy" && echo "$st" | grep -q "neo4j:healthy"; then healthy=1; break; fi
done
check "infra-healthy" "1" "$healthy"
[ "$healthy" = "1" ] || { log "infra never healthy; aborting"; exit 1; }

# --- config + install per README (fresh venv) --------------------------------
log "--- configure .env + install (fresh venv) ---"
cat > .env <<ENVEOF
ORCH_REDIS_HOST=127.0.0.1
ORCH_REDIS_PORT=6379
ORCH_NEO4J_URI=bolt://127.0.0.1:7687
ORCH_NEO4J_DB=neo4j
ORCH_DASHBOARD_URL=http://127.0.0.1:$PORT
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

  # dashboard UI actually serves HTML
  local ui; ui="$(curl -s -L -o /dev/null -w '%{http_code}' "$base/")"
  check "dashboard-ui-200" "200" "$ui"
  local title; title="$(curl -s -L "$base/" | grep -c 'Orchestrator Plan UI')"
  check "dashboard-ui-renders" "1" "$title"
}
log "--- production assertions ---"
phase_assertions

# --- verdict -----------------------------------------------------------------
log "=== RESULT: $FAILS failure(s) @ $SHA ==="
[ "$FAILS" = "0" ] && log "GATE GREEN" || log "GATE RED"
exit "$FAILS"
