# claude-code-fleet-orchestrator

`claude-code-fleet-orchestrator` is a local stop-discipline orchestrator for one operator running their own Claude Code fleet on one machine.

It keeps sessions on-task without requiring a human to re-dispatch work after every stop:

- the task graph lives in Neo4j
- session inbox/idle state lives in Redis
- `orch-watch` reacts to stop/idle transitions
- the task/plan CLIs and API expose the shared work graph
- the integrity gate blocks silent-fallback patterns mechanically

Package version in this branch: `1.4.0`.

## What It Is

This repository is for the local, single-user, single-machine case:

- one operator
- one trusted machine
- one local Redis
- one local Neo4j
- multiple local CLI sessions coordinating through one task graph

It is not a hosted service, not a multi-tenant control plane, and not an auth-first web app. The default trust boundary is localhost.

## Why

Without an orchestrator, a worker session stops and the operator has to notice, inspect state, decide whether there is more ready work, and manually wake the next step.

This package closes that loop:

- `dispatch.py` records active work for worker sessions
- the companion `claude-code-fleet-notify` hooks report stop outcomes
- `orch-watch` decides whether the session or supervisor should be woken
- `taey-plan` / `taey-task` expose the current and next-ready work graph

The differentiator is the integrity gate: “works or fails loud” is enforced by code review automation and CI, not by operator discipline alone.

## Install

Prerequisites:

- Python `3.10+`
- Redis reachable from this machine
- Neo4j reachable from this machine
- the companion transport package [`claude-code-fleet-notify`](https://github.com/palios-taey/claude-code-fleet-notify) installed for the stop-hook and inbox runtime

Install from the repo root:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install .
```

The install provides four console entry points:

- `orch-cron`
- `orch-watch`
- `taey-plan`
- `taey-task`

The tasks API is started as a module:

```bash
python -m fleet_orchestrator.tasks_api
```

## Five-Minute Quickstart

1. Install the package.

   ```bash
   python3 -m venv .venv
   source .venv/bin/activate
   pip install .
   ```

2. Start the local API on loopback.

   ```bash
   python -m fleet_orchestrator.tasks_api
   ```

3. In another shell, verify the API is up.

   ```bash
   curl http://127.0.0.1:5002/health
   ```

4. Verify the installed entry points.

   ```bash
   orch-cron --help
   orch-watch --help
   taey-plan --help
   taey-task --help
   ```

5. Inspect work once your graph is configured.

   ```bash
   taey-plan current
   taey-plan next
   ```

## Runtime Pieces

| Component | Path | Purpose |
| --- | --- | --- |
| Tasks API | `src/fleet_orchestrator/tasks_api.py` | FastAPI service on `127.0.0.1:5002` by default, plus the `/ui/` browser surface |
| Graph model | `src/fleet_orchestrator/orch_schema.py` | Neo4j schema and state transitions for projects, phases, tasks, and questions |
| Plan ingest | `src/fleet_orchestrator/plan_loader.py` | Markdown plan ingest into the graph |
| Dispatch/state | `src/fleet_orchestrator/dispatch.py` | Redis dispatch wire plus worker outcome recording |
| Readiness | `src/fleet_orchestrator/plan_readiness.py` | Readiness checker used by `orch-watch` |
| Daemons | `src/fleet_orchestrator/scripts/orch_watch.py`, `src/fleet_orchestrator/scripts/orch_cron.py` | Event-driven watch loop and recurring wake runner |

## Configuration

`src/fleet_orchestrator/config.py` loads environment defaults from `.env` candidates and then reads the live process environment.

Load-bearing settings:

- `ORCH_NEO4J_URI`
- `ORCH_NEO4J_USER` / `ORCH_NEO4J_PASS` when your Neo4j requires auth
- `ORCH_NEO4J_REQUIRE_AUTH=1` if you want missing auth to fail loud
- `ORCH_REDIS_HOST` / `ORCH_REDIS_PORT`
- `ORCH_API_HOST` / `ORCH_API_PORT`

The default API bind is `127.0.0.1:5002`.

## Documentation

- [ARCHITECTURE.md](ARCHITECTURE.md) — runtime shape and data flow
- [SCHEMA.md](SCHEMA.md) — actual Neo4j node/relationship model and priority convention
- [docs/PLAN_FORMAT.md](docs/PLAN_FORMAT.md) — markdown plan ingest format
- [CHANGELOG.md](CHANGELOG.md) — branch and release history
- [SECURITY.md](SECURITY.md) — local-trust security model

## Integrity Gate

Run the repository gate locally with:

```bash
python3 tools/lint_no_silent_fallbacks.py --all
```

CI runs the same gate and also verifies that a clean `pip install .` exposes all four CLI entry points.

## License

Apache-2.0
