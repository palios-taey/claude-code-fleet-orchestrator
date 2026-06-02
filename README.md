# claude-code-fleet-orchestrator

`claude-code-fleet-orchestrator` coordinates supervised worker sessions over Redis, Neo4j, and a notify transport. It provides:

- a dispatch primitive that records active work for a worker session
- an event-driven watch daemon that escalates stuck or newly-unblocked work
- a FastAPI surface for tasks, projects, and plan ingestion
- CLI tools for plan and task operations

## Requirements

- Python 3.10+
- Redis
- Neo4j
- a working `claude-code-fleet-notify` installation, or `ORCH_NOTIFY_LIB_ROOT` pointing at one

## Configuration

Copy [.env.example](.env.example) to `.env` and set the values for your environment.

Required variables:

- `ORCH_REDIS_HOST`
- `ORCH_REDIS_PORT`
- `ORCH_NEO4J_URI`
- `ORCH_NEO4J_DB`
- `ORCH_DASHBOARD_URL`

Optional variables:

- `ORCH_REDIS_SENTINELS`
- `ORCH_REDIS_SENTINEL_MASTER`
- `ORCH_NEO4J_USER`
- `ORCH_NEO4J_PASS`
- `ORCH_NOTIFY_LIB_ROOT`
- `ORCH_NOTIFY_CLI`
- `ORCH_SESSION_IDS`
- `ORCH_PRODUCT_OWNER_MAP`
- `ORCH_DOTENV`

## Install

```bash
scripts/install
```

## Smoke test

```bash
python3 -c "import fleet_orchestrator; print(fleet_orchestrator.__version__)"
orch doctor --explain-scope
orch-cron --help
orch-watch --help
taey-plan --help
taey-task --help
```

## Run the API

```bash
python3 -m uvicorn lib.tasks_api:app --host 127.0.0.1 --port 5002
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5002/api/projects
```

## Plan ingest example

```bash
cat > /tmp/sample-plan.md <<'EOF'
# Project: sample-project - Sample Project
> Minimal plan used for smoke testing.

## Phase: sample-phase - Phase One [order: 1]

### Task: sample-task - Verify install [priority: 50] [owner: worker-a]
- Confirm the orchestrator CLI entry points resolve.
EOF

taey-plan ingest /tmp/sample-plan.md
```

## Run the watcher

```bash
orch-watch \
  --redis-host 127.0.0.1 \
  --readiness-checker lib/plan_readiness.py:check_readiness
```

## Companion products

The orchestrator is the core. A few small, separately released products compose with it. Start minimal: orchestrator + notify + `orch doctor` green. Add the others only when you hit the specific problem they solve.

### Required

- **[claude-code-fleet-notify](https://github.com/palios-taey/claude-code-fleet-notify)** — the hook, daemon, Redis inbox, and CLI layer the orchestrator depends on. `scripts/install` wires its hooks for you.

### Recommended (manual install)

- **[claude-code-api-watchdog](https://github.com/palios-taey/claude-code-api-watchdog)** — surfaces Claude Code API stalls and failures instead of leaving a wedged session silent.
- **[mcp-reconnect](https://github.com/palios-taey/mcp-reconnect)** — keeps MCP connections alive across disconnects.

### Optional

- **[restart-safe-agents](https://github.com/palios-taey/restart-safe-agents)** — patterns for agents that survive restart without losing in-flight work.
- **[claude-code-fleet-cockpit-template](https://github.com/palios-taey/claude-code-fleet-cockpit-template)** — the start-here template for the shared fleet operating spine: routing, recaps, action logs, prompting standards, 6SIGMA workflow, per-CLI orientation, and cron registry for teams running the released claude-code-fleet products.

### Planned, not yet released

- A one-command suite installer is planned but not yet published.

## Documentation

- [docs/SCHEMA.md](docs/SCHEMA.md)
- [docs/PLAN_FORMAT.md](docs/PLAN_FORMAT.md)
- [SUPPORT.md](SUPPORT.md)
- [SECURITY.md](SECURITY.md)

## License

Apache-2.0
