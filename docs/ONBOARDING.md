# Onboarding: run your own fleet on the orchestrator

This guide takes one operator from a clean machine to a Claude Code fleet that
**keeps itself on-task** — sessions don't stop while they have ready work, and
you don't have to cycle through them asking "why did you stop?"

## What you are setting up

This is a **local, single-user, single-machine** orchestrator. One operator
runs it on one box to coordinate *their own* Claude Code sessions (and any
CLI peers). It is **multi-project** — any session can register and run its own
plan without clobbering another's state — but it is **not** multi-tenant, not
hosted, and has **no untrusted callers**. The security boundary is the
localhost bind, not an auth token (see `SECURITY.md`).

The one job it does: a session with incomplete, ready work is **not allowed to
stop**. When a session tries to stop, the stop-discipline engine checks the
project graph; if there is ready work it owns, the Stop hook returns the next
task instead of letting the session go idle. The only sanctioned stops are
(a) genuinely no ready work, (b) blocked on another worker/process with no
parallel work, or (c) a declared stop condition (e.g. needs-human).

## Prerequisites

- **Python 3.10+**
- **Neo4j** reachable on a bolt URI (the project graph lives here). Auth is
  optional — unset credentials means a no-auth connection, which is the normal
  local setup.
- **Redis** (session inbox + idle/active flags + last-outcome cache).
- **Claude Code** with the companion [`claude-code-fleet-notify`](https://github.com/palios-taey/claude-code-fleet-notify)
  hooks installed — the Stop / PreToolUse / PostToolUse / UserPromptSubmit
  hooks are what actually hold a session on-task. The orchestrator is the
  brain; the hooks are the hands.

## 1. Install

```bash
python -m venv .venv && source .venv/bin/activate   # isolate from system Python
pip install .
```

This installs the package and four console entry points:

| Command | Purpose |
|---|---|
| `taey-plan` | register / inspect plans and tasks (`list`, `show`, `current`, `next`, `ingest`, `assign`, `stop-conditions`) |
| `taey-task` | create / list / update individual tasks (`create`, `list`, `status`, `update`) |
| `orch-watch` | the readiness watch loop (wakes a session when a zero-dependency task becomes ready) |
| `orch-cron` | periodic readiness/heartbeat sweep |

Verify the install:

```bash
python -c "import fleet_orchestrator; print(fleet_orchestrator.__version__)"
taey-plan --help
```

## 2. Configure

Configuration is environment-driven and **fails loud** when a required value is
missing — there are no silent defaults to a wrong endpoint. Set these in your
shell profile or a `.env` (point `ORCH_DOTENV` at an explicit file if you keep
more than one):

| Env var | Required | Default | Meaning |
|---|---|---|---|
| `ORCH_NEO4J_URI` | **yes** | — (raises if unset) | bolt URI for the project graph, e.g. `bolt://127.0.0.1:7687` |
| `ORCH_NEO4J_USER` / `ORCH_NEO4J_PASS` | no | unset → no-auth | Neo4j credentials, if your Neo4j requires them |
| `ORCH_NEO4J_DB` | no | `neo4j` | database name |
| `ORCH_REDIS_HOST` / `ORCH_REDIS_PORT` | no | `127.0.0.1` / `6379` | Redis location |
| `ORCH_API_HOST` / `ORCH_API_PORT` | no | `127.0.0.1` / `5002` | tasks API bind (defaults to loopback — the security boundary) |
| `ORCH_DASHBOARD_URL` | no | `http://localhost:5002` | where the CLIs reach the API |

> `ORCH_API_HOST` defaults to `127.0.0.1` deliberately. Binding a routable
> interface exposes the no-auth API to your network — only do it knowingly.

A minimal local `.env` (everything except `ORCH_NEO4J_URI` can be omitted to
take the localhost defaults):

```bash
# .env  (loaded automatically; or point ORCH_DOTENV at it)
export ORCH_NEO4J_URI=bolt://127.0.0.1:7687
# export ORCH_NEO4J_USER=neo4j        # only if your Neo4j requires auth
# export ORCH_NEO4J_PASS=...          # only if your Neo4j requires auth
```

```bash
source .env
```

## 3. Start the tasks API

The CLIs and hooks talk to a small FastAPI service. Schema initialization
(unique constraints) runs automatically on startup.

```bash
python -m uvicorn fleet_orchestrator.tasks_api:app --host 127.0.0.1 --port 5002
```

Confirm it is up and loopback-only:

```bash
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5002/api/projects   # 200
```

## 4. Register your plan

A plan is a markdown file in the format described in
[`PLAN_FORMAT.md`](./PLAN_FORMAT.md) — phases, tasks, owners, priorities, and
dependencies. The markdown is canonical: edit the file and re-ingest; don't
hand-edit the graph fields that came from a plan.

```bash
taey-plan ingest path/to/your_plan.md
taey-plan show your-project-id
```

Optionally declare the conditions under which a *supervisor* session is allowed
to stop (otherwise the only stop is "no ready work"):

```bash
taey-plan stop-conditions your-project-id set blocked-on-worker blocker-found-needs-human release-ready
```

## 5. Run your fleet end-to-end

1. **See what a session should do now**

   ```bash
   taey-plan current            # in-progress work for this session
   taey-plan next               # next ready task this session owns / matches
   ```

2. **The session works the task.** When it finishes, it records evidence —
   a task cannot transition to `completed` without **both** a commit SHA and a
   production observation (self-reported "done" with no evidence is rejected;
   that is the feature that makes the fleet trustworthy unattended):

   ```bash
   taey-task update <task-id> completed \
     --commit-sha <sha> \
     --production-observation "what you observed in production"
   ```

3. **It cannot silently idle.** If the session tries to stop while it still
   owns ready work, the Stop hook blocks the stop and hands it the next task.
   If it is legitimately waiting on another worker, it records that on the
   task it is holding so the wait is intentional rather than a silent stall:

   ```bash
   taey-task update <task-id> in_progress \
     --blocked-on "waiting on <worker> to finish <task>; no parallel work"
   ```

   A worker reporting back (via the fleet-notify inbox) wakes the session and
   the loop continues — no human in the loop. (At the project level you can
   also declare allowed supervisor stop conditions with
   `taey-plan stop-conditions <project-id> set ...`, per step 4.)

## 6. The integrity gate (the differentiator)

The repo ships a mechanical gate (`tools/lint_no_silent_fallbacks.py`, a
pre-commit hook, and a CI workflow) that blocks silent-fallback patterns —
bare `except: pass`, hardcoded operator paths, swallowed subprocess failures.
"Works or fails loud" is enforced by the gate, not by discipline. Run it
anytime:

```bash
python tools/lint_no_silent_fallbacks.py --all
```

## Where to go next

- `PLAN_FORMAT.md` — the full plan schema (phases, deps, owners, priorities, stop-conditions).
- `ARCHITECTURE.md` / `SCHEMA.md` — how the engine and graph fit together.
- `SECURITY.md` — the local-trust model and what changes if you expose the API.
