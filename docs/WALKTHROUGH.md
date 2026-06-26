# Walkthrough — setup and a first supervised-loop exercise

A guided path: install → configure → define a plan → manually exercise one supervised loop →
observe → release through the ship-gate. Each step says **what to run** and **what you should see**,
so you can validate as you go. For reference detail see [README.md](../README.md),
[SETUP.md](../SETUP.md), [docs/PLAN_FORMAT.md](PLAN_FORMAT.md), [docs/SHIPPABILITY.md](SHIPPABILITY.md).

> **What you are setting up.** One *local, single-user* coordinator. It tracks your work as a
> plan (project → phases → tasks) in Neo4j, hands ready tasks to worker sessions, wakes a
> supervisor when a worker finishes, refuses to let a session stop while ready work exists, and
> refuses to call a project "released" until its gate tasks pass. There is **no auth** — it trusts
> the local machine. Do not expose the mutable API (`:5002`) to an untrusted network.

---

## Step 0 — Prerequisites

- Python 3.10+ with the stdlib `venv` module (Debian/Ubuntu: `python3-venv`).
- Redis and Neo4j reachable locally (the bundled local examples use `127.0.0.1:6379` and `127.0.0.1:7687`), **or** let the
  installer bring them up via Docker.
- A sibling checkout of [`claude-code-fleet-notify`](https://github.com/palios-taey/claude-code-fleet-notify)
  (the hook/daemon/inbox layer), or `ORCH_NOTIFY_LIB_ROOT` pointing at one.
- Claude Code installed (uses `~/.claude/settings.json`).

**Expect:** `python3 --version` ≥ 3.10; the notify repo present beside this one.

---

## Step 1 — Install

Preview first (writes nothing):

```bash
scripts/install --dry-run
```

**Expect:** a printed install plan + a Claude-settings diff, no files changed.

Then install. If Redis/Neo4j are already running, skip Docker:

```bash
scripts/install --skip-compose     # BYO infra
# or
scripts/install                    # let Docker bring up Redis + Neo4j
```

**Expect (install flow):** venv created → package installed → Claude settings + notify hooks wired
when the notify checkout is available → delegated notify daemon started → orchestrator services
started → `orch doctor` runs at the end.

Verify:

```bash
python3 -c "import fleet_orchestrator; print(fleet_orchestrator.__version__)"
scripts/orch doctor --explain-scope
```

**Expect:** a version string, and doctor reporting **green** on: Redis PING, Neo4j query, env
validation, `/health`, the managed Claude deny entries (exactly once), the installed hooks (exactly
once), the stop-decision round trip, the notify daemon, and `orch-watch` running. Anything red here
is a setup problem to fix *before* going further — doctor is your single source of truth for "is the
substrate healthy."

---

## Step 2 — Configure

`scripts/install` seeds `.env` from [.env.example](../.env.example). The five you must get right:

```bash
ORCH_REDIS_HOST=127.0.0.1
ORCH_REDIS_PORT=6379
REDIS_HOST=127.0.0.1
REDIS_PORT=6379
ORCH_NEO4J_URI=bolt://127.0.0.1:7687
ORCH_NEO4J_DB=neo4j
ORCH_DASHBOARD_URL=http://127.0.0.1:5002
```

Enable the optional features you intend to use:

- **Plan refs** (clickable file-slice pointers): set `ORCH_REF_ALLOWED_ROOT=/abs/path/to/your/repos`
  (one path, comma-list, or JSON list), or set `ORCH_SESSION_ROOTS` so each session's
  repo root is auto-derived as an allowed ref root. Refs are *disabled* until one of
  those sources yields an allowed root.
- **Dashboard network exposure**: default `ORCH_HOST=127.0.0.1` is the security boundary. Any
  non-loopback bind or LAN URL is an explicit, deliberate opt-in only for a trusted single-user
  network. Set `ORCH_AUTH_TOKEN` for non-loopback mutable access, or set
  `ORCH_ALLOW_UNAUTH_NON_LOOPBACK=1` to explicitly acknowledge tokenless trusted-LAN exposure.
  Do not accept untrusted callers.
- **Two-way chat** is on by default. It is an injection vector, so keep the API loopback-only or use `ORCH_AUTH_TOKEN` on a trusted LAN; set `ORCH_CHAT_ENABLED=0` only to hide the route intentionally.

**Expect:** re-run `scripts/orch doctor` after edits; still green.

---

## Step 3 — Define your plan (the planning exercise)

This is the part you author. A plan is one markdown file: exactly one **Project**, one or more
**Phases** (ordered), and **Tasks** under them. The bracketed metadata is where the orchestration
behavior comes from. Full spec: [docs/PLAN_FORMAT.md](PLAN_FORMAT.md).

```md
# Project: my-thing — My Thing

## Phase: build — Build it  [order: 1]

### Task: scaffold — Stand up the skeleton            [priority: 10] [owner: worker-a] [tags: code]
### Task: feature — Implement the feature             [priority: 20] [owner: worker-a] [tags: code] [depends: scaffold]
### Task: docs — Write the docs                       [priority: 30] [owner: worker-a] [tags: docs] [depends: feature]

## Phase: release — Release it  [order: 2]

### Task: prodtest — Full production run              [priority: 10] [owner: worker-a] [tags: prodtest] [depends: docs]
### Task: audit — Full-code audit + sign-off          [priority: 20] [owner: worker-a] [tags: audit] [depends: prodtest]
```

> **Task ids are scoped to your project.** You write plain ids (`scaffold`, `audit`, …); the
> orchestrator stores them as `<project-id>::<id>` (e.g. `my-thing::audit`), so a *second* project can
> reuse the same generic ids with zero collision. `[depends: <id>]` resolves within the same project;
> to depend on another project's task, write its full `other-project::task` id. The dashboard/CLIs show
> and accept the scoped id.

**What to define, and why it matters:**

| Field | Required? | What it controls |
|---|---|---|
| `# Project:` id + name | yes | the project node; one per file |
| `## Phase:` id + name | yes | grouping; `[order: N]` sets phase sequence |
| `### Task:` id + description | yes | the unit of work |
| `[owner: <session>]` | optional | which session pulls/owns the task |
| `[priority: <int>]` | optional | ranking among ready tasks (lower = higher) |
| `[depends: a,b]` | optional | **the gate** — the task stays unready until `a` and `b` are `completed` |
| `[tags: ...]` | optional | capability tags; gate tasks use `prodtest` / `audit` |
| `[ref: path]` / `[ref: path:Lx-Ly]` | optional | clickable whole-file or file-slice pointer in the dashboard (needs an allowed ref root from `ORCH_REF_ALLOWED_ROOT` or `ORCH_SESSION_ROOTS`) |

**Design tip:** make the last tasks of a project its release gate (a `prodtest` task + an `audit`
task) and wire everything else to `depends` into them. That is what makes "shippable" unreachable
without the validation steps actually closing (Step 7).

---

## Step 4 — Ingest and verify the gating

```bash
taey-plan ingest /path/to/my-thing.md
taey-plan list
```

**Expect:** `phases_created=2 tasks_created=5 errors=0`. The source file does not move — it's hashed
for provenance and loaded into Neo4j. Re-ingesting after edits is **idempotent** (updates in place;
tasks deleted from the markdown are reported as `stale_tasks`, not auto-removed).

Now prove the dependency gate works:

```bash
taey-plan next worker-a
```

**Expect:** only **`scaffold`** comes back — it's the one task with no `depends`. `feature`, `docs`,
and the two gate tasks are *not* offered until their predecessors complete. That is the engine
enforcing the DAG, not a convention.

---

## Step 5 — Run the dashboard and observe

```bash
scripts/orch serve          # foreground, Ctrl-C to stop
# or: scripts/orch enable   # background service
```

Open `http://127.0.0.1:5002/ui/`.

**Expect:** a session-first board (auto-refresh ~5s). Each session card shows its current
in-progress task and next ready task; selecting a session filters its projects; project detail shows
phases + a task table (status / owner / priority / blocked-on); `[ref:]` pointers are clickable and
drill down to live file lines. The **pause** checkbox only freezes the UI refresh — it does *not*
pause sessions or the stop engine.

> Note: the visible session strip is loaded from `GET /api/sessions`. For the public read-only
> surface, `ORCH_PUBLIC_SHOW_SESSIONS` controls the allowlist.

---

## Step 6 — The supervisor loop

This is the cycle a supervisor session follows (see README "Core loop"). In this walkthrough you
exercise the cycle manually; it does not create an autonomous always-running supervisor by itself.

1. **Pull** — `taey-plan next <session>` → the top ready task you own.
2. **Dispatch** — hand it to a worker: `fleet_orchestrator.dispatch.dispatch(worker, task_id, description, ...)`.
   It claims the task (`in_progress`), writes the worker's `current_task`, and the notify daemon
   injects the prompt when that worker is idle. If another dispatcher already has a live
   `current_task` bound to that worker, dispatch refuses with `worker busy with <dispatcher>:<task> (status)`;
   use `taey-task dispatch --force` only when you deliberately intend to replace that binding.
3. **Wake** — when the worker stops, its Stop hook notifies the supervisor; the daemon injects the
   result when the supervisor is idle. With hooks and the daemon healthy, no manual relay is needed
   for that wake.
4. **Stop-discipline** — a session must not stop while ready work exists. Intentional waits must use
   the structured `blocked_on` form `AWAIT:<kind>:<detail>` with kind `human-review`, `family-consent`,
   or `external-signal`; free-text `blocked_on` is informational only and worker-liveness may expire it
   back to pending. For cross-session cascades, use `AWAIT:external-signal:<id>` and clear it when the
   external executor lands.
5. **Watcher** — run `orch-watch --redis-host 127.0.0.1 --readiness-checker fleet_orchestrator.plan_readiness:check_readiness`
   so a supervisor is paged the moment a worker's completion unblocks its work, or a task goes stuck.

**Expect:** completing a task flips the next dependent task to ready (watch `taey-plan next` or the
dashboard). Try to stop a session with ready work and the stop-discipline engine blocks the stop and
tells you why.

**Mark work done — with evidence:**

```bash
curl -s -X PATCH http://127.0.0.1:5002/api/task/scaffold \
  -H 'Content-Type: application/json' \
  -d '{"status":"completed","evidence":{"commit_sha":"<sha>","repo":"OWNER/REPO","production_observation":"<what you observed>"}}'
```

**Expect:** `{"ok": true, ...}` and `feature` becomes the next ready task. (Evidence is recorded as
task metadata; it is what the release gate in Step 7 reads.)

---

## Step 7 — Release through the ship-gate

A project is **not** shippable on your say-so. It is shippable only when every gate task passes.
Gate tasks are matched by their project-local name (`ORCH_SHIP_GATES`, default `prodtest,audit`) — so
the `prodtest` / `audit` tasks from Step 3 (stored `my-thing::prodtest`, `my-thing::audit`) are the gates.

```bash
# before the gate tasks are completed:
curl -s -X POST http://127.0.0.1:5002/api/projects/my-thing/ship
```

**Expect:** **409** — refused, listing the gate tasks not yet passed. There is no human-approval
override.

Complete the gate tasks (each with its evidence — the production run for the `prodtest` gate, the
independent audit sign-off for the `audit` gate), then:

```bash
curl -s -X GET  http://127.0.0.1:5002/api/projects/my-thing/shippability   # verdict dict
curl -s -X POST http://127.0.0.1:5002/api/projects/my-thing/ship           # verdict-only POST
```

**Expect:** `shippability` reports each gate satisfied; `ship` returns a self-describing verdict
(`action:"verdict"`, `shipped:false`) and does not persist shipped state. See
[docs/SHIPPABILITY.md](SHIPPABILITY.md) for the gate definition.

> **Coming next release:** the gate is being extended so you *define the steps per project* in the
> plan (`[ship-gates: prodtest, audit, …]`) and each step demands typed evidence — the `audit` gate's evidence
> must come from an identity *other than the task's owner* (read from the ledger, not typed). That
> raises the bar from "mark it done" to "forge multiple independent artifacts." Honest scope: it is
> strong structural deterrence + a tamper-evident record, not cryptographic un-forgeability.

---

## When something's wrong

`scripts/orch doctor --explain-scope` is the first stop — it does real connectivity checks (Redis
PING, Neo4j query), confirms the hooks/daemon/watcher are installed and running, and round-trips a
stop decision. Stop hooks are intentionally **fail-open**: if the local API is down, a session can
still stop (availability over enforcement on a dev box) — so a "stop wasn't blocked" symptom often
means the API isn't reachable; check doctor.
```
