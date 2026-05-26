# Plan Format Spec — execution tracker ingestion

The execution tracker indexes plans wherever they live on disk. Source files don't move — `taey-plan ingest <path>` reads the file, hashes it for provenance, and creates `OrchProject` / `OrchPhase` / `OrchTask` nodes in Neo4j with `source_path` pointing back to the original.

## Minimum format

```
# Project: <project-id> — <Project Name>
> One-line description of the project.

## Phase: <phase-id> — <Phase Name>  [order: 1]

### Task: <task-id> — <Task description>  [priority: 60] [owner: <session>] [tags: tag1,tag2] [depends: other-task-id]
- Optional bullet body folded into the task description.
- Multiple bullets become one description.
```

Rules:
- Project, phase, and task IDs are arbitrary strings — short, kebab-case is the convention but not enforced. They become the Neo4j node `id`.
- Each project file has exactly one `# Project:` header. Multiple `## Phase:` and `### Task:` headers are expected.
- Header lines must use `—` (em dash) or `-` (hyphen) between id and name.
- Bracketed metadata is optional. Recognized keys: `order` (int), `priority` (int), `owner` (string — session name), `tags` (comma-list), `depends` (comma-list of task ids).
- Lines inside fenced code blocks (between triple-backtick) are skipped — useful for embedding format examples in plan docs.
- A bullet body following a task header is folded into the task description.

## Provenance fields stored on `OrchProject`

When ingested via the API or `taey-plan ingest <path>`:
- `source_path` — absolute path to the file you ingested
- `source_sha256` — content hash at ingestion time (lets us detect drift later)
- `source_kind` — `markdown`, `consultation`, `chat`, or `ad_hoc` (auto-detected by path; override via API)
- `ingested_at` — UTC datetime
- `ingested_by` — session name that called ingest

## Idempotency

Re-running `taey-plan ingest <path>` on an unchanged file: 0 created, all tasks `updated` (description/priority/owner refreshed). Tasks present in Neo4j but absent from the current markdown are returned in `stale_tasks` and **not auto-deleted** — you decide whether to archive or remove them.

## CLI surface

```bash
taey-plan list                                   # all projects with phase + status counts
taey-plan show <project-id>                      # phase breakdown, task counts per phase
taey-plan current                                # what this session is currently executing
taey-plan next [session]                         # top pending task owned-by-you or unowned-team-matched
taey-plan ingest <path-to-md>                    # parse + load (or refresh) into Neo4j
taey-plan assign <task-id> <session>             # change task owner
```

## API surface (port 5002)

- `GET /api/projects` — list
- `GET /api/projects/{id}` — summary
- `POST /api/projects` — create empty project (with provenance)
- `POST /api/projects/{id}/phases` — add a phase
- `POST /api/projects/load-md` — body `{md_text, source_path, source_kind, ingested_by}`
- `GET /api/sessions/{sess}/current` — current in_progress work for a session
- `GET /api/sessions/{sess}/next-ready` — next ready task

## Source-of-truth discipline

The markdown file is canonical. Edit the file → re-ingest. Don't edit Neo4j fields by hand for things that came from a markdown plan; they'll get overwritten on next ingest.

For ad-hoc tasks not tied to a written plan: `taey-task create "..." --priority N` keeps working as before; those go under the `default` project.

## Where to keep your plan files

Wherever they already are. Examples already on Mira:
- `/path/to/repo` — conductor-owned plans
- `/path/to/repo` — treasurer consultations
- `/path/to/repo` — plan-mode outputs (ephemeral; copy to a stable home before ingesting if you want them tracked long-term)

Just point `taey-plan ingest` at the file. The path is the link.
