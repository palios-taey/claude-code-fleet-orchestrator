# Schema — Task model + reservation notes

> Status: schema lives in operator-controlled storage today (JSON registry for orch-cron, Redis keys for the dispatch primitive). A Neo4j-backed plan tracker ships in v0.4.0 (Phase D extraction from conductor) — when it lands, the rules below become enforced constraints. **The rules apply now regardless** — they guide the JSON registry format and the eventual Neo4j schema both.

## OrchTask

One node label for all tasks. Two `kind` values that change which fields are valid and which `status` enum applies. Per Family Phase C consultation 2026-05-26 (Gaia): don't fragment to `OrchRecurringTask` — sibling labels pollute every `MATCH (t:OrchTask)` query and double the migration friction for the few live entries we have.

### Common fields

| Field | Type | Required | Notes |
|---|---|---|---|
| `id` | string | yes | Unique task identifier |
| `description` | string | yes | One-line human description |
| `kind` | enum | yes | `one_shot` (default) or `recurring` |
| `owner` | string | no | Session name that owns the task |
| `priority` | int | no | Higher = earlier in queue |
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
4. (When plan tracker lands v0.4) the same fields persist on the OrchTask node.

Cost: <1ms per fire, even at 8 fires/day per loop. No cardinality explosion. No node-size bloat. The sidecar is plain JSON so any auditor (graph query, monitoring script, human grep) can verify the state file hasn't drifted since the last fire.

`orch-cron` writes the sidecar automatically when `state_file` is configured. Migration of existing recurring_triggers.json entries: backfill happens on the next fire — no batch migration needed.

## Reserved schema (v0.4+)

The Phase C consultation noted that per-fire visibility is the next natural growth (Gaia: "a recurring task's *fires* are the things that complete"). Reserved now so v0.4+ can add per-fire nodes without a label migration:

```
(:OrchTask {kind: 'recurring'})
   -[:FIRED {ts: <unix>, hash: <sha256>}]->
   (:OrchRecurringFire {fire_id, ts, prompt_hash, result, hostname})
```

For v0.3.x we DO NOT create per-fire nodes — the cardinality (8/day per loop) is fine in a JSONL log + sidecar hash. We reserve the relationship + node names so v0.4+ adds them without breaking existing queries.

## Dispatch tracking (Phase A primitive, v0.1.0+)

For one-shot tasks dispatched to workers via `lib/dispatch.py:dispatch()`, the Redis state is the source of truth (no Neo4j needed for the dispatch loop itself):

| Redis key | Type | Lifecycle |
|---|---|---|
| `taey:<worker>:current_task` | JSON | Set on dispatch. Cleared by Stop hook only when outcome=done. |
| `taey:<worker>:last_outcome` | JSON `{outcome, details}` | Optionally set by worker via `record_outcome()` before stopping. |
| `taey:<worker>:parent` | string | Optional explicit supervisor override (else suffix-strip). |
| `taey:<worker>:idle` | "1" | Set by Stop hook, cleared by UserPromptSubmit hook. |

These keys also pair with OrchTask records when the plan tracker is wired (v0.4+) — `dispatch()` will write the task to Neo4j AND set `current_task`. For v0.1–v0.3 they live only in Redis.
