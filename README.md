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

- `ORCH_REDIS_HOST` — Redis host for the orchestrator API and watcher.
- `ORCH_REDIS_PORT` — Redis port for the orchestrator API and watcher.
- `ORCH_NEO4J_URI` — Neo4j Bolt URI, for example `bolt://127.0.0.1:7687`.
- `ORCH_NEO4J_DB` — Neo4j database name.
- `ORCH_DASHBOARD_URL` — base URL for the API and browser UI, for example `http://127.0.0.1:5002`.

Optional variables:

- `ORCH_REDIS_SENTINELS` — comma-separated Redis Sentinel `host:port` pairs.
- `ORCH_REDIS_SENTINEL_MASTER` — Sentinel master name; defaults to `orch-master`.
- `ORCH_NEO4J_USER` / `ORCH_NEO4J_PASS` — Neo4j credentials when auth is enabled.
- `ORCH_NOTIFY_LIB_ROOT` — path to a local `claude-code-fleet-notify` checkout when `identity` is not already importable.
- `ORCH_NOTIFY_CLI` — override the notify CLI binary; defaults to `taey-notify`.
- `ORCH_REF_ALLOWED_ROOT` — trusted root, comma-separated roots, or a JSON list of roots under which plan source files must live before `[ref:...]` slices are enabled.
- `ORCH_SESSION_IDS` — optional allowlist for the browser notify form target validation. When set, `POST /api/sessions/{target}/notify` rejects targets not listed here.
- `ORCH_PRODUCT_OWNER_MAP` — optional session-to-product map used by `lib.dispatch` bug-lock enforcement. Accepts JSON or comma-separated `session=product` pairs. `PRODUCT_OWNER_MAP` is also accepted as a fallback alias.
- `ORCH_DOTENV` — explicit `.env` file path to load before config validation.

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

## Run the API / Dashboard

The simplest way to bring up the dashboard is:

```bash
orch serve        # foreground, Ctrl-C to stop
# or, for a persistent background service:
orch enable
```

Both read `ORCH_HOST` / `ORCH_PORT` from your `.env`, so you configure once and
launch with one command. To verify:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5002/api/projects
```

(You can still run uvicorn directly — `python3 -m uvicorn lib.tasks_api:app --host "$ORCH_HOST" --port "$ORCH_PORT"` — but `orch serve` is the supported path.)

### Serve the dashboard on your own network

By default the dashboard binds `127.0.0.1` — reachable only from the machine it
runs on. To open it to other devices on your LAN (phone, laptop, another
workstation), set the bind interface in `.env` and relaunch:

```bash
# .env
ORCH_HOST=0.0.0.0          # all interfaces; or a specific LAN IP like 10.0.0.5
ORCH_PORT=5002
ORCH_DASHBOARD_URL=http://10.0.0.5:5002   # a reachable address, kept in sync
```

Then `orch serve` (or `orch enable`) and open `http://<your-lan-ip>:5002/ui/`
from any device on the network. **There is no authentication** — this is a
single-user, local product — so only expose it on a network you trust, with no
inbound port-forward from the internet.

### Two-way chat box

The dashboard includes a floating chat bar to message a session and read the
scrollable reply/escalation history. It is **off by default** because chat
messages become context an AI session reads (an injection vector). Enable it
only on a trusted/contained network:

```bash
# .env
ORCH_CHAT_ENABLED=1
```

### Web UI / Dashboard URL

The API root redirects to the browser UI, and the static dashboard is served at
`${ORCH_DASHBOARD_URL}/ui/`. With the default `.env.example`, that is
`http://127.0.0.1:5002/ui/`.

### Public read-only dashboard (v1.6.0+)

A separate, **read-only-by-construction** app (`lib/public_readonly.py`, launched via `scripts/orch-public`, default `127.0.0.1:5005`) serves the same session-first view safely for public exposure behind a single tunnel route. It defines **only GET routes** — no create/update/notify endpoint exists in the app, so a write request is a 404/405 because the route is absent, not guarded. It applies a **fail-closed session allowlist** (`ORCH_PUBLIC_SHOW_SESSIONS`, default approved sessions only; everything unassigned/denied is hidden), an **outbound field allowlist** that scrubs operator filesystem paths and hosts from free-text and drops source paths / internal-ops fields, serves **ref pointers only** (never file contents), sanitizes `/health`, and disables the interactive API docs. Never point a tunnel at the live mutable `:5002` API — use `:5005`.

Observed in [`ui/static/app.js`](ui/static/app.js) and [`ui/index.html`](ui/index.html):

- The UI is session-first and auto-refreshes every `5000` ms.
- The session strip is currently hardcoded to these sessions:
  - `conductor`
  - `weaver`
  - `tutor`
  - `infra`
  - `taeys-hands`
  - `treasurer`
  - `hunter`
  - `taey-ed`
  - `x-claude`
- Each session card shows the current in-progress task and the next ready task from:
  - `GET /api/sessions/{session}/current`
  - `GET /api/sessions/{session}/next-ready`
- The projects panel is filtered by the selected session and populated from the supervisor-scoped `GET /api/sessions/{session}/projects` listing.
- Project cards show phase count, task total, and pending / in-progress / completed / failed counts.
- Project detail shows phases plus a task table with task id, status, owner, priority, and blocked-on.
- The footer form sends typed messages to the selected session through `POST /api/sessions/{session}/notify`.
- The browser exposes four notify types: `standard`, `escalation`, `command`, and `response_ready`.
- The pause checkbox freezes UI auto-refresh only. It does not pause sessions or the stop-discipline engine.

`ORCH_SESSION_IDS` does not populate the session cards. It only constrains browser notify targets on the API side. Changing the visible session strip currently requires editing `ui/static/app.js`.

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

## Plan refs

Plans can attach repeatable file refs on project, phase, or task headers:

```md
### Task: sample-task - Verify install [priority: 50] [ref: docs/PLAN_FORMAT.md:1-40]
```

Observed in [`lib/orch_schema.py`](lib/orch_schema.py):

- refs are stored as structured metadata, not copied into Neo4j as file contents
- file slices are read fresh when the orchestrator builds runtime `ref_context` payloads
- refs require `source_path` on ingest when refs are present
- refs are disabled unless `ORCH_REF_ALLOWED_ROOT` is configured
- relative refs are sandboxed under both the plan file's directory and one of the configured allowed roots
- absolute paths, `~` paths, control characters, `..` escapes, and symlink escapes are rejected
- oversized or unreadable files degrade to warnings instead of crashing the request path

## Core loop — how a supervisor uses it

The orchestrator runs one loop per supervisor session (a Claude Code / CLI instance):

1. **Plan** — author a markdown plan (`# Project` / `## Phase` / `### Task` with
   `[priority]`, `[owner]`, `[depends]`, `[ref]`) and `taey-plan ingest <file>`.
   The markdown is the source of truth; re-ingest after edits (idempotent).
2. **Pull ready work** — `taey-plan next [session]` returns the top ready task you
   own. A task is *ready* only when its `[depends]` predecessors are `completed`
   (engine-enforced via `DEPENDS_ON` edges) — a phase can't start until its
   prerequisites close.
3. **Dispatch** — hand work to a peer worker with `lib.dispatch.dispatch(...)`. It
   claims the task (`in_progress`), writes the worker's `current_task`, and the
   notify daemon injects the prompt when the worker is idle.
4. **Wake** — when a worker stops, its Stop hook notifies the supervisor; the
   daemon injects the message when the supervisor is idle. The supervisor wakes
   with the result — no human relay.
5. **Stop-discipline** — a session must not stop while ready work exists;
   `blocked_on` is the only valid wait, and a stop cites a `user_stop_condition`.
6. **Ship** — a project is shippable only when its ship-gate tasks pass (below).

The dashboard (`orch serve` → `/ui/`) shows every session's current + next task,
the project plans with clickable drill-down refs, and a two-way chat per session.

## Dynamic context + automation features

| Feature | What it does | Entry point |
|---|---|---|
| **Refs (5-level)** | `[ref: path:Lx-Ly]` at overall/supervisor/project/phase/task level; the dashboard renders clickable pointers that drill down to the live file lines + provenance. | plan `[ref:]` + `ORCH_REF_ALLOWED_ROOT` |
| **Context assembler** | Per-task wake packet: selects relevant MEMORY + refs + rules, renders to a per-CLI token budget, stamps a content-bound provenance hash. | `orch-assemble <session> [task] --cli claude\|codex\|gemini\|grok` |
| **Loop engine** | Loops that advance on *validated artifact presence* (never self-report); rejects unmeetable stop-conditions at declaration. | `orch-loop` / `lib.loop_engine` |
| **Two-way chat** | Per-session dashboard chat: send delivers to the session inbox + stores history; expand/collapse; permanent-stop escalations surface as a "needs you" badge. OFF by default. | dashboard chat bar + `ORCH_CHAT_ENABLED` |
| **Decision receipts** | Each stop/chat/wake/cycle/assembly can emit a content-hashed receipt (`why_this_context`, `refs_used`, `observable_state_hash`). | `lib.decision_receipt` |
| **Rules tier** | Supervisor- + project-level standing rules auto-loaded by the assembler; promotable from chat. | `lib.rules_tier` |
| **Project template** | Auto-injects the sub-role gate scaffold (scout → code → audit → verify → family) as `depends`-encoded gate tasks. | `lib.orch_template` |
| **Shippability gate** | A project is shippable only when every `-prodtest`/`-audit` gate task is completed; `POST /api/projects/{id}/ship` returns 409 otherwise. No human-approval override. | `GET/POST /api/projects/{id}/shippability\|ship` · [docs/SHIPPABILITY.md](docs/SHIPPABILITY.md) |

## Run the watcher

```bash
orch-watch \
  --redis-host 127.0.0.1 \
  --readiness-checker lib/plan_readiness.py:check_readiness
```

## `taey-task`

`taey-task` is the ad hoc task CLI. It talks to the dashboard API and is separate from markdown plan ingestion.

Observed in [`scripts/taey-task`](scripts/taey-task):

- `taey-task create "<description>"` — create a new task through `POST /api/task/create`
- `taey-task list` — list ranked ready tasks from `GET /api/tasks/ranked`
- `taey-task status <task-id>` — inspect a task through `GET /api/tasks/{task_id}`
- `taey-task update <task-id> <status>` — patch task state through `PATCH /api/task/{task_id}`

Additional observed flags:

- `create --priority <int>`
- `create --from <sender>`
- `create --type standard|micro`
- `update --blocked-on <value>`
- `update --clear-blocked-on`

Use `taey-plan` when the work belongs in a markdown-backed project/phase/task plan. Use `taey-task` for direct task creation and state updates through the API.

## Companion products

The orchestrator is the core. A few small, separately released products compose with it. Start minimal: orchestrator + notify + `orch doctor` green. Add the others only when you hit the specific problem they solve.

### Required

- **[claude-code-fleet-notify](https://github.com/palios-taey/claude-code-fleet-notify)** — the hook, daemon, Redis inbox, and CLI layer the orchestrator depends on. `scripts/install` wires its hooks for you when notify is installed as a sibling checkout or when `ORCH_NOTIFY_LIB_ROOT` points at it.

### Recommended (manual install)

- **[claude-code-api-watchdog](https://github.com/palios-taey/claude-code-api-watchdog)** — surfaces Claude Code API stalls and failures instead of leaving a wedged session silent.
- **[mcp-reconnect](https://github.com/palios-taey/mcp-reconnect)** — keeps MCP connections alive across disconnects.

### Optional

- **[restart-safe-agents](https://github.com/palios-taey/restart-safe-agents)** — patterns for agents that survive restart without losing in-flight work.
- **[claude-code-fleet-cockpit-template](https://github.com/palios-taey/claude-code-fleet-cockpit-template)** — the start-here template for the shared fleet operating spine: routing, recaps, action logs, prompting standards, 6SIGMA workflow, per-CLI orientation, and cron registry for teams running the released claude-code-fleet products.

### Planned, not yet released

- A one-command suite installer is still planned but not yet published. Today, `scripts/install` wires the required notify integration only; the optional companions above still need manual installs.

## Documentation

- [docs/SCHEMA.md](docs/SCHEMA.md) — Neo4j/Redis data model
- [docs/PLAN_FORMAT.md](docs/PLAN_FORMAT.md) — markdown plan spec (project/phase/task/refs)
- [docs/SHIPPABILITY.md](docs/SHIPPABILITY.md) — the ship-gate definition + enforcement
- [SUPPORT.md](SUPPORT.md)
- [SECURITY.md](SECURITY.md)

## License

Apache-2.0
