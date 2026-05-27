# Changelog

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
