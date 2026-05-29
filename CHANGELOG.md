# Changelog

## v1.1.0 - 2026-05-29

- Added project-level `user_stop_conditions` so plans can declare when a session is actually allowed to stop instead of reflexively halting after every wake or notification.
- Extended `scripts/orch-watch` with an idle-time autonomy gate: if a session stops with an `in_progress` task and no `blocked_on`, the daemon now evaluates plan predicates, writes auditable `blocked_on=<condition>` when a stop condition matches, auto-continues the session with a concrete next-ready task when the plan still has work, or sends an explicit clarify-intent wake when the plan is silent.
- Added `GET` / `POST /api/projects/{id}/user-stop-conditions`, markdown ingest support for a `## User Stop Conditions` section, and `taey-plan stop-conditions <project-id> get|set` for live plan control.

## v1.0.6 - 2026-05-28

- Fixed treasurer's reported `neo4j.exceptions.AuthError: Unsupported authentication token, missing key scheme` in the `lib.dispatch._orch_task_exists()` path by teaching `lib.config.get_neo4j_driver()` to honor `ORCH_NEO4J_USER` and `ORCH_NEO4J_PASS` when they are set.
- Preserved backward compatibility for the existing no-auth default: if those env vars are unset, the driver is still created with `auth=None`.
- Added live verification for the current no-auth conductor environment, an explicit auth-configured path, and a fail-loud auth omission case in `PHASE_V106_NEO4J_AUTH_VERIFICATION.md`, with the current Mira host-state drift called out explicitly.

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
