# Changelog

## Unreleased

- No unreleased changes recorded.

## v1.5.1 - 2026-06-02

- Fixed `get_project_summary()` for projects with phases after a production `UnboundLocalError` surfaced on the real fleet dataset.

## v1.5.0 - 2026-06-02

- Added plan refs using repeatable `[ref:<path>:<Lstart>-<Lend>]` metadata with runtime `ref_context` reads gated by `ORCH_REF_ALLOWED_ROOT`.
- Added project lifecycle API support, including project completion and reset flows.
- Added the easy-setup entrypoints: `scripts/install`, `orch doctor`, `orch enable`, `orch disable`, and `orch uninstall`.
- Closed the `AskUserQuestion` / `AskUserQuestion(*)` escape hatch through managed Claude settings integration.
- Hardened the repo for standalone public use by moving runtime coupling to environment variables, removing hardcoded internal paths and hosts, and adding packaging metadata for `pip install .`.

## v1.4.0 - 2026-06-02

- Added the stop-enforcer backend: in-progress stop blocking plus handoff-validation support in the orchestrator API and schema helpers.

## v1.3.3 - 2026-05-31

- Applied the v1.3.3 queue-regression and priority-validation hotfix pass, including the additional readiness-path catch in `plan_readiness.py`.

## v1.3.2 - 2026-05-31

- Added the `coalesce(t.priority, 999999999)` NULL-guard fold across task-priority sort sites.

## v1.3.1 - 2026-05-31

- Fixed task-priority ordering so `t.priority` sorts ascending throughout current-work, next-ready, and ready-task selection paths.

## v1.3.0 - 2026-05-31

- Added the stop-discipline engine backend: Neo4j `OrchProject` / `OrchPhase` / `OrchTask` schema, markdown plan ingestion, session current/next-ready APIs, project stop-condition APIs, and the `taey-plan` / `taey-task` CLIs.

## v1.2.1 - 2026-05-30

- Added the session-first `/ui/` browser surface.
- Added session-scoped notify support through the API.
- Expanded project summaries so the UI can render task details without extra write endpoints.
