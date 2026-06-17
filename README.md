# claude-code-fleet-orchestrator

`claude-code-fleet-orchestrator` is a local-first, single-user orchestration layer for a fleet of Claude Code or CLI agent sessions running on one operator's machine. It is not a hosted service, not a multi-tenant team server, and not a SaaS control plane. The default posture is private: the mutable API and dashboard bind to `127.0.0.1` unless the operator explicitly sets `ORCH_HOST` to another interface.

The system gives one person a durable "score" for coordinating multiple coding agents without babysitting every handoff. State lives in Neo4j and Redis because projects, phases, tasks, dependencies, human-review gates, refs, and provenance are graph-shaped; the product asks "what is ready, blocked, supervised, or gated?" more often than it asks for flat rows. A FastAPI service on `:5002`, a browser dashboard, command-line tools, and Claude Code hooks connect each session's lifecycle to that state.

Motivating scenario: you have `supervisor`, `worker-codex`, and `worker-gemini` sessions open on one Linux workstation. Codex is implementing, Gemini is measuring, and the supervisor is coordinating. Without a shared score, work gets lost after `/clear`, a worker can stop while a dependent task is ready, and "done" becomes a chat claim. This repo makes those handoffs explicit local state.

![Fleet dashboard showing session cards, current work, projects, and task details](docs/dashboard.png)

Screenshot: local `:5005` read-only dashboard view with real fleet state. The operator API/dashboard runs on `:5002`; `:5005` is a separate read-only dashboard surface served by `scripts/orch-public`, useful when you want a scrubbed view without mutable routes.

Status: active single-user local tool, Apache-2.0 licensed (see [CHANGELOG.md](CHANGELOG.md) and the GitHub releases for the current version — the version is not duplicated here, where it would drift). It is mature enough to run its own ship gates, but it still expects a technical operator comfortable with local Redis, Neo4j, and Claude Code hook wiring.

## Mental Model

You run several Claude Code or CLI sessions as a fleet. The orchestrator keeps the shared project score: projects contain phases, phases contain tasks, and each task has an owner, status, priority, dependencies, optional refs, and completion evidence.

**Stop-discipline engine.** The hook path is designed so a session does not silently stop while it still has ready work. The Stop hook asks the orchestrator for a stop decision; if work remains and hooks are installed, the hook blocks the stop and feeds back the next action. Human-review gates are a first-class stop state: when work is waiting on a person, the session can stop cleanly instead of looping.

**Dynamic context injection.** Plans and tasks can attach reference docs at overall, supervisor, project, phase, and task tiers. `GET /api/sessions/{session_id}/wake-packet` is enabled by default and assembles a task-scoped packet with refs, ranked memory, rules, provenance, and a size report. `ORCH_WAKE_PACKET_ENDPOINT_ENABLED=0` disables only that endpoint; the old `ORCH_WAKE_PACKET_ENABLED` name remains as a deprecated alias. This pairs with `/clear`: clear accumulated chat context, then re-inject the clean slice for the current task. Empty refs mean none were attached; that is valid.

**Evidence-gated completion.** Terminal task updates are designed to require evidence. For normal task completion, provide a JSON object with real artifacts such as `commit_sha`, `gate`, and `production_observation`; the API rejects evidence-less terminal claims. Human-review gate tasks must be completed through the question/UI path, not by ordinary task status updates.

## Five-Minute Quickstart

Prerequisites:

- Linux or a Linux-like environment with a POSIX shell
- Python 3.10+ with `venv`
- Git
- Docker Compose for the bundled Redis + Neo4j path
- Claude Code
- `claude-code-fleet-notify` as a sibling checkout, required for Claude Code hooks and the stop-discipline loop

Use one canonical first-run path: install into a local `.venv`, start the bundled loopback Redis/Neo4j, then run the API/dashboard with `orch serve`. After that smoke test is green, wire Claude Code hooks through `claude-code-fleet-notify` to enable stop discipline.

```bash
git clone https://github.com/palios-taey/claude-code-fleet-orchestrator.git
git clone https://github.com/palios-taey/claude-code-fleet-notify.git
cd claude-code-fleet-orchestrator
python3 -m venv .venv
. .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -e .
cp .env.example .env
```

The bundled compose file sets `NEO4J_AUTH=none`. Neo4j and Redis bind to loopback and run **without auth** — the orchestrator does not support internal-service credentials (the network is the trust boundary; run these on a trusted host/LAN):

```bash
docker compose up -d
```

If Docker is unavailable and you already run Redis and Neo4j yourself, edit `.env` first and use `scripts/install --skip-compose`. Run your Neo4j with auth disabled (`NEO4J_AUTH=none`):

```bash
ORCH_REDIS_HOST=127.0.0.1
ORCH_REDIS_PORT=6379
ORCH_NEO4J_URI=bolt://127.0.0.1:7687
ORCH_NEO4J_DB=neo4j
```

Verify the install:

```bash
orch serve
```

In another terminal:

```bash
. .venv/bin/activate
curl -s http://127.0.0.1:5002/health
```

Open the dashboard:

```bash
open http://127.0.0.1:5002/ui/
```

If `open` is not available on your OS, paste `http://127.0.0.1:5002/ui/` into a browser on the same machine.

Wire Claude Code hooks after the API/dashboard smoke test. This step is required for the Stop hook behavior described above; without it, the API and dashboard work, but sessions will not consult the orchestrator when they stop.

```bash
scripts/install --dry-run
scripts/install --skip-compose
```

`--skip-compose` reuses the Redis and Neo4j you already started with Docker Compose. The installer reconciles `.venv`, applies managed Claude Code hook settings through `claude-code-fleet-notify`, starts notify daemons, starts `orch-watch`, and runs `orch doctor`.

For automation, the package also exposes `fleet-orchestrator-api`, which starts the same FastAPI app as `orch serve`. Prefer `orch serve` in the quickstart because it prints the dashboard URL and uses the same operator lifecycle surface as `orch enable`, `orch disable`, and `orch doctor`.

## Configuration

Copy [.env.example](.env.example) to `.env`. The loader reads `ORCH_DOTENV` first if set, then `.env` in the current directory, then `.env` at the repo root.

Required:

- `ORCH_REDIS_HOST` and `ORCH_REDIS_PORT`: Redis used by notifications, liveness, locks, and daemon state.
- `ORCH_NEO4J_URI` and `ORCH_NEO4J_DB`: Neo4j used for projects, phases, tasks, refs, gates, and evidence. Run Neo4j with auth disabled (`NEO4J_AUTH=none`) — the orchestrator connects with no credentials and does not support internal-service auth.

Network posture:

- `ORCH_HOST`: dashboard/API bind host. Default is `127.0.0.1`. Set `ORCH_HOST=0.0.0.0` only as an explicit trusted single-user LAN opt-in.
- `ORCH_PORT`: dashboard/API port. Default is `5002`.
- `ORCH_DASHBOARD_URL`: base URL used by CLIs and hooks. Default is `http://127.0.0.1:5002`.
- `ORCH_AUTH_TOKEN`: optional mutable-endpoint token. When set, `POST`, `PUT`, `PATCH`, and `DELETE` requests require either `Authorization: Bearer <token>` or `X-API-Key: <token>`. Read endpoints remain open. When unset, the default loopback bind is the security boundary.

By default the dashboard binds `127.0.0.1`, reachable only from the machine it runs on. That localhost bind is the security boundary for the mutable API: the dashboard is not a multi-user service and must not accept untrusted callers. Binding to any non-loopback interface is an explicit, deliberate operator opt-in for a trusted single-user network only.

Fleet and context:

- `ORCH_SESSION_IDS`: optional comma-separated session allowlist for the dashboard notify form and wake-packet endpoint.
- `ORCH_NOTIFY_LIB_ROOT`: path to `claude-code-fleet-notify` if it is not importable and not a sibling checkout. Required for managed Claude Code hook wiring and stop discipline.
- `ORCH_NOTIFY_CLI`: notify CLI name. Default is `taey-notify`.
- `ORCH_REF_ALLOWED_ROOT`: one or more trusted roots for plan/source refs. Refs are disabled fail-safe when unset.
- `ORCH_SESSION_ROOTS`: JSON or comma-separated `session=/repo/root` map used by the context assembler to find each session's `MEMORY.md` and repo rules.
- `ORCH_WAKE_PACKET_ENDPOINT_ENABLED`: default `1`; set `0` to disable only the wake-packet API endpoint. Deprecated alias: `ORCH_WAKE_PACKET_ENABLED`.
- `ORCH_RULES_ROOT`: optional rules directory used by the context assembler.
- `ORCH_SHIP_GATES`: comma-separated task-id suffixes that must be complete before a project can be marked shippable.

## What "taey" Means

The `taey-*` commands are the agent-facing CLI tools installed by this package. "Taey" is the project codename used for the local fleet protocol: `taey-plan` works with projects and plans, `taey-task` works with task state, `taey-question` works with human-review gates, and `taey-dispatch` works with out-of-band runner liveness. `orch` is the operator lifecycle command for serving, enabling, disabling, and doctoring the local service.

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
taey-plan ingest /absolute/path/to/plan.md --supervisor supervisor
```

The plan format is documented in [docs/PLAN_FORMAT.md](docs/PLAN_FORMAT.md). Task headers can declare priority, owner, dependencies, and refs. Example:

```md
# Project: demo - Demo

## Phase: build - Build [order: 1]

### Task: demo::build-1 - Verify install [priority: 50] [owner: worker-codex]
Run the smoke test and record evidence.
```

An agent checks and updates tasks:

```bash
taey-task status demo::build-1
taey-task update demo::build-1 completed --evidence '{"commit_sha":"abc123","gate":"curl /health","production_observation":"HTTP 200 ok true"}'
```

A human-review gate records a question that must be answered by a person:

```bash
taey-question create-gate demo::build demo::human-review "Ship this artifact?" --reviewer operator
taey-question answer <question-id> "Ship it" --from supervisor
```

For harness-driven work where the runner is not the agent session itself:

```bash
taey-dispatch out-of-band register demo::build-1 --supervisor supervisor --owner worker-codex --runner acceptance-harness --ttl 300
taey-dispatch out-of-band heartbeat demo::build-1
taey-dispatch out-of-band complete demo::build-1 --status completed --supervisor supervisor --evidence '{"commit_sha":"abc123","gate":"production probe","production_observation":"verified live"}'
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

The operator API and dashboard run on `:5002`; the dashboard is served at `/ui/`, and the API root redirects there. Useful read endpoints:

- `GET /health`
- `GET /api/projects`
- `GET /api/projects/{project_id}`
- `GET /api/sessions`
- `GET /api/sessions/{session_id}/current`
- `GET /api/sessions/{session_id}/next-ready`
- `GET /api/sessions/{session_id}/stop-decision`
- `GET /api/sessions/{session_id}/wake-packet`

Mutable endpoints create and update projects, phases, tasks, questions, stop conditions, loop state, and notifications. Treat `:5002` as a local operator API, not a public service.

There is also a distinct read-only dashboard app:

```bash
scripts/orch-public --port 5005
```

That serves `fleet_orchestrator.public_readonly:app` on `127.0.0.1:5005`. It has no mutable routes and uses explicit public-session/project filters such as `ORCH_PUBLIC_SHOW_SESSIONS`, `ORCH_PUBLIC_HIDE_SESSIONS`, and `ORCH_PUBLIC_HIDE_PROJECT_IDS`. The screenshot above is this `:5005` read-only view; a project title inside it may reflect live fleet project data rather than the package version.

**Note on dashboards:** the embedded screenshot is the read-only dashboard on `:5005`, which renders plan/session/task state only and is safe to share. The mutable operator dashboard on `:5002` additionally surfaces live operator content — including inter-session chat — and must never be screenshotted, exposed, or bound to a non-loopback interface. Treat `:5002` as private operator-only; `:5005` is the shareable surface.

## Refs And Wake Packets

Refs are structured pointers attached to the plan. They are not copied into Neo4j as permanent file contents. At runtime, the assembler reads allowed refs fresh and wraps untrusted content in nonce envelopes designed to keep file text from being interpreted as packet structure. See [Dynamic Context And Tiered Refs](docs/DYNAMIC_CONTEXT_REFS.md) for the five tiers, clear-then-reinject workflow, and empty-context framing.

Enable refs by setting `ORCH_REF_ALLOWED_ROOT` to trusted roots. Without it, ref use fails closed.

Wake packets are enabled by default:

```bash
curl -s "http://127.0.0.1:5002/api/sessions/worker-codex/wake-packet?cli=codex"
```

The response includes `packet` plus `packet_meta` with a provenance hash, size report, snapshot fingerprints, and the generating commit.

## Scope

This is for one operator coordinating their own local fleet on their own machine.

Supported OS scope: Linux is the tested target. macOS may work for the API path, but the installer, process management, shell scripts, and Claude Code hook wiring are written for a Unix-like environment and are not documented as Windows-native.

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

The acceptance tests are standalone scripts (not `unittest`/`pytest` modules) — each is run individually, exactly as the ship gate does. Install the test extra (the tests use the FastAPI TestClient, which needs `httpx`), then run any test as a script. Most need a reachable Neo4j and the same environment as the API; some set `ORCH_TEST_NAMESPACE` to isolate their data. The authoritative list of tests and their per-test environment is [.github/workflows/ship-gate.yml](.github/workflows/ship-gate.yml).

```bash
pip install -e ".[test]"
# Acceptance scripts are standalone (run each directly, not via unittest/pytest).
# Each talks to your orchestrator's Neo4j/Redis and needs a test namespace to
# isolate its data. With your normal orchestrator env active (ORCH_NEO4J_URI
# etc. from .env):
ORCH_TEST_NAMESPACE=local-acceptance python tests/human_review_gate_acceptance.py
```

The authoritative list of acceptance scripts and the exact per-test environment is [.github/workflows/ship-gate.yml](.github/workflows/ship-gate.yml) — the ship gate runs each as its own step with an isolated environment. Run additional scripts the same way (one process each); give each its own `ORCH_TEST_NAMESPACE` to avoid cross-test data collisions.

## More Docs

- [SETUP.md](SETUP.md): operator install flow and lifecycle details.
- [docs/WALKTHROUGH.md](docs/WALKTHROUGH.md): guided first supervised loop.
- [docs/PLAN_FORMAT.md](docs/PLAN_FORMAT.md): markdown plan format.
- [docs/CONFIGURATION.md](docs/CONFIGURATION.md): every environment flag the orchestrator reads — default, what it gates, and its classification. Core accountability (completion-evidence, supervisor keep-going) is hardcoded with no disable flag.
- [AUDIT.md](AUDIT.md): reviewer entry point — audit the code against its stated claims (for any code review of this repo).
- [SECURITY.md](SECURITY.md): security posture and reporting.
