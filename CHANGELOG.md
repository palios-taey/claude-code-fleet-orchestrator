# Changelog

## v1.0.5 - 2026-05-28

- Closed Gaia's pending v0.4.1 zero-dependency wake gap from `/home/mira/the-conductor/plans/conductor_v041_followups.md`: `lib.orch_schema.create_task()` now carries owner metadata in the initial write and immediately wakes an idle owner when the new task has no upstream dependencies.
- Added a dispatch-time OrchTask claim gate in `lib.dispatch.dispatch()`: if the `task_id` exists in Neo4j, dispatch now atomically re-checks `status='pending'` plus "all dependencies completed" in the same Cypher write that flips the task to `in_progress`, aborting with `OrchTaskNotReady` before any Redis `current_task` mutation when the task is stale or still blocked.
- Recorded the live zero-dep wake proof and the dispatch race proof in `PHASE_V041_RACE_VERIFICATION.md`.

## v1.0.4 - 2026-05-27

- Added the architecture spec §3.1 bug-lock pre-dispatch gate in `lib/dispatch.py` via `BugLockActive` plus a new `is_bugfix=False` kwarg that allows bug-fix dispatches to proceed under an active lock.
- Added minimal session-to-product resolution for conductor-owned work so dispatches to `conductor-*` sessions check `support:product:the-conductor:bug_lock` before any Redis state mutation.
- Production-verified the 5 spec-required conditions in live Redis/orch-watch state and recorded the exact evidence in `PHASE_BUGLOCK_VERIFICATION.md`.

## v1.0.3 - 2026-05-27

- Fixed the `orch-watch` continuation gap where repeated PEER_IDLE wakes were informational only and could trap self-owned sessions in a wake/acknowledge/stop loop.
- Repeated idle wakes now carry a continuation directive when another ready OrchTask exists for the same session.
- Added optional `OrchTask.blocked_on` state plus `taey-task update <id> in_progress --blocked-on <signal>` so genuinely waiting tasks can suppress repeat PEER_IDLE wakes entirely.
- When no other ready work exists and `blocked_on` is unset, the wake body now says that explicitly so the session can confirm it is correctly waiting instead of stopping ambiguously.

## v1.0.2 - 2026-05-27

- Fixed self-owned `taey-task` / `taey-plan` work visibility so `orch-watch` can see and react to those sessions' stop events.
