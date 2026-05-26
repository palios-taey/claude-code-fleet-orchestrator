# claude-code-fleet-orchestrator

> Tmux-fleet orchestration: supervisor↔worker dispatch, plan/task tracking, recurring schedules, universal Stop+notify across Claude Code / codex / gemini / grok / any tmux-driven REPL CLI.

Built on top of [`claude-code-fleet-notify`](https://github.com/palios-taey/claude-code-fleet-notify) (≥ v0.2.0), which provides the message transport (Redis inbox, daemon, tmux-send). This repo adds the supervisor-worker coordination layer.

## Why

You spawn a worker CLI in another tmux pane (codex, gemini, grok, a second Claude Code, anything driven by a REPL prompt). You dispatch a task. Then nothing — the supervisor session doesn't know if the worker received the task, doesn't know when it starts, doesn't know when it finishes, doesn't see the outcome inline. So you keep tabbing between panes, or you give up and write everything from one session.

This product closes that gap with one primitive: **the worker's Stop hook is the universal notifier**. Don't trust the worker to call `taey-notify` manually — make the Stop hook do it for every CLI, with the completed task's content embedded in the notify body. Supervisors see outcomes without context-switching.

Layered on top: a plan/task tracker (Neo4j-backed OrchProject/Phase/Task DAG), a recurring-task runner (cron-fired but with state tracked in files referenced from the task itself), and an event-driven watchloop (Redis keyspace listener — fires only when state changes, not on a wall clock).

## What's included

| Component | Purpose |
|---|---|
| `hooks/stop_*.py` | Per-CLI Stop hook variants — set idle=1 + notify supervisor with completion content. Claude Code, codex, gemini handled directly; grok inherits Claude Code via `~/.claude/settings.json`. |
| `lib/dispatch.py` | `dispatch(supervisor, worker, task_id, prompt)` — writes worker inbox, writes `taey:<worker>:current_task` so Stop hook has content to report. |
| `lib/orch_schema.py` | Neo4j schema: OrchProject ←HAS_PHASE← OrchPhase ←HAS_TASK← OrchTask, with `kind ∈ {one_shot, recurring}` + `schedule` + `state_file` for x-claude-style file-tracked processes. |
| `scripts/taey-plan` | CLI: project list/show/current/next-ready/ingest-md/assign. |
| `scripts/taey-task` | CLI: task create/update/list/delegate. |
| `scripts/orch-watch` | Event-driven watchloop. Subscribes to Redis keyspace notifications, wakes a supervisor only when its owned work changes state. |
| `scripts/orch-cron` | Recurring runner — reads Neo4j for `kind=recurring` tasks, fires their wake prompts on schedule. Replaces the static `recurring_triggers.json` pattern. |

## Status

Pre-release. Currently building. See [`docs/STATUS.md`](docs/STATUS.md) for what's wired and what's still scaffold.

## License

Apache-2.0
