# Changelog

## Unreleased

- No unreleased changes recorded.

## v1.8.3 - 2026-06-15

Honesty/transparency release — behavior-neutral (docs, examples, test hardening; no production logic change). Driven by a repo-grounded multi-lens audit that read the actual source.

- **Operator identity removed from the public surface.** Genericized the operator's personal name and machine profile out of code defaults, UI, examples, comments, `.env.example` (which had shipped the full fleet topology), and test fixtures. (#100, #101, #102)
- **Durable recurrence guard.** The de-umbilical acceptance test now scans the whole tracked tree (not just a curated surface list) and fails if personal/machine operator identity reappears — narrowly scoped to personal/machine identity, leaving generic fleet-role test data alone. Fails closed if `git` is unavailable. (#102)
- **Docs match code.** New [docs/CONFIGURATION.md](docs/CONFIGURATION.md) documents all 66 environment variables with honest defaults and posture (including the tokenless-by-default mutable API, the non-env `CF_STOP_INPROGRESS` Redis enablement switch, `ACCOUNTABILITY_LEDGER_PATH`, and the then-current `ORCH_WAKE_PACKET_ENABLED` endpoint-only scope, now superseded by `ORCH_WAKE_PACKET_ENDPOINT_ENABLED`). New [AUDIT.md](AUDIT.md) is a self-checking reviewer entry point stating the system's claims (C1–C9) and known gaps (G1–G3) with file:line. Corrected the `ORCH_SESSION_IDS` doc (empty = unrestricted, not fail-closed). (#100)
- **Smaller fixes.** `lane_state.py` honestly labeled as a not-yet-wired measurement scaffold; wake-packet endpoint's fail-open-by-design contract documented; the `commit_sha` evidence-rejection message corrected to the real bound (4–64 hex). (#100, #101)

## v1.8.2 - 2026-06-14

- Added a `test` extra (`pip install -e ".[test]"`) so a fresh install can run the acceptance suite (the FastAPI TestClient needs `httpx`, which is not a runtime dependency); README documents the real, script-style way to run the suite. (#98)
- Fixed a long-standing version drift: `fleet_orchestrator/version.py` had stayed at `1.6.0` across the v1.7.0/v1.8.0/v1.8.1 tags, so every install since v1.7.0 misreported its version. Bumped to 1.8.2; added a fail-loud `version-tag-consistency` workflow (a release tag that does not match `version.py` fails), a `RELEASING.md`, derived the version-identity test's expected value from the source of truth (it had hardcoded `1.6.0`), and wired that test into the ship gate. (#99)

## v1.8.1 - 2026-06-14

- Zero operator identity: the forced-subrole gate ships a generic template (scout → code → audit → review → approval) with stage owners resolved from `ORCH_GATE_OWNERS`; the dashboard chat target comes from the configured session list; operator session/role names and codenames removed from all defaults, fallbacks, comments, and examples, enforced by a de-umbilical acceptance test so it cannot regress.

## v1.8.0 - 2026-06-14

- Adoption-ready: pinned `requirements.txt`, AI-native adopter README + contributor `CLAUDE.md`, and a doc-currency CI gate that blocks docs referencing nonexistent functions/files/env-vars/CLI commands.
- Private by default and self-contained: mutable API/dashboard binds `127.0.0.1` by default with optional `ORCH_AUTH_TOKEN`; no operator umbilical (zero hardcoded operator paths/IPs; missing config fails loud).
- Autonomous accountability: stop-discipline keep-going with clean stop on declared `AWAIT:<kind>:<detail>` waits; worker-liveness TTL auto-requeue + supervisor wake; one-verb peer dispatch (`taey-task dispatch`); evidence-gated completion across CLI and API; human-review gates as first-class stop states.
- Adversarial release discipline: `r5-audit-gate` (two independent audits on risky-path changes before merge) now covers the state-mutating `taey-*` CLIs; acceptance tests require an isolated namespace.

## v1.7.0 - 2026-06-11

- Consolidation: a single, pip-installable, AI-native `fleet_orchestrator` package (removed the `lib/` namespace, import-shim, duplicate config, and hardcoded operator path) with a `fleet-orchestrator-api` console-script entrypoint and safe-by-default `127.0.0.1` bind.
- Trust: introduced the structural `r5-audit-gate` (two independent adversarial auditors must post a `success` status on the exact SHA before merge, enforced by branch protection), plus completion gate, stranger-install gate, and a re-runnable live-capability ledger.
- Stop-discipline engine: tool-only liveness heartbeat, default-on supervisor keep-going, peer-liveness end to the dispatch busy-loop, actively-worked `blocked_on` release, evidence-required completion keystone.
- Dynamic context: per-session wake packets (refs + memory + rules) with an unforgeable nonce envelope against prompt injection, decision receipts, forced sub-role gate template, and the signal/clock/task-state loop engine.
- Robustness/security: wake-packet selection no longer renders empty, dotenv quote-stripping, atomic lock-first task claim, Cypher-injection defense-in-depth, `update_task_status` owner preservation, dashboard panel/slug/project-detail fixes.

## v1.6.0 - 2026-06-02

- Added a public read-only dashboard surface (`fleet_orchestrator/public_readonly.py`, `scripts/orch-public`, `ui/public_index.html`, `ui/static/public-app.js`): GET-only by construction (no write/mutate/notify route exists in the app), fail-closed session allowlist (`ORCH_PUBLIC_SHOW_SESSIONS`, default approved sessions only), outbound field allowlist with operator-path/host scrubbing, by-id backstop (`ORCH_PUBLIC_HIDE_PROJECT_IDS`), pointer-only refs (file contents never served), `127.0.0.1` bind, sanitized `/health` (no infra detail), and disabled interactive API docs (`/docs`, `/redoc`, `/openapi.json`). Intended for a single read-only tunnel route; never the live mutable API.
- Fixed ingest dependency gating: ingested `[depends:]` edges now gate task readiness — tasks are created in a held `ingesting` status until their dependencies are wired, then released, and `add_dependency` fails loud (surfacing an ingest error) on a missing target instead of silently leaving the task ungated.

## v1.5.1 - 2026-06-02

- Fixed `get_project_summary()` for projects with phases after a production `UnboundLocalError` surfaced on the real fleet dataset.

## v1.5.0 - 2026-06-02

- Added plan refs using repeatable `[ref:<path>:<Lstart>-<Lend>]` metadata with runtime `ref_context` reads gated by allowed roots from `ORCH_REF_ALLOWED_ROOT` and, on current main, auto-derived `ORCH_SESSION_ROOTS` repo roots.
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
