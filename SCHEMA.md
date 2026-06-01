# Schema

This document reflects the actual graph and queueing behavior implemented in `src/fleet_orchestrator/orch_schema.py`, `plan_loader.py`, `tasks_api.py`, and the v1.3+/v1.4 branch work. It describes the durable Neo4j model, not the older pre-graph design notes.

## Node types

### `OrchProject`

Top-level execution container for one plan or ad-hoc project.

Common properties written by the current code include:

| Field | Meaning |
| --- | --- |
| `id` | project identifier |
| `name` | display name |
| `description` | freeform summary |
| `status` | project state, typically `active`, `in_progress`, `stopped`, or `completed` |
| `supervisor` | session responsible for the project |
| `priority` | queue priority; lower numbers run earlier |
| `user_stop_conditions` | serialized stop-condition list |
| `stop_reason_current` | current stop-reason payload, if any |
| `stop_reason_history` | serialized history of stop reasons |
| `priority_history` | serialized history of priority changes |
| `in_progress_heartbeat_at` | last project-level heartbeat timestamp while work is in progress |
| `source_path`, `source_sha256`, `source_kind` | provenance for ingested markdown plans |
| `ingested_at`, `ingested_by` | plan ingest metadata |
| `migration_exempt` | migration compatibility flag |

### `OrchPhase`

Ordered subdivision of a project.

| Field | Meaning |
| --- | --- |
| `id` | phase identifier |
| `name` | phase title |
| `order` | phase ordering value |
| `status` | phase status, updated as tasks complete |

### `OrchTask`

Unit of execution.

| Field | Meaning |
| --- | --- |
| `id` | task identifier |
| `description` | task body shown to operators |
| `status` | task state; current code uses `pending`, `in_progress`, `completed`, `failed`, `interrupted` |
| `priority` | queue priority; lower numbers run earlier |
| `owner` | session that owns the task |
| `created_by` | creator / ingest origin |
| `blocked_on` | non-empty marker for a genuinely waiting in-progress task |
| `result` | optional task result payload |
| `closeout_commit_sha` | completion evidence |
| `closeout_production_observation` | completion evidence |
| `closeout_evidence_note` | optional extra closeout note |
| `forced_continuation_count` | still live; reset and read by current branch code |
| `heartbeat_exempt_secs` | optional heartbeat override |

### `OrchQuestion`

Open question linked to a task or created standalone.

| Field | Meaning |
| --- | --- |
| `id` | question identifier |
| `text` | question body |
| `context` | supporting context |
| `task_id` | copied task id when linked |
| `asked_by` | originator |
| `status` | `open` or `answered` in current code |
| `answer`, `answered_by`, `answered_at` | answer payload when resolved |

## Relationships

| Relationship | Meaning |
| --- | --- |
| `(OrchProject)-[:HAS_PHASE]->(OrchPhase)` | phase membership |
| `(OrchPhase)-[:HAS_TASK]->(OrchTask)` | task membership |
| `(OrchTask)-[:DEPENDS_ON]->(OrchTask)` | prerequisite task edge |
| `(OrchQuestion)-[:CONCERNS_TASK]->(OrchTask)` | question linked to a task |

## Ready-work rule

A task is ready when all of the following are true:

- task status is effectively `pending`
- owner matches the queried session
- `blocked_on` is empty
- every upstream `DEPENDS_ON` task is `completed`

The code treats missing/null task status as `pending` in ready-work queries and project summaries for backward compatibility with older rows.

## Priority convention

The code sorts by ascending priority, not descending priority.

That means:

- `priority=1` outranks `priority=50`
- projects are chosen by `coalesce(project.priority, 999999999) ASC`
- tasks are chosen by `coalesce(task.priority, 999999999) ASC`

Tie-breakers are phase order and creation time.

## Transaction boundary

`update_task_status()` is the canonical task transition path. In the current branch it performs:

1. current-state read
2. transition validation
3. task write
4. parent project heartbeat/status recompute

inside one managed Neo4j write transaction. That is load-bearing: task state and project recompute are intended to commit together or roll back together.

## Question creation invariant

`create_question()` now validates the referenced task before it creates an `OrchQuestion` node. If `task_id` is non-empty and the task is missing, it raises `TaskWriteError` and does not leave an orphan question node behind.

## Redis state that complements the graph

The execution graph is Neo4j-backed, but active session coordination still depends on Redis keys used by the companion notify package:

- `taey:<worker>:current_task`
- `taey:<worker>:last_outcome`
- `taey:<worker>:parent`
- `taey:<session>:idle`

Those keys are operational session state. They are not a substitute for the durable task graph.
