# claude-code-fleet-orchestrator

> Turn scattered AI terminals into a supervised tmux fleet: dispatch work to Claude Code / Codex / Gemini / Grok / any **hookable** REPL CLI, get `done`/`error`/`interrupted` outcomes back inline so the supervisor can update the plan instead of babysitting panes.

Current version: **v1.0.5** (zero-dep owned tasks now wake their idle owner at creation time, and `dispatch()` now claims ready OrchTasks before any Redis `current_task` write so stale blocked tasks cannot be dispatched — see [`docs/STATUS.md`](docs/STATUS.md)).

Built on top of [`claude-code-fleet-notify`](https://github.com/palios-taey/claude-code-fleet-notify) (≥ v1.0.0), which provides the message transport (Redis inbox, daemon, tmux-send, per-CLI Stop hooks). This repo adds the supervisor-worker coordination layer.

**Scope**: terminal-native hookable REPL CLIs only — IDE-embedded agents (Cursor, etc.) and many-to-many distributed topologies are NOT in scope; see [fleet-notify's scope section](https://github.com/palios-taey/claude-code-fleet-notify#scope-what-this-is-and-what-this-isnt).

## Why

You spawn a worker CLI in another tmux pane (codex, gemini, grok, a second Claude Code, anything driven by a REPL prompt). You dispatch a task. Then nothing — the supervisor session doesn't know if the worker received the task, doesn't know when it starts, doesn't know when it finishes, doesn't see the outcome inline. So you keep tabbing between panes, or you give up and write everything from one session.

This product closes that gap with one primitive: **the worker's Stop hook is the universal notifier**. Don't trust the worker to call `taey-notify` manually — make the Stop hook do it for every CLI, with the completed task's content embedded in the notify body (the hook implementation lives in `fleet-notify`; this package adds the dispatcher-side `current_task` write so the hook has something to report).

Layered on top: an event-driven watchloop (Redis keyspace listener — fires only on state changes, no poll spam) and a recurring-task runner with file-tracked state + hash-on-fire provenance.

## What's shipped (v0.4.x)

| Component | Purpose | Phase |
|---|---|---|
| `lib/dispatch.py` | `dispatch()` / `record_outcome()` / `check_previous_task()` / `clear_current_task()`. Writes `taey:<worker>:current_task` atomically with stale-outcome + stuck-dedup clear, performs the spec §3.1 bug-lock pre-check before any worker-state mutation, and as of `v1.0.5` conditionally claims OrchTasks in Neo4j before the Redis write so blocked tasks cannot slip through on a stale readiness snapshot. | A — v0.1.0, updated in v1.0.5 |
| `scripts/orch-watch` | Event-driven supervisor wake daemon. PSUBSCRIBE on `current_task` / `idle` / `last_activity` keyspace notifications + 30-min safety-net sweep. Fires high-priority `peer_idle` escalations on stuck workers (idle + unresolved `current_task` for > threshold) and optional `wake` messages on done-DEL when a configurable readiness-checker says the completion unblocked an OrchTask the supervisor owns. | B — v0.2.0 / v0.2.1 |
| `scripts/orch-cron` | Recurring-task runner. Drop-in replacement for static `recurring_triggers.json`-style cron runners. Adds optional `state_file` per trigger (append-only JSONL audit log) + SHA-256 hash-on-fire sidecar (`<state_file>.meta.json`) so the file pointer is tamper-evident. | C — v0.3.0 |
| `docs/SCHEMA.md` | Task model spec. One `OrchTask` label, kind-aware status enum (`one_shot` ∈ {pending,in_progress,completed,failed,blocked}; `recurring` ∈ {active,paused,retired} — NEVER completed); reserves `(:OrchTask)-[:FIRED]->(:OrchRecurringFire)` for v0.4+ per-fire visibility. | C — v0.3.0 |

The Stop hook itself lives in [`claude-code-fleet-notify`](https://github.com/palios-taey/claude-code-fleet-notify) (`hooks/_shared.py:action_stop` + per-CLI hook variants for Claude Code / codex / gemini; Grok inherits Claude Code automatically). This package is the dispatcher-side counterpart that writes the keys the hook reads.

## What shipped in v0.4.0 (Phase D — plan tracker + default readiness checker)

| Component | Purpose |
|---|---|
| `lib/orch_schema.py` | Neo4j schema implementation of [`docs/SCHEMA.md`](docs/SCHEMA.md): OrchProject ↔ OrchPhase ↔ OrchTask DAG with kind-aware status, dependency ready-task discovery, phase-completion cascade, session current/next-ready, and creation-time zero-dep wake for idle owners. |
| `lib/plan_loader.py` | Markdown plan ingest (idempotent, content-hash provenance). |
| `lib/tasks_api.py` | FastAPI app on `:5002` — `/api/tasks`, `/api/projects`, `/api/projects/load-md`, `/api/sessions/{sid}/current\|next-ready`. |
| `lib/config.py` | `OrchConfig` + Redis/Neo4j connection helpers; path-flexible `.env` loading. |
| `lib/plan_readiness.py` | **Default readiness checker** for `orch-watch --readiness-checker`. LOOSE semantic (wake only on blocked→ready transition); self-loop exclusion; SETNX dedup for concurrent finals. |
| `scripts/taey-plan` | CLI: project list / show / current / next-ready / ingest-md / assign. |
| `scripts/taey-task` | CLI: task create / update / list / delegate. |

### Default readiness checker

`orch-watch` v0.2.1+ accepts `--readiness-checker module_path_or_file:function`. Wiring it to the v0.4.0 default:

```bash
orch-watch \
    --readiness-checker /path/to/claude-code-fleet-orchestrator/lib/plan_readiness.py:check_readiness
```

Now when a worker finishes cleanly (Stop hook CAS-clears `current_task` on outcome=done), `orch-watch` queries Neo4j: does any OrchTask owned by the supervisor have a `DEPENDS_ON` edge to the completed task AND all OTHER deps already complete? If yes AND supervisor is idle, page them. LOOSE semantics, so a task with N deps completing in sequence wakes the supervisor exactly once (on the Nth completion, not all N).

> **v1.0.5 note**: the zero-dep wake gap from the original v0.4.1 follow-up plan is now closed. The remaining queued edge from that note is the already-completed-deps-at-edge-creation wake in `add_dependency`; the May 28 dispatch instead prioritized a stricter dispatch-time ready-claim guard so blocked tasks cannot be assigned from a stale view.

## Install

```bash
# 1. Install the transport dependency first (covers Claude Code / codex / gemini / grok hooks)
git clone https://github.com/palios-taey/claude-code-fleet-notify.git
cd claude-code-fleet-notify
sudo make install
bash scripts/install-hooks.sh --all --apply
bash scripts/start_notify_daemons.sh start

# 2. Install this orchestrator
cd ..
git clone https://github.com/palios-taey/claude-code-fleet-orchestrator.git
cd claude-code-fleet-orchestrator

# 3. Enable Redis keyspace notifications for orch-watch
redis-cli CONFIG SET notify-keyspace-events 'Kgl$'
redis-cli CONFIG REWRITE   # persist

# 4. Start orch-watch (one per machine)
python3 scripts/orch-watch --redis-host 127.0.0.1 &
```

## Usage

```python
from lib.dispatch import dispatch, record_outcome, check_previous_task, clear_current_task

# Supervisor side
prev = check_previous_task('treasurer-codex')
if prev:
    # Previous dispatch did not complete cleanly (outcome != done left
    # current_task in place). Decide: retry, investigate, or cancel.
    ...

dispatch(
    worker='treasurer-codex',
    task_id='scout-cycle-22',
    description='Scout r/MachineLearning for acute-pain replies',
    supervisor='treasurer',     # written to taey:treasurer-codex:parent
)

# Worker side, just before stopping
record_outcome('treasurer-codex', 'done', 'found 3 qualifying targets, posted 2 replies')

# When the worker stops, its Stop hook (in fleet-notify) reads current_task
# + last_outcome and pushes a single peer_idle message to the supervisor's
# inbox with outcome inline. Zero context-switch.
```

```bash
# Recurring tasks via JSON registry (orch-cron)
cat > /etc/orch/recurring.json <<EOF
{
  "triggers": [{
    "id": "x-claude-cycle",
    "session": "x-claude",
    "tz": "America/New_York",
    "minute": 9,
    "hours": [8, 10, 12, 14, 16, 18, 20, 22],
    "prompt_file": "/path/to/repo",
    "state_file": "/var/log/orch/x-claude.jsonl",
    "enabled": true,
    "status": "active"
  }]
}
EOF

# Run from system cron (every minute) — exact-minute match
* * * * * /usr/local/bin/orch-cron --registry /etc/orch/recurring.json
```

## License

Apache-2.0
