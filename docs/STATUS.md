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

## Out of scope for v0.1.0

- Plan tracker REST API (Phase D)
- Recurring runner from Neo4j (Phase C)
- Per-CLI installer scripts (manual install for now — symlink hooks into `~/.claude/settings.json`, `~/.codex/config.toml`, `~/.gemini/settings.json` paths)
- Multi-machine routing (currently localhost-only; future scope)
