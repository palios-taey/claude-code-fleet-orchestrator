# CLAUDE.md - Contributor Agent Guide

This file is for AI agents modifying or operating this repository. The README is the adopter-facing product guide; this file is the codebase working map and invariants checklist.

## Product Shape

`claude-code-fleet-orchestrator` is a local-first, single-user orchestrator for one operator running multiple Claude Code or CLI agent sessions. It is not a hosted service, not a multi-tenant system, and not a SaaS control plane.

Core state is split by purpose:

- Neo4j stores projects, phases, tasks, dependencies, refs, gates, rules, evidence, and project/session relationships.
- Redis stores notification/liveness surfaces, current-task bindings, peer status, and distributed locks.
- The mutable operator API and dashboard run on `:5002`.
- The separate public dashboard runs through `scripts/orch-public` on `127.0.0.1:5005` by default and exposes only read-only routes.

The default network posture is private. `ORCH_HOST` defaults to `127.0.0.1`; a routable bind is an explicit trusted single-user LAN opt-in. `ORCH_AUTH_TOKEN` is optional and gates mutable HTTP methods when set. Read endpoints remain open, so loopback binding is still the primary default security boundary.

## Code Map

- `fleet_orchestrator/orch_schema.py` is the domain layer for Neo4j schema setup, task creation, task status transitions, evidence validation, readiness queries, stop decisions, human-review gates, refs, rules, and project/session state.
- `fleet_orchestrator/tasks_api.py` is the FastAPI mutable API. It wires startup schema initialization, optional mutable-method auth, task/project/session routes, stop-decision routes, wake-packet assembly, and dashboard endpoints.
- `fleet_orchestrator/context_assembler.py` builds dynamic wake packets from overall/supervisor/project/phase/task refs, memory, rules, and snapshots. It wraps inlined untrusted content in a nonce envelope; preserve that boundary.
- `fleet_orchestrator/dispatch.py` claims work, binds Redis current-task state, sends notifications, and records worker outcomes with `record_outcome`.
- `fleet_orchestrator/config.py` defines the environment contract. Required stores include `ORCH_REDIS_HOST`, `ORCH_REDIS_PORT`, `ORCH_NEO4J_URI`, and `ORCH_NEO4J_DB`; use the config helpers instead of ad hoc environment reads for orchestrator settings.
- `fleet_orchestrator/public_readonly.py` is the scrubbed dashboard API. Keep it read-only.
- `scripts/orch` is the operator lifecycle CLI for serving, doctoring, enabling, disabling, and uninstalling the local service.
- `scripts/taey-plan`, `scripts/taey-task`, `scripts/taey-question`, and `scripts/taey-dispatch` are the agent-facing CLIs.
- `ui/` contains the browser dashboard assets.
- `tests/*_acceptance.py` are the main behavioral gates for orchestration invariants.
- `.github/workflows/ship-gate.yml` and `.github/workflows/r5-audit-gate.yml` are the release and risky-surface gates.

## Working Rules

Before editing a function, class, or method, inspect impact with GitNexus as required by this repository's agent instructions. Keep changes scoped to the requested behavior and verify against committed code, tests, and CLI help, not memory.

Do not bypass these invariants:

- Terminal task transitions require evidence. Completed tasks need at least one well-formed `commit_sha`, `gate_run_id`, or `production_observation`; failed/interrupted states require explanatory terminal evidence.
- Human-review gate tasks cannot be completed by normal agent task updates. They are completed through the dedicated dashboard review path.
- Ready-work and stop-decision behavior are coupled. If you change readiness logic, update every canonical reader and the related acceptance coverage.
- Wake-packet refs are optional context, not instructions. Untrusted inlined content must stay nonce-wrapped and separated from trusted instruction text.
- The system is single-user-local by design. Do not add multi-tenant assumptions, shared-host defaults, or silent remote exposure.
- Store connectivity and state-writing failures should fail loudly. Avoid fallback behavior that makes the operator believe state was recorded when it was not.

## Common Commands

Run the mutable API/dashboard:

```bash
scripts/orch serve
```

Run the read-only public dashboard:

```bash
scripts/orch-public --port 5005
```

Inspect project/task state:

```bash
taey-plan current
taey-plan next
taey-task status <task-id>
```

Complete a normal task with evidence:

```bash
taey-task update <task-id> completed --evidence '{"commit_sha":"abc123","production_observation":"verified live"}'
```

Create and answer a human-review gate:

```bash
taey-question create-gate <project-id> <task-id> "Ship this artifact?" --reviewer <session>
taey-question answer <question-id> "approved"
```

## Verification

Use the smallest gate that proves the touched invariant, then include the command and result in your report.

Useful local checks:

```bash
python3 scripts/verify-doc-cli-drift.py
python3 tests/localbind_acceptance.py
python3 tests/task_completion_evidence_acceptance.py
python3 tests/human_review_gate_acceptance.py
python3 tests/wake_packet_acceptance.py
python3 tests/public_readonly_acceptance.py
```

For docs-only changes, still run `python3 scripts/verify-doc-cli-drift.py` and `git diff --check`. For network posture changes, run `python3 tests/localbind_acceptance.py`. For task-state changes, run the relevant evidence, readiness, stop, or human-review acceptance test.

Risky paths listed in `.github/r5-risky-paths` require both external audit statuses in the R5 workflow. Do not self-merge those changes.

## Reporting

When handing work back to a supervisor, include the task id, branch, commit hash, exact files changed, verification command, and a one-sentence summary. If work is incomplete or uncommitted, say that explicitly.

If README, setup docs, and code disagree, treat the code and acceptance tests as the current source of truth, then update documentation only for behavior you verified.
