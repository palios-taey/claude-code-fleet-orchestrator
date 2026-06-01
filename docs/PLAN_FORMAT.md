# Plan Format Spec

The execution tracker indexes markdown plans into `OrchProject`, `OrchPhase`, and `OrchTask` nodes.

## Minimum format

```md
# Project: project-id - Project Name
> One-line project description.

## Phase: phase-id - Phase Name [order: 1]

### Task: task-id - Task description [priority: 60] [owner: worker-a] [depends: other-task-id]
- Optional bullet content becomes part of the task description.

## User Stop Conditions
- stop_when_all_ready_tasks_dispatched
```

## Rules

- One `# Project:` header per file.
- Zero or one `## User Stop Conditions` section at project scope.
- `order` and `priority` are integers.
- `owner`, `tags`, and `depends` are optional.
- Content inside fenced code blocks is ignored by the loader.

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
