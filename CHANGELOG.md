# Changelog

## v1.0.3 - 2026-05-27

- Fixed the `orch-watch` continuation gap where repeated PEER_IDLE wakes were informational only and could trap self-owned sessions in a wake/acknowledge/stop loop.
- Repeated idle wakes now carry a continuation directive when another ready OrchTask exists for the same session.
- Added optional `OrchTask.blocked_on` state plus `taey-task update <id> in_progress --blocked-on <signal>` so genuinely waiting tasks can suppress repeat PEER_IDLE wakes entirely.
- When no other ready work exists and `blocked_on` is unset, the wake body now says that explicitly so the session can confirm it is correctly waiting instead of stopping ambiguously.

## v1.0.2 - 2026-05-27

- Fixed self-owned `taey-task` / `taey-plan` work visibility so `orch-watch` can see and react to those sessions' stop events.
