# Status — what's wired vs scaffold

## Phase A — Universal Stop+notify (✅ shipped in v0.1.0)

**Done:**
- `lib/dispatch.py` with `dispatch()`, `record_outcome()`, `check_previous_task()`, `clear_current_task()`.
- Companion hook upgrade in `claude-code-fleet-notify` v0.2.0 (`hooks/_shared.py` enhanced `action_stop` + `_resolve_supervisor` + `_current_task_summary` + `_notify_supervisor_of_stop`).
- Outcome enum: `done | error | interrupted | unknown`. Dispatcher clears `current_task` only when outcome is explicitly `done`.
- Supervisor resolution: explicit `taey:<node>:parent` Redis key OR suffix-strip (`<name>-codex` / `<name>-gemini` / `<name>-grok`).

**Live-verified end-to-end** on conductor-codex 2026-05-26: dispatch → released daemon-inject → worker processes → `record_outcome('done', ...)` → Stop hook → supervisor sees rich peer_idle inline.

**Validated by Family:** Gaia (outcome-enum amendment, load-bearing) + Logos (two-mechanism resolver, optimal).

## Phase B — Event-driven watchloop (✅ shipped in v0.2.1)

`scripts/orch-watch` — replaces the 3-min-poll watchloop that was spamming supervisors with "CONTINUE" messages even when there was no work. PSUBSCRIBE to Redis keyspace notifications + 30-min safety-net sweep (hybrid per Logos Phase B consultation 2026-05-26: keyspace events are best-effort/at-most-once, the poll is the reliability backstop).

**Two signals it pages on** (both silent stalls no awake actor would notice, per Gaia same consultation):

1. **Stuck `current_task` while worker idle** — worker stopped, Stop hook sent its single peer_idle, supervisor missed/didn't act, unresolved task sits indefinitely. Threshold: `--stuck-threshold-sec` (default 300s).
2. **Done-DEL that unblocks supervisor's OrchTask while supervisor is idle** — symmetric twin. When a worker finishes cleanly (outcome=done → Stop hook clears current_task), the readiness checker is invoked; if a previously-blocked task is now ready for an idle supervisor, page the supervisor. Requires `--readiness-checker module:function` to wire a real check; if unset, DEL events are logged and skipped.

**Both signals route through a single `investigate()` handler.** The PSUBSCRIBE path (event-driven) and the periodic sweep (reliability backup) call the same function with the same semantics.

**What it does NOT page on** (covered elsewhere):
- `inbox > 0 + idle=1` → released fleet-notify daemon already injects via tmux.
- `Worker just stopped, outcome enum set` → Stop hook already sent peer_idle.

These are pulls, not pages — supervisors drain them on their next loop.

**Requires** Redis `notify-keyspace-events` to include `Kgl$`. Daemon auto-sets this if missing; installer (Phase D) will make it permanent via `CONFIG REWRITE`.

**Readiness checker interface**:
```python
def check_readiness(supervisor: str, completed_task: dict) -> Optional[str]:
    """Return a wake message body if this completion unblocked work
    for an idle supervisor, else None."""
```
Plan tracker ships a default implementation in v0.4.0 (Phase D). For v0.2.x, operator wires their own.

**Smoke-tested 2026-05-26**:
- Stale current_task (600s old) on idle worker → STUCK escalation landed in supervisor inbox.
- Done-DEL with synthetic readiness checker returning a wake message → UNBLOCK wake landed in supervisor inbox.

Currently running on the Mira fleet via `peer-respawn.sh` DAEMONS list (without `--readiness-checker` for now; just stuck-detection).

## Phase C — Recurring task type (✅ shipped in v0.3.0)

`scripts/orch-cron` + [`docs/SCHEMA.md`](SCHEMA.md) — first-class recurring tasks with file-tracked state and tamper-evident hash provenance.

**What ships**:
- `scripts/orch-cron` — drop-in replacement for static `recurring_triggers.json`-style runners. Backward-compatible with existing JSON registry format; adds optional `state_file` per trigger that becomes an append-only JSONL audit log of fires.
- `docs/SCHEMA.md` — formal task model. One `OrchTask` label, kind-aware status enum (`one_shot` → `{pending,in_progress,completed,failed,blocked}`; `recurring` → `{active,paused,retired}` NEVER `completed`). Reserves `(:OrchTask)-[:FIRED]->(:OrchRecurringFire)` for v0.4+ per-fire visibility.
- Hash-on-fire sidecar — every fire that writes the state file also writes `<state_file>.meta.json` with `last_fire_log_hash` (SHA-256 of the full appended file), `last_fire_ts`, `last_fire_id`, `last_fire_size_bytes`. Tamper-evident integrity without graph bloat.

**Family Phase C consultation 2026-05-26 (both amendments load-bearing)**:
- Gaia: one label not two; kind-aware status; reserve FIRED relationship.
- Logos: hash-on-fire is mathematically required (pure trust-the-pointer violates BLACK_HOLE + CANNOT_LIE_PROVENANCE).

**Real-fleet production verified 2026-05-26**:
- Trigger at current ET wall-clock minute → orch-cron fired → state.jsonl recorded `result:dispatched` → meta.json `last_fire_log_hash` matched actual file SHA-256 → message landed in Redis inbox via released taey-notify CLI.
- Bug found + fixed during real-fleet smoke: dry-run was setting the dedup key (blocked subsequent real fires); state file was recording skipped/dry-run results (polluted audit trail). Both fixed before commit.

**Migration of live fleet**: x-claude/treasurer's existing recurring_triggers.json entries can be extended in-place by adding `state_file` field. orch-cron is backward-compatible — existing entries without state_file continue to work, just without the audit log. Cutover from conductor's `recurring_trigger_runner.py` to orch-cron can happen incrementally.

## Phase D — v0.2.0 + plan tracker / CLIs (⏳ planned)

Pull `taey-plan` + `taey-task` CLIs + `tasks_api.py` + `neo4j_schema.py` + `plan_loader.py` from conductor into this repo. Ship as v0.2.0 with the full orchestration surface.

## v0.3.1 — Audit fixes (✅ shipped)

Family code-audit consultation 2026-05-26 returned 8 findings across the four files in v0.3.0. All addressed (companion patch in fleet-notify v0.2.1).

**TIER 1** (one fix collapses 5 findings per Gaia's one-cut diagnosis):
- Compare-and-swap on `(worker, task_id)` for `dispatch()` ↔ Stop-hook clear. The Stop hook's done-clear now runs as a Lua compare-and-delete keyed on the task_id observed when the summary was built; if a concurrent `dispatch()` wrote a fresh task_id between observation and clear, the Lua sees the mismatch and skips. `dispatch()` atomically clears stale `last_outcome` + task-specific stuck-dedup in a MULTI pipeline so the next Stop hook reads the fresh state.

**TIER 2**:
- `peer-idle-notified` + `orch-watch-stuck` dedup keys now include `:<task_id>` (was per-worker, masked genuine re-alerts within TTL — Logos contract #3 + Gaia dispatch #2).
- `orch-watch` sweep moved out of `pubsub.listen()` blocking generator into a `get_message(timeout=N)` polling loop. Old code: in a quiet fleet, sweep never fired after bootstrap, defeating the Phase B "PSUBSCRIBE + safety-net poll" reliability design. Live-verified: stuck task on quiet fleet detected within `min(60s, sweep/4)` of the sweep interval (Logos critical reliability gap).
- Stop hook writes `taey:<node>:last_clear_was_done` (30s TTL marker) on successful CAS done-clear. orch-watch's DEL handler reads the marker — absent means force-clear (supervisor `clear_current_task()`), so readiness check is skipped (no spurious unblock-wake on administratively-cleared errors — Gaia orch-watch #2).

**TIER 3**:
- `orch-watch` stuck-time measurement now uses Redis-server time (`_redis_now()`) + `last_activity` boundary (not local `time.time()` + `started_at`). Cross-host clock skew can't fire alerts early/late; a worker that just transitioned to idle isn't flagged stuck just because dispatch was 5+ minutes ago (Gaia orch-watch #3).

**Verified end-to-end**:
- TIER 1 CAS race: dispatch B raced past Stop-clear of A; new code skipped clear (task-B survived); old code would silently delete task-B.
- TIER 1 stale outcome: re-dispatch after error wipes stale `outcome=error`; next Stop hook reads fresh state.
- TIER 2b sweep: stuck task on quiet fleet (no events) detected within 8s on sweep_interval=8s.
- TIER 2c done-marker: present after Stop-hook CAS clear (readiness check runs); absent after `clear_current_task()` (readiness check skipped, no spurious unblock-wake).

## Phase D — Plan tracker + default readiness checker (✅ shipped in v0.4.0)

Extracted from conductor's internal codebase per the lib-extract-then-re-import hardening pattern (Jesse 2026-05-26). Conductor now consumes from the orchestrator clone via 3-line shims that `sys.path.insert` the orchestrator root and re-export — `python3 -m uvicorn conductor.tasks_api:app` keeps working unchanged, but the actual code lives in the orchestrator repo as canonical.

**What shipped**:

| Component | Source of truth | Purpose |
|---|---|---|
| `lib/config.py` | extracted from `conductor/config.py` | `OrchConfig` + Redis/Neo4j connection helpers. `.env` loading is path-flexible: respects `ORCH_DOTENV` env var → CWD `.env` → package-root `.env`. |
| `lib/orch_schema.py` | extracted from `conductor/neo4j_schema.py` | 18 functions on the OrchProject/Phase/Task DAG: CRUD, dependencies, ready-task discovery, phase-completion cascade, session current/next-ready, question schema. |
| `lib/plan_loader.py` | extracted from `conductor/plan_loader.py` | Markdown plan parser. Idempotent, content-hash provenance. |
| `lib/tasks_api.py` | extracted from `conductor/tasks_api.py` | 7 FastAPI endpoints on `:5002`: `/api/tasks`, `/api/projects`, `/api/projects/load-md`, `/api/sessions/{sid}/current|next-ready`. |
| `lib/plan_readiness.py` | **NEW** | Default readiness checker for `orch-watch --readiness-checker`. LOOSE semantic (wake on the transition, not on every completion). Self-loops excluded. SETNX dedup per downstream task handles concurrent-finals race. Best-effort: Neo4j or Redis failure returns `None` rather than raising. |
| `scripts/taey-task` | extracted from `conductor/scripts/taey-task` | Task create/update/list CLI. |
| `scripts/taey-plan` | extracted from `conductor/scripts/taey-plan` | Project list/show/current/next-ready/ingest-md CLI. |

**Conductor shims** (lib-extract-then-re-import pattern):
- `conductor/tasks_api.py` → `from lib.tasks_api import app` (preserves the uvicorn command).
- `conductor/neo4j_schema.py` → re-exports the full public surface.
- `conductor/plan_loader.py` → re-exports `load_plan_from_text`.

**Family Phase D consultation 2026-05-26 (both amendments load-bearing)**:
- **Gaia (LOOSE + edge cases)**: incorporated — wake on the blocked→ready transition not on every completion; self-loops excluded from the Cypher; concurrent-finals handled by SETNX dedup keyed on downstream task_id. Zero-dep tasks and already-completed-deps-at-edge-creation are queued for v0.4.1 (separate creation-time and write-time wake paths, not silently dropped).
- **Logos (release-blocker)**: lib-extract-then-re-import does NOT achieve zero-downtime because Python freezes module state in `sys.modules` at first import and FastAPI registers routes at import time. Disk-level shim doesn't reach the running daemon. Path chosen: **(a) coordinated daemon restart** with prior notification to active fleet sessions (treasurer, taeys-hands). Interruption window ~10s; no in-flight `/api/*` calls observed during the window. Honest documentation of the trade-off rather than pretending the pattern was zero-downtime.

**orch-watch wired with the new checker** (peer-respawn.sh):
```
orch-watch:cd /path/to/repo && python3 scripts/orch-watch \
    --readiness-checker /path/to/repo:check_readiness
```

**Real-fleet production verified 2026-05-26**:
- Test graph: `pdsmk-dep-A → pdsmk-down-B` (B depends on A, owned by conductor, status=pending).
- A status flipped to `completed`.
- `check_readiness('conductor', {task_id: 'pdsmk-dep-A', ...})` returned: `"UNBLOCK: completion of task=pdsmk-dep-A just unblocked task=pdsmk-down-B (\"dep on A\") owned by you. 0 other deps were already done. Pick it up with `taey-plan next` or dispatch a worker."`
- Second call within TTL returned `None` (SETNX dedup working — concurrent-finals race protection holds).
- `taey-plan list` from orchestrator's `scripts/` returned 14 live projects from production Neo4j.
- `taey-task list` from orchestrator's `scripts/` returned top-priority pending tasks.

## v0.4.1 — Follow-ups (queued from Phase D consultation)

- **Zero-dep tasks**: tasks with no `DEPENDS_ON` edges never trigger a transition wake (they have no completion event to react to). Need a separate creation-time wake path in `orch_schema.create_task` that pages the owner immediately if the new task has zero deps + owner is idle.
- **Already-completed deps at edge-creation**: when `add_dependency(t, d)` is called and `d` is already `status=completed`, no future transition fires for `t`. Need a write-time check in `orch_schema.add_dependency` that runs the same LOOSE-check Cypher and fires wake if `t` is now ready.
- Both deferred from v0.4.0 because they're additive features, not correctness gaps in the shipped path. Tracked in [issue tracker / next-session backlog].

## Out of scope for v0.4.x

- Per-CLI installer scripts (manual install for now — symlink hooks into `~/.claude/settings.json`, `~/.codex/hooks.json`, `~/.gemini/settings.json` via fleet-notify's `install-hooks.sh --all`).
- Multi-machine routing (currently localhost-only; future scope).
- Web dashboard for OrchTask graph visualization (Neo4j Browser works fine for now).
