# Gemini Invariant Audit - Full Adversarial Audit - 2026-06-16

Project: `full-adversarial-audit-2026-06-16`

Task: `full-adversarial-audit-2026-06-16::invariant-audit-gemini`

Auditor: `conductor-gemini`

Source: result delivered to `taey:conductor-codex:inbox`.

## Target and Scope

Target: `origin/main` (`19a45d0`) plus PR #108 (`653c637`).

Scope: invariant claims audit.

## Backdoor Search and Shell/Subprocess

Checked terms:

```text
force
ok: true
enabled: false
ORCH_
CF_
os.environ
subprocess
shell=True
eval(
exec(
except Exception
session.run
SET .*status
completed
shipped
auth
token
password
secret
write_text
read_text
open(
Path(
```

Findings:

- `shell=True`: used only in `gate_runner.py` for executing `assert_cmd`; Gemini classifies this as accepted risk because gate commands intentionally need arbitrary shell constructs.
- `exec(`: used in `easy_setup.py` to read `version.py`; Gemini classifies this as safe because it reads a local tracked file, not attacker-controlled input.
- `ok: true, enabled: false`: used in `tasks_api.py` loop endpoints when `ORCH_LOOPS_ENABLED` is false; Gemini classifies this as contradicted/misleading success.

## Claims Matrix

### 1. Local / Single-User Threat Model

- API/infrastructure assumes trusted local operator: Confirmed, Observed.
- Mutable API tokenless unless `ORCH_AUTH_TOKEN` is set: Confirmed, Observed. Evidence: `tasks_api.py:_optional_mutable_auth`.
- Non-loopback unauthenticated exposure logs a warning rather than refusing startup: Confirmed, Observed. Evidence: `tasks_api.py:_warn_if_mutable_api_exposed`.

### 2. Install and Uninstall Safety

- Dry-run writes nothing: Confirmed, Observed. Evidence: `scripts/install` short-circuits.
- Installer writes settings atomically: Confirmed, Observed. Evidence: `easy_setup.py` `atomic_write_text` uses tempfile and `os.replace`.
- Hook/settings ownership is tracked: Confirmed, Observed. Evidence: `easy_setup.py` managed marker logic.
- Uninstall is surgical by default: Confirmed, Observed. Evidence: `scripts/orch` calls permission guard removal by default.
- Explicit restore is destructive only after preflight: Confirmed, Observed. Evidence: `scripts/orch` prints preflight restore diff.

### 3. Configuration and Env Boundaries

- Required env fails loud: Confirmed, Observed. Evidence: refs fail on missing `ORCH_REF_ALLOWED_ROOT`.
- Defaults are generic and not operator-specific: Confirmed, Observed. Evidence: `tests/standalone_sessions_acceptance.py`.
- Session `.env` cannot poison shared process config after PR #108: Confirmed, Observed. Evidence: hotfix `SESSION_ENV_ALLOWLIST`.
- Feature toggles match docs and have no hidden off-switch for core gates: Confirmed, Observed.

### 4. Task Lifecycle and Evidence Gates

- Terminal task completion requires evidence: Confirmed, Observed. Evidence: `_validate_terminal_status_write`.
- Evidence is shape-only, not provenance proof: Confirmed, Observed. Evidence: `_evidence_value_well_formed`.
- Human-review gates have a separate completion path: Confirmed, Observed. Evidence: `complete_human_review_gate`.
- Direct or alternate writers cannot set completed without evidence: Confirmed, Observed.

### 5. Project and Shippability Semantics

- No gate tasks means not shippable: Confirmed, Observed. Evidence: `shippability.py`.
- Ship gates must be complete before `/ship` returns success: Confirmed, Observed. Evidence: `tasks_api.py` `/ship` endpoint.
- `/ship` is read-only and does not mutate project status to `shipped`: Observed note.
- Shippability evidence claim depends on upstream task evidence unless locally checked: Confirmed, Observed. Evidence: `shippability.py` checks `status == "completed"`, relying on C1 evidence gate.
- Project `force` completion is distinct from evidence-gated task completion and must not be mistaken for shipped/done evidence: Confirmed, Observed. Evidence: `complete_project(... force=True)` bypasses incomplete-task checks.

### 6. Stop Discipline and Liveness

- Ready work blocks stop where enforcement applies: Confirmed, Observed. Evidence: `_raw_stop_decision`.
- Handoff/stop enforcement gaps G1-G3 are accurately documented: Confirmed, Observed.
- Redis/liveness failure modes match fail-open/fail-closed docs: Confirmed, Observed. Evidence cited by Gemini: `scripts/orch-watch` masks Redis exceptions globally, failing open for liveness escalation.

### 7. Wake Packet and Context Safety

- Dynamic context is optional and fail-open for wake reliability: Confirmed, Observed. Evidence: wake-packet fail-open contract in `tasks_api.py`.
- Untrusted refs/memory/rules are nonce-wrapped: Confirmed, Observed. Evidence: context assembler untrusted nonce path.
- Session env loading cannot import unsafe `ORCH_*` keys after PR #108: Confirmed, Observed. Evidence: `SESSION_ENV_ALLOWLIST`.

### 8. Refs and Filesystem Boundary

- Refs are disabled until `ORCH_REF_ALLOWED_ROOT` is set: Confirmed, Observed.
- Ref paths cannot escape source root or allowed root: Confirmed, Observed. Evidence: absolute paths and `..` rejected.
- Non-regular/oversized/unreadable refs do not silently become trusted content: Confirmed, Observed. Evidence: regular-file/stat checks and warnings.

### 9. Public Readonly Surface

- Separate app, GET-only routes, docs disabled, pointer-only refs: Confirmed, Observed. Evidence: `public_readonly.py`.
- Public identifiers/owners/supervisors are accepted as visible UI data by Jesse for this audit: Accepted Risk.

### 10. Scripts, CLIs, Subprocess, and Shell Surfaces

- CLI help and docs match behavior: Confirmed, Observed.
- Subprocess calls do not introduce shell injection: Confirmed, Observed, with `shell=True` accepted for builder gates.
- Disabled features do not return misleading success unless documented/accepted: Contradicted. Evidence: loop declaration returns `200 OK {"ok": true, "enabled": false}` when disabled.

### 11. CI / Release Gates

- Ship gate workflow name matches actual coverage: Confirmed, Observed.
- R5 audit gate enforces required status contexts as documented: Confirmed, Observed. Evidence cited: `scripts/orch-pre-merge-gate`.
- Version-tag consistency prevents release version drift: Confirmed, Observed. Evidence: `.github/workflows/version-tag-consistency.yml`.

## Gemini Summary

Gemini verdict: implementation adheres closely to documented claims. PR #108 seals the environment-poisoning attack vector. Notable deviations/risks are API ergonomics and semantics: misleading HTTP 200 on disabled loop features and the read-only nature of `/ship`.
