# claude-code-fleet-orchestrator

`claude-code-fleet-orchestrator` is a local-first, single-user orchestration layer for a fleet of Claude Code or CLI agent sessions running on one operator's machine. It is not a hosted service, not a multi-tenant team server, and not a SaaS control plane. The default posture is private: the mutable API and dashboard bind to `127.0.0.1` unless the operator explicitly sets `ORCH_HOST` to another interface.

The system gives one person a durable "score" for coordinating multiple coding agents without babysitting every handoff. State lives in Neo4j and Redis. A FastAPI service on `:5002`, a browser dashboard, command-line tools, and Claude Code hooks connect each session's lifecycle to that state.

## Mental Model

You run several Claude Code or CLI sessions as a fleet. The orchestrator keeps the shared project score: projects contain phases, phases contain tasks, and each task has an owner, status, priority, dependencies, optional refs, and completion evidence.

**Stop-discipline engine.** A session cannot silently stop while it still has ready work. The Stop hook asks the orchestrator for a stop decision; if work remains, the hook blocks the stop and feeds back the next action. Human-review gates are a first-class stop state: when work is waiting on a person, the session can stop cleanly instead of looping.

**Dynamic context injection.** Plans and tasks can attach reference docs at overall, supervisor, project, phase, and task tiers. When `ORCH_WAKE_PACKET_ENABLED=1`, `GET /api/sessions/{session_id}/wake-packet` assembles a task-scoped packet with refs, ranked memory, rules, provenance, and a size report. This pairs with `/clear`: clear accumulated chat context, then re-inject the clean slice for the current task. Empty refs mean none were attached; that is valid.

**Evidence-gated completion.** Terminal task updates require evidence. For normal task completion, provide a JSON object with real artifacts such as `commit_sha`, `gate`, and `production_observation`; the API rejects evidence-less terminal claims. Human-review gate tasks must be completed through the question/UI path, not by ordinary task status updates.

## Five-Minute Quickstart

Prerequisites:

- Python 3.10+ with `venv`
- Redis
- Neo4j
- Git
- Optional for the bundled local infrastructure path: Docker Compose
- Optional for Claude Code hook wiring: a sibling checkout of `claude-code-fleet-notify`

Clone and install the package:

```bash
git clone https://github.com/palios-taey/claude-code-fleet-orchestrator.git
cd claude-code-fleet-orchestrator
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
cp .env.example .env
```

Start local Redis and Neo4j. The bundled compose file binds both services to loopback:

```bash
docker compose up -d
```

If you already run Redis or Neo4j elsewhere, edit `.env` instead. These are the required connection variables:

```bash
ORCH_REDIS_HOST=127.0.0.1
ORCH_REDIS_PORT=6379
ORCH_NEO4J_URI=bolt://127.0.0.1:7687
ORCH_NEO4J_DB=neo4j
```

Start the API and dashboard:

```bash
fleet-orchestrator-api
```

In another shell:

```bash
. .venv/bin/activate
curl -s http://127.0.0.1:5002/health
open http://127.0.0.1:5002/ui/
```

If `open` is not available on your OS, paste `http://127.0.0.1:5002/ui/` into a browser on the same machine.

Wire Claude Code hooks after the API smoke test. Keep `claude-code-fleet-orchestrator` and `claude-code-fleet-notify` as sibling checkouts, or set `ORCH_NOTIFY_LIB_ROOT=/absolute/path/to/claude-code-fleet-notify` in `.env`.

Preview the hook/settings changes first:

```bash
scripts/install --dry-run
```

Then apply them:

```bash
scripts/install --skip-compose
```

`--skip-compose` tells the installer to use the Redis and Neo4j you already started. The installer creates `.venv` if needed, installs the package, wires managed Claude Code hooks, starts notify daemons, starts orchestrator services, and runs `orch doctor`.

## Configuration

Copy [.env.example](.env.example) to `.env`. The loader reads `ORCH_DOTENV` first if set, then `.env` in the current directory, then `.env` at the repo root.

Required:

- `ORCH_REDIS_HOST` and `ORCH_REDIS_PORT`: Redis used by notifications, liveness, locks, and daemon state.
- `ORCH_NEO4J_URI` and `ORCH_NEO4J_DB`: Neo4j used for projects, phases, tasks, refs, gates, and evidence.
- `ORCH_NEO4J_USER` / `ORCH_NEO4J_PASS`: optional credentials when your Neo4j instance requires auth.

Network posture:

- `ORCH_HOST`: dashboard/API bind host. Default is `127.0.0.1`. Set `ORCH_HOST=0.0.0.0` only as an explicit trusted single-user LAN opt-in.
- `ORCH_PORT`: dashboard/API port. Default is `5002`.
- `ORCH_DASHBOARD_URL`: base URL used by CLIs and hooks. Default is `http://127.0.0.1:5002`.
- `ORCH_AUTH_TOKEN`: optional mutable-endpoint token. When set, `POST`, `PUT`, `PATCH`, and `DELETE` requests require either `Authorization: Bearer <token>` or `X-API-Key: <token>`. Read endpoints remain open. When unset, the default loopback bind is the security boundary.

By default the dashboard binds `127.0.0.1`, reachable only from the machine it runs on. That localhost bind is the security boundary for the mutable API: the dashboard is not a multi-user service and must not accept untrusted callers. Binding to any non-loopback interface is an explicit, deliberate operator opt-in for a trusted single-user network only.

Fleet and context:

- `ORCH_SESSION_IDS`: optional comma-separated session allowlist for the dashboard notify form and wake-packet endpoint.
- `ORCH_NOTIFY_LIB_ROOT`: path to `claude-code-fleet-notify` if it is not importable and not a sibling checkout.
- `ORCH_NOTIFY_CLI`: notify CLI name. Default is `taey-notify`.
- `ORCH_REF_ALLOWED_ROOT`: one or more trusted roots for plan/source refs. Refs are disabled fail-safe when unset.
- `ORCH_SESSION_ROOTS`: JSON or comma-separated `session=/repo/root` map used by the context assembler to find each session's `MEMORY.md` and repo rules.
- `ORCH_WAKE_PACKET_ENABLED`: set to `1` to enable the wake-packet API.
- `ORCH_RULES_ROOT`: optional rules directory used by the context assembler.
- `ORCH_SHIP_GATES`: comma-separated task-id suffixes that must be complete before a project can be marked shippable.

## How An AI Agent Uses This

An agent reads the project score through the CLI, does the work in its normal repo, and writes status back with evidence.

```bash
taey-plan list
taey-plan show <project-id>
taey-plan current <session-id>
taey-plan next <session-id>
```

A supervisor ingests markdown plans:

```bash
taey-plan ingest /absolute/path/to/plan.md --supervisor conductor
```

The plan format is documented in [docs/PLAN_FORMAT.md](docs/PLAN_FORMAT.md). Task headers can declare priority, owner, dependencies, and refs. Example:

```md
# Project: demo - Demo

## Phase: build - Build [order: 1]

### Task: demo::build-1 - Verify install [priority: 50] [owner: conductor-codex]
Run the smoke test and record evidence.
```

An agent checks and updates tasks:

```bash
taey-task status demo::build-1
taey-task update demo::build-1 completed --evidence '{"commit_sha":"abc123","gate":"curl /health","production_observation":"HTTP 200 ok true"}'
```

A human-review gate records a question that must be answered by a person:

```bash
taey-question create-gate demo::build demo::human-review "Ship this artifact?" --reviewer jesse
taey-question answer <question-id> "Ship it" --from conductor
```

For harness-driven work where the runner is not the agent session itself:

```bash
taey-dispatch out-of-band register demo::build-1 --supervisor conductor --owner conductor-codex --runner acceptance-harness --ttl 300
taey-dispatch out-of-band heartbeat demo::build-1
taey-dispatch out-of-band complete demo::build-1 --status completed --supervisor conductor --evidence '{"commit_sha":"abc123","gate":"production probe","production_observation":"verified live"}'
```

The hooks close the loop:

- `PreToolUse` / `PostToolUse` activity hooks keep liveness fresh.
- The Stop hook asks the orchestrator whether the session may stop.
- `orch-watch` listens for Redis state transitions and wakes supervisors when a stopped or idle session has actionable work.

## API And Dashboard

Run the API in the foreground:

```bash
orch serve
```

Run managed background services:

```bash
orch enable
orch doctor --explain-scope
```

Stop managed services:

```bash
orch disable
```

Remove orchestrator-managed Claude settings and services:

```bash
orch uninstall
```

The dashboard is served at `/ui/`. The API root redirects there. Useful read endpoints:

- `GET /health`
- `GET /api/projects`
- `GET /api/projects/{project_id}`
- `GET /api/sessions`
- `GET /api/sessions/{session_id}/current`
- `GET /api/sessions/{session_id}/next-ready`
- `GET /api/sessions/{session_id}/stop-decision`
- `GET /api/sessions/{session_id}/wake-packet`

Mutable endpoints create and update projects, phases, tasks, questions, stop conditions, loop state, and notifications. Treat `:5002` as a local operator API, not a public service.

## Refs And Wake Packets

Refs are structured pointers attached to the plan. They are not copied into Neo4j as permanent file contents. At runtime, the assembler reads allowed refs fresh and wraps untrusted content in nonce envelopes so file text cannot forge packet sections.

Enable refs by setting `ORCH_REF_ALLOWED_ROOT` to trusted roots. Without it, ref use fails closed.

Enable wake packets:

```bash
export ORCH_WAKE_PACKET_ENABLED=1
curl -s "http://127.0.0.1:5002/api/sessions/conductor-codex/wake-packet?cli=codex"
```

The response includes `packet` plus `packet_meta` with a provenance hash, size report, snapshot fingerprints, and the generating commit.

## Scope

This is for one operator coordinating their own local fleet on their own machine.

It is not:

- a multi-tenant system
- a hosted control plane
- an internet-facing dashboard
- a replacement for per-repo CI, code review, or production verification
- a general scheduler for arbitrary teams

Routing policy is intentionally limited. The current system coordinates explicit owners, supervisors, dependencies, gates, and stop discipline. Automatic model/worker routing is future work.

## Verification

Useful local checks:

```bash
python3 -c "import fleet_orchestrator; print(fleet_orchestrator.__version__)"
orch --help
orch doctor --explain-scope
taey-plan --help
taey-task --help
taey-question --help
taey-dispatch --help
curl -s http://127.0.0.1:5002/health
```

The ship gate runs acceptance scripts under [tests/](tests/), including stop decisions, human-review gates, ref safety, wake packets, public read-only behavior, and task completion evidence.

## More Docs

- [SETUP.md](SETUP.md): operator install flow and lifecycle details.
- [docs/WALKTHROUGH.md](docs/WALKTHROUGH.md): guided first supervised loop.
- [docs/PLAN_FORMAT.md](docs/PLAN_FORMAT.md): markdown plan format.
- [SECURITY.md](SECURITY.md): security posture and reporting.
