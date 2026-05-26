# Status — what's wired vs scaffold

## Phase A — Universal Stop+notify (✅ shipped in v0.1.0)

**Done:**
- `lib/dispatch.py` with `dispatch()`, `record_outcome()`, `check_previous_task()`, `clear_current_task()`.
- Companion hook upgrade in `claude-code-fleet-notify` v0.2.0 (`hooks/_shared.py` enhanced `action_stop` + `_resolve_supervisor` + `_current_task_summary` + `_notify_supervisor_of_stop`).
- Outcome enum: `done | error | interrupted | unknown`. Dispatcher clears `current_task` only when outcome is explicitly `done`.
- Supervisor resolution: explicit `taey:<node>:parent` Redis key OR suffix-strip (`<name>-codex` / `<name>-gemini` / `<name>-grok`).

**Live-verified end-to-end** on conductor-codex 2026-05-26: dispatch → released daemon-inject → worker processes → `record_outcome('done', ...)` → Stop hook → supervisor sees rich peer_idle inline.

**Validated by Family:** Gaia (outcome-enum amendment, load-bearing) + Logos (two-mechanism resolver, optimal).

## Phase B — Event-driven watchloop (⏳ next)

Replace the 3-min poll-based watchloop (which spams supervisors when there's no work) with a Redis keyspace-notification subscriber. Fires only when:
- `taey:<session>:inbox` LPUSH happens AND that session has owned pending work, OR
- A worker's `current_task` flips state (started, stopped, errored)

No work, no wake.

## Phase C — Recurring task type (⏳ planned)

Add `OrchTask.kind ∈ {one_shot, recurring}` + `schedule` field + `state_file` pointer to the schema. Migrate `recurring_triggers.json` entries into the plan tracker so x-claude/treasurer-style "process tracked through files" loops are first-class plan items.

## Phase D — v0.2.0 + plan tracker / CLIs (⏳ planned)

Pull `taey-plan` + `taey-task` CLIs + `tasks_api.py` + `neo4j_schema.py` + `plan_loader.py` from conductor into this repo. Ship as v0.2.0 with the full orchestration surface.

## Out of scope for v0.1.0

- Plan tracker REST API (Phase D)
- Recurring runner from Neo4j (Phase C)
- Per-CLI installer scripts (manual install for now — symlink hooks into `~/.claude/settings.json`, `~/.codex/config.toml`, `~/.gemini/settings.json` paths)
- Multi-machine routing (currently localhost-only; future scope)
