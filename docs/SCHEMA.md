# Schema — Task model + reservation notes

> Status: the Neo4j-backed plan tracker is live in the current release. Redis keys still back the dispatch primitive, and `orch-cron` still uses operator-controlled JSON state for recurring fires. The rules below describe the current task model plus reserved future-expansion names.

## OrchTask

One node label for all tasks. Two `kind` values that change which fields are valid and which `status` enum applies. Per Family Phase C consultation 2026-05-26 (Gaia): don't fragment to `OrchRecurringTask` — sibling labels pollute every `MATCH (t:OrchTask)` query and double the migration friction for the few live entries we have.

### Common fields

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string | yes | Unique task identifier |
| `description` | string | yes | One-line human description |
| `kind` | enum | yes | `one_shot` (default) or `recurring` |
| `owner` | string | no | Session name that owns the task |
| `priority` | int | no | Lower = earlier in ready-task queues |
| `created_at` | float | yes | unix epoch |
| `updated_at` | float | yes | unix epoch |

### Kind-aware `status` enum (LOAD-BEARING per Gaia)

The `status` field's allowed values depend on `kind`. **Never** allow `completed` on a recurring task — it's semantically wrong (a recurring task is a *factory*, not a single unit of work) and breaks downstream `MATCH (t:OrchTask {status: 'completed'})` queries that should match only finished one-shot work.

**`kind = one_shot`** — lifecycle of a single unit of work:
```
status ∈ {pending, in_progress, completed, failed, blocked}
```

**`kind = recurring`** — lifecycle of a task factory; individual fires complete, the factory itself does not:
```
status ∈ {active, paused, retired}
```

Implementations MUST reject writes that put a recurring task into a one-shot status or vice versa.

### Recurring-only fields

| Field | Type | Required | Notes |
|---|---|---|---|
| `schedule` | object | yes (recurring) | `{tz, minute, hours[]}` — wall-clock fire times |
| `prompt_file` | path | yes (recurring) | File whose contents become the fire message body |
| `state_file` | path | no (recurring) | Append-only JSONL of fire records |
| `last_fire_ts` | float | written by orch-cron | unix epoch of last fire |
| `last_fire_log_hash` | string | written by orch-cron | `sha256:<hex>` of full state_file after last fire |

## State file integrity (Logos amendment, LOAD-BEARING)

`state_file` is a pointer to an external filesystem artifact (a JSONL log). Naive "trust the pointer" violates [BLACK_HOLE preservation invariants](https://github.com/palios-taey) and [CANNOT_LIE_PROVENANCE](https://github.com/palios-taey) — graph queries can't audit the log artifact without a reference back to the schema.

**Rule: hash-on-fire.** Every cron fire that writes to `state_file`:

1. Appends the fire record (one JSON line).
2. Computes SHA-256 of the full appended file.
3. Writes `<state_file>.meta.json` with:
   ```json
   {
     "last_fire_log_hash": "sha256:abc123...",
     "last_fire_ts": 1779800000,
     "last_fire_id": "x-claude-cycle-20260526-1209",
     "last_fire_size_bytes": 42137
   }
   ```
4. The same field names are reserved for graph-side mirrors if recurring fire state is later copied onto OrchTask nodes.

Cost: <1ms per fire, even at 8 fires/day per loop. No cardinality explosion. No node-size bloat. The sidecar is plain JSON so any auditor (graph query, monitoring script, human grep) can verify the state file hasn't drifted since the last fire.

`orch-cron` writes the sidecar automatically when `state_file` is configured. Migration of existing recurring_triggers.json entries: backfill happens on the next fire — no batch migration needed.

## Reserved schema (future expansion)

The Phase C consultation noted that per-fire visibility is the next natural growth (Gaia: "a recurring task's *fires* are the things that complete"). Reserved now so v0.4+ can add per-fire nodes without a label migration:

```
(:OrchTask {kind: 'recurring'})
   -[:FIRED {ts: <unix>, hash: <sha256>}]->
   (:OrchRecurringFire {fire_id, ts, prompt_hash, result, hostname})
```

Current behavior does not create per-fire nodes — the cardinality (8/day per loop) stays in JSONL + sidecar hash. The relationship and node names remain reserved so a future graph expansion can add them without breaking existing queries.

## Dispatch tracking (Phase A primitive, v0.1.0+)

For one-shot tasks dispatched to workers via `fleet_orchestrator/dispatch.py:dispatch()`, the fleet-notify Redis state (`REDIS_HOST` / `REDIS_PORT`) is the source of truth (no Neo4j needed for the dispatch loop itself):

| Redis key | Type | Lifecycle |
|---|---|---|
| `taey:<worker>:current_task` | JSON `{task_id, description, supervisor, dispatcher, started_at}` | Set on dispatch. Dispatch refuses to overwrite a different dispatcher's live graph-backed binding (`in_progress` or `dispatched`) unless the caller explicitly forces replacement. If the bound graph task has moved to a non-live status, dispatch clears/replaces the stale binding. Status transitions out of `in_progress`, `taey-task outcome error/interrupted` returns, and worker-liveness pending reverts clear the matching binding; `taey-task unbind <worker>` / `DELETE /api/sessions/{worker}/current-task` is the manual escape hatch for inspected stale state. Older/direct bind paths may omit `dispatcher`; the guard falls back to `supervisor` for those records. |
| `taey:<worker>:last_outcome` | JSON `{outcome, details}` | Optionally set by worker via `taey-task outcome` before stopping. |
| `taey:<worker>:last_completion_receipt` | JSON `{outcome, task_id, worker, ts, details?}` | Written for `taey-task outcome done` and owner PATCH completion after the matching `current_task` is cleared or already absent. The stop engine uses this bounded receipt as the post-clear proof that a cleared `current_task` really corresponds to the worker's last `done` outcome. |
| `taey:<worker>:parent` | string | Optional explicit supervisor override (else suffix-strip). |
| `taey:<worker>:idle` | "1" | Set by Stop hook, cleared by UserPromptSubmit hook. |
| `taey:<worker>:last_activity` / `taey:<worker>:last_tool_activity` | unix timestamp string | Stamped by fleet-notify lifecycle hooks. Worker liveness treats `last_tool_activity` as task work only when the worker's `current_task` matches the task under inspection. |
| `taey:<worker>:tool_running` / `taey:<worker>:tool_running_at` | "1" / unix timestamp string | Set by fleet-notify PreToolUse and cleared by PostToolUse. Orchestrator liveness consumes this only as an age-bounded matching-current-task signal; a stale `tool_running` key alone does not preserve or clear work. |

These keys coexist with the live Neo4j task tracker. When a dispatched task already exists as an OrchTask, `dispatch()` claims it in Neo4j and also sets `current_task` in fleet-notify Redis. If the Redis bind is refused because another dispatcher already owns a live worker slot, the new claim is rolled back to `pending` and the existing binding is preserved. If the existing binding resolves to a non-live graph task, the stale binding is cleared and the new dispatch proceeds. Fleet-notify Redis remains the direct source of truth for stop-hook / idle-state coordination.
