# Status — what's wired vs scaffold

## Phase A — Universal Stop+notify (✅ shipped in v0.1.0)

**Done:**
- `lib/dispatch.py` with `dispatch()`, `record_outcome()`, `check_previous_task()`, `clear_current_task()`.
- Companion hook upgrade in `claude-code-fleet-notify` v0.2.0 (`hooks/_shared.py` enhanced `action_stop` + `_resolve_supervisor` + `_current_task_summary` + `_notify_supervisor_of_stop`).
- Outcome enum: `done | error | interrupted | unknown`. Dispatcher clears `current_task` only when outcome is explicitly `done`.
- Supervisor resolution: explicit `taey:<node>:parent` Redis key OR suffix-strip (`<name>-codex` / `<name>-gemini` / `<name>-grok`).

**Live-verified end-to-end** on conductor-codex 2026-05-26: dispatch → released daemon-inject → worker processes → `record_outcome('done', ...)` → Stop hook → supervisor sees rich peer_idle inline.

**Validated by Family:** Gaia (outcome-enum amendment, load-bearing) + Logos (two-mechanism resolver, optimal).

## Phase B — Event-driven watchloop (✅ shipped in v0.2.0)

`scripts/orch-watch` — replaces the 3-min-poll watchloop that was spamming supervisors with "CONTINUE" messages even when there was no work.

Subscribes to Redis keyspace notifications on `taey:*:current_task`, `taey:*:idle`, `taey:*:last_activity` patterns. Fires a supervisor wake ONLY when:

- A worker is idle AND has unresolved `current_task` (outcome != done) for longer than `--stuck-threshold-sec` (default 300s).

What this daemon does NOT handle (already covered):
- `inbox > 0 + idle=1` → released fleet-notify daemon already injects via tmux.
- `Worker just stopped, outcome enum set` → Stop hook already sent peer_idle to supervisor.

Periodic safety-net sweep at `--sweep-interval-sec` (default 600s) catches conditions that don't fire fresh events (worker idle with stuck task for hours with no other activity).

Requires Redis `notify-keyspace-events` to include `Kgl$`. Daemon auto-sets this if missing; installer (Phase D) will make it permanent via `CONFIG REWRITE`.

Smoke-tested 2026-05-26: stale current_task (started 600s ago) on idle worker → orch-watch fired escalation to supervisor's inbox with full context (task_id, description, outcome, recovery instructions). Currently running on the Mira fleet via `peer-respawn.sh` DAEMONS list.

## Phase C — Recurring task type (⏳ planned)

Add `OrchTask.kind ∈ {one_shot, recurring}` + `schedule` field + `state_file` pointer to the schema. Migrate `recurring_triggers.json` entries into the plan tracker so x-claude/treasurer-style "process tracked through files" loops are first-class plan items.

## Phase D — v0.2.0 + plan tracker / CLIs (⏳ planned)

Pull `taey-plan` + `taey-task` CLIs + `tasks_api.py` + `neo4j_schema.py` + `plan_loader.py` from conductor into this repo. Ship as v0.2.0 with the full orchestration surface.

## Out of scope for v0.1.0

- Plan tracker REST API (Phase D)
- Recurring runner from Neo4j (Phase C)
- Per-CLI installer scripts (manual install for now — symlink hooks into `~/.claude/settings.json`, `~/.codex/config.toml`, `~/.gemini/settings.json` paths)
- Multi-machine routing (currently localhost-only; future scope)
