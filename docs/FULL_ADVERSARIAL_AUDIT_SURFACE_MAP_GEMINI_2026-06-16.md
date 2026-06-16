# Gemini Surface Map - Full Adversarial Audit - 2026-06-16

Project: `full-adversarial-audit-2026-06-16`

Task: `full-adversarial-audit-2026-06-16::surface-map-gemini`

Auditor: `conductor-gemini`

Source: result delivered to `taey:conductor-codex:inbox`.

## Target

`origin/main` (`19a45d0`) plus PR #108 / hotfix (`653c637`), audited on `adversarial-audit-2026-06-16`.

## Commands Run

- `git branch -a`
- `git log`
- grep for routing decorators
- grep for `subprocess`, `exec`, `eval`, `open`, env
- `cat` on script and core Python files

## Files Reviewed

- `fleet_orchestrator/tasks_api.py`
- `fleet_orchestrator/public_readonly.py`
- `fleet_orchestrator/gate_runner.py`
- `fleet_orchestrator/handoff_validation.py`
- `fleet_orchestrator/accountability_ledger.py`
- `fleet_orchestrator/easy_setup.py`
- `fleet_orchestrator/context_assembler.py`

## Surface Inventory

### Mutable/Read Surfaces

Observed:

- FastAPI mutable surface in `tasks_api.py`: 14 `POST`, 3 `PATCH`, 3 `DELETE` under `/api/`, including `/task/create`, `/projects/{id}/ship`, `/loops/declare`.
- FastAPI read-only public surface in `public_readonly.py`: 10 `GET` routes, including `/health`, `/api/projects`.
- No `POST`, `PATCH`, or `DELETE` routes observed in `public_readonly.py`.

### CLI Commands and Scripts

Observed shipped script surfaces:

- `orch`
- `orch-cron`
- `orch-gate-run`
- `orch-pre-merge-gate`
- `orch-public`
- `orch-watch`
- `taey-dispatch`
- `taey-plan`
- `taey-question`
- `taey-task`
- `install`
- `verify-doc-cli-drift.py`
- `verify-public-readonly.py`

### Subprocess Calls

Observed:

- 89 subprocess instances across `easy_setup.py`, `gate_runner.py`, `dispatch.py`, `out_of_band.py`, and scripts.
- `shell=True` used exactly once in `gate_runner.py:64` for executing builder-defined gate assertions.

### Environment Variables

Observed:

- Over 60 env vars, including `ORCH_HOST`, `ORCH_AUTH_TOKEN`, `CF_HANDOFF_ENFORCE`, `CF_STOP_INPROGRESS`, and `ORCH_LOOPS_ENABLED`.

### File IO and Persistence

Observed:

- Neo4j writes via `orch_schema.py`, including `SET t.status='completed'`.
- Redis publishes in `dispatch.py`, `plan_readiness.py`, and `handoff_validation.py`.
- Local appends in `accountability_ledger.py` to `ACCOUNTABILITY_LEDGER_PATH`.
- Reads in `handoff_validation.py` from `CF_HANDOFF_SESSION_FLAGS_FILE`.
- `easy_setup.py` executes `version.py`.

## Claims Verification

### Mutable API Tokenless Unless `ORCH_AUTH_TOKEN` Is Set

Verdict: Confirmed.

Register: Observed.

Evidence: `tasks_api.py:178` `_optional_mutable_auth`.

Severity: Medium, accepted risk.

Invalidated if non-loopback bind without token does not warn or refuses startup.

### Public Readonly Surface Is GET-Only

Verdict: Confirmed.

Register: Observed.

Evidence: `public_readonly.py` uses only `@app.get`.

Severity: High.

Invalidated if any `@app.post` is added to `public_readonly.py`.

### Session `.env` Cannot Poison Shared Process Config After PR #108

Verdict: Confirmed.

Register: Observed.

Evidence: `context_assembler.py:248` limits imports to `SESSION_ENV_ALLOWLIST`.

Severity: High.

Invalidated if `os.environ.setdefault` applies to `ORCH_NEO4J_*`.

### Terminal Task Completion Requires Evidence

Verdict: Confirmed.

Register: Observed.

Evidence: `orch_schema.py:209` `_validate_terminal_status_write`.

Severity: Critical.

Invalidated if `SET t.status='completed'` occurs outside gated paths.

## Backdoor Candidates

### `exec(`

Observed in `easy_setup.py:72` to read `version.py`.

Disposition: safe/accepted because it is scoped to repo files and not attacker-controlled.

### `subprocess(..., shell=True)`

Observed in `gate_runner.py:64`.

Disposition: documented behavior. It executes `assert_cmd` provided by gate definition.

### Omitted Surfaces

Gemini reported no omitted surfaces beyond documented surfaces.

## Overall Verdict

Gemini verdict: confirmed mapping matches documentation.
