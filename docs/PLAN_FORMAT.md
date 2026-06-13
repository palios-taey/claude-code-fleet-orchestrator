# Plan Format Spec

The execution tracker indexes markdown plans into `OrchProject`, `OrchPhase`, and `OrchTask` nodes.

## Minimum format

```md
# Project: project-id - Project Name
> One-line project description.

## Phase: phase-id - Phase Name [order: 1]

### Task: task-id - Task description [priority: 60] [owner: worker-a] [depends: other-task-id] [ref: docs/STRUCTURE.md:10-40]
- Optional bullet content becomes part of the task description.

## User Stop Conditions
- stop_when_all_ready_tasks_dispatched
```

## Rules

- One `# Project:` header per file.
- Zero or one `## User Stop Conditions` section at project scope.
- `order` and `priority` are integers.
- `owner`, `tags`, and `depends` are optional.
- `recurring: true` marks a completed task as re-claimable by `dispatch()` for the next cycle. This is for markdown-tracked repeated work items, not the cron-factory `kind=recurring` reservation in `docs/SCHEMA.md`.
- `ref` is repeatable and uses `[ref: <path>:<Lstart>-<Lend>]`.
- Content inside fenced code blocks is ignored by the loader.

## Ref semantics

Observed in `fleet_orchestrator/orch_schema.py`:

- refs are stored as structured metadata and resolved later into `ref_context`
- the orchestrator reads ref file slices fresh at API/runtime read time rather than copying file contents into Neo4j
- refs require a `source_path` for the ingested plan when refs are present
- refs are disabled unless `ORCH_REF_ALLOWED_ROOT` is set
- `ORCH_REF_ALLOWED_ROOT` can be a single path, a comma-separated list, or a JSON list of paths
- ref paths must be relative
- absolute paths, `~`, control characters, `..` escapes, and symlink escapes are rejected
- the resolved ref path must stay under both the plan file directory and one of the configured allowed roots
- unreadable or oversized refs degrade to warnings in `ref_context`

## CLI surface

```bash
taey-plan list
taey-plan show <project-id>
taey-plan current
taey-plan next [session]
taey-plan ingest <path-to-md>
taey-plan assign <task-id> <session>
taey-plan stop-conditions <project-id> get
taey-plan stop-conditions <project-id> set <condition> [<condition> ...]
```

## `taey-task` vs `taey-plan`

`taey-plan` is the markdown-backed project tracker CLI.

Observed in `scripts/taey-task`, `taey-task` is the direct task-management CLI and exposes:

```bash
taey-task create "<description>"
taey-task list
taey-task status <task-id>
taey-task update <task-id> <status>
```

Use `taey-plan` for project / phase / task structures sourced from markdown. Use `taey-task` for direct task creation, ranking, inspection, and status updates through the API.

## API surface

- `GET /api/projects`
- `GET /api/projects/{id}`
- `GET /api/projects/{id}/user-stop-conditions`
- `POST /api/projects`
- `POST /api/projects/{id}/phases`
- `POST /api/projects/{id}/user-stop-conditions`
- `POST /api/projects/load-md`
- `GET /api/sessions/{session}/current`
- `GET /api/sessions/{session}/next-ready`

## Source of truth

The markdown file is canonical. Re-ingest after editing the file.

## Where to keep plans

Keep plan files wherever they already live in your environment and point `taey-plan ingest` at the file you want indexed.
