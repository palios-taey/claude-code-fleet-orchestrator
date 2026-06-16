# Adversarial Claims/Code Audit - 2026-06-16

Repo: `claude-code-fleet-orchestrator`

Audit status: **initial adversarial summary, not a final every-line certification**.

Coordinator: `conductor-codex`, filling in for `conductor` / Claude while Claude is at usage limit.

Peer audit status:

- `conductor-grok`: response received and incorporated.
- `conductor-gemini`: response received and incorporated in the follow-up update to this report.

Branches/SHAs considered:

- `origin/main`: `19a45d052ca54a22ef98ea898e244d99884ebfb6`
- Hotfix PR branch: `context-env-boundary-hotfix` at `653c63731297612c90aa7d7631d749f3d3d13e36`
- PR: https://github.com/palios-taey/claude-code-fleet-orchestrator/pull/108
- Larger issue filed before this audit: https://github.com/palios-taey/claude-code-fleet-orchestrator/issues/109

## Executive Read

Observed: the repo's core audit claims are more honest than they first look. Many sharp edges are explicitly documented in `AUDIT.md`, `README.md`, `docs/CONFIGURATION.md`, and tests. The task completion evidence gate is real for normal task completion paths, the public readonly service is GET-only, ship gates fail closed when no gate tasks exist, and mutable auth is intentionally tokenless by default unless `ORCH_AUTH_TOKEN` is set.

Observed: there are still operationally serious issues. The current `main` branch imports every `ORCH_*` key from a session repo `.env` during wake packet assembly, which is exactly the class of bug that caused the recent shared-process config poisoning incident. PR #108 narrows that to context keys only, but `main` remains vulnerable until the PR merges.

Observed: project completion has an explicit `force: true` bypass for unfinished tasks. This is tested as intended behavior. That does not bypass task-level evidence, but it can mark a project completed while work remains incomplete. If project status is used as "done", "ship", or accountability evidence, this is a high-risk semantic bypass.

Observed: shippability claims "completed with evidence", but `evaluate_shippability()` only checks gate task status. The evidence requirement is enforced upstream by the task completion path, not locally by shippability. That dependency is acceptable only if all completed task rows were produced by the gated writer and legacy/manual database writes are out of scope.

Observed: Gemini endorsed the C1-C9 invariants and G1-G3 documented gaps, but found three API-contract sharp edges: flat completion evidence accepts `production_observation` but not flat `commit_sha`/`gate_run_id`, `/ship` returns a verdict without mutating project state, and disabled loops return `ok: true`.

Inferred: this repo should not be merged broadly as "clean" until PR #108 is merged and the project-completion/shippability semantics are explicitly reconciled. PR #108 itself is narrowly scoped and should be considered for merge because it fixes a real production outage class without expanding behavior.

Unknown: a literal every-line audit of the roughly 30K lines under source/scripts/tests/docs is not complete. This file is a control sheet and first-pass adversarial summary with concrete findings to drive the next pass.

## High Findings

### F1 - `main` still has request-time session `.env` poisoning

Severity: High on `main`; mitigated on PR #108.

Observed on `main`: `fleet_orchestrator/context_assembler.py:230-244` reads a session repo `.env` and imports every key that starts with `ORCH_` via `os.environ.setdefault(key, value.strip())`.

Observed on hotfix branch `653c637`: PR #108 adds a `SESSION_ENV_ALLOWLIST` containing only `ORCH_RULES_ROOT` and `ORCH_SESSION_ROOTS`, and updates the regression test.

Why this matters: the FastAPI process uses global singleton Redis/Neo4j drivers. Importing arbitrary per-session `ORCH_NEO4J_*`, `ORCH_REDIS_*`, or auth-related keys into process env can poison unrelated sessions and cause driver config mismatch failures.

What would invalidate this finding: `main` contains the same allowlist behavior as PR #108, or wake packet assembly no longer mutates process-global env.

Recommended control: merge PR #108, then tackle issue #109 to remove request-path global env mutation entirely.

### F2 - Project completion can bypass unfinished work with `force: true`

Severity: High if project status is treated as authoritative completion; Medium if project completion is only cosmetic/manual bookkeeping.

Observed: `fleet_orchestrator/tasks_api.py:712-723` exposes `POST /api/projects/{project_id}/complete` and passes `force=_strict_force_flag(data)`.

Observed: `fleet_orchestrator/orch_schema.py:3311-3331` sets `p.status = 'completed'` when `$force` is true, regardless of unfinished tasks.

Observed: `tests/ref_feature_acceptance.py:303-305` asserts this behavior: `"force-true-bypasses"`.

Why this matters: task completion is evidence-gated, but project completion is not. A caller can leave tasks unfinished and still mark the containing project completed.

What would invalidate this finding: product semantics explicitly state forced project completion is a manual close/abandon action and cannot be consumed as evidence of done/ship anywhere.

Recommended control: rename or split the state so forced close is not represented as the same `completed` state as naturally completed projects. At minimum, persist `force`, `completed_by`, and a required reason in Neo4j, and ensure ship/done dashboards distinguish forced closure.

## Medium Findings

### F3 - Shippability depends on upstream evidence rather than checking evidence locally

Severity: Medium.

Observed: `fleet_orchestrator/shippability.py:57-69` marks shippable when every configured gate task has `status == "completed"`. It does not inspect `completion_evidence`.

Observed: `fleet_orchestrator/orch_schema.py:119-162` performs shape-only evidence validation, and `fleet_orchestrator/orch_schema.py:2521-2577` validates evidence before normal terminal task writes.

Why this matters: the shippability reason says "all ship-gates completed with evidence", but the shippability module proves only "completed". The evidence part is an inferred invariant from the writer path. Legacy rows, manual Cypher writes, migrations, or alternate writers could break that invariant.

What would invalidate this finding: `get_project_summary()` guarantees every completed gate task includes decoded completion evidence, and `evaluate_shippability()` rejects completed gates missing evidence.

Recommended control: make `evaluate_shippability()` directly reject gate tasks with missing/empty evidence, even if the normal writer should already prevent them.

### F4 - Mutable API is tokenless by default and only warns on non-loopback exposure

Severity: Medium; as-claimed.

Observed: `fleet_orchestrator/config.py:163-166` documents `ORCH_HOST=127.0.0.1` and `ORCH_AUTH_TOKEN` unset by default.

Observed: `fleet_orchestrator/tasks_api.py:165-183` only logs a warning when mutable API is bound to a non-loopback host without a token; it does not refuse startup. Auth is enforced only when `ORCH_AUTH_TOKEN` is set.

Why this matters: this is acceptable for a single-user local tool only if deployment remains local/trusted. In the fleet environment, accidental non-loopback bind plus no token makes every mutable route reachable without credentials.

What would invalidate this finding: startup refuses non-loopback mutable API exposure unless an explicit override is set, or deployment controls prove non-loopback is never used without a token.

Recommended control: keep the documented local default, but require an explicit `ORCH_ALLOW_UNAUTH_NON_LOOPBACK=1` for warn-only non-loopback startup.

### F5 - Flat PATCH completion evidence rejects some valid evidence keys

Severity: Medium.

Observed: `fleet_orchestrator/evidence_contract.py:4` defines top-level request evidence keys as only `reason`, `error`, and `production_observation`.

Observed: `fleet_orchestrator/tasks_api.py:370-379` lifts only those top-level keys into the `completion_evidence` object when callers omit the nested `evidence` block.

Why this matters: `PATCH /api/task/{task_id}` with `{"status": "completed", "production_observation": "..."}` works, but `{"status": "completed", "commit_sha": "abcd1234"}` or `{"status": "completed", "gate_run_id": "run-123"}` is treated as no evidence and rejected. Callers must use nested `{"evidence": {"commit_sha": "..."}}` for those keys. This is likely a client/API contract footgun, not a data integrity bypass.

What would invalidate this finding: flat top-level `commit_sha` and `gate_run_id` are intentionally unsupported and docs/CLI examples consistently require the nested `evidence` object.

Recommended control: either document that only `production_observation`, `reason`, and `error` are lifted from flat PATCH payloads, or add `commit_sha` and `gate_run_id` to the lifted request keys.

### F6 - `/ship` is a readonly verdict endpoint despite being a POST

Severity: Low.

Observed: `fleet_orchestrator/tasks_api.py:736-743` evaluates shippability and returns `{"ok": true, "shippable": true, "verdict": ...}` when gates pass, but does not update Neo4j project state to `shipped`.

Why this matters: automated callers may read the endpoint name and POST method as a state transition. The implementation is a gate verdict, not a ship-state mutation.

What would invalidate this finding: product semantics explicitly define `/ship` as a pure "can ship" assertion endpoint, and no UI/automation treats it as a project state transition.

Recommended control: rename the route/response in docs to "ship verdict", or persist an explicit shipped/ship_attempt event if the API is intended to transition state.

### F7 - Disabled loop declaration returns `ok: true`

Severity: Low.

Observed: `fleet_orchestrator/tasks_api.py:959-962` returns `{"ok": true, "enabled": false}` when `ORCH_LOOPS_ENABLED=0`.

Why this matters: pipelines that only check `ok` can believe loop declaration succeeded even though the feature is disabled. This is similar to a silent mock.

What would invalidate this finding: all clients check `enabled` and docs explicitly state disabled loop endpoints return a non-operative success envelope.

Recommended control: return `ok: false` with `enabled: false`, or use `403`/`501` when loop support is disabled.

## Claims Matrix

| Claim | Audit Result | Evidence |
| --- | --- | --- |
| C1: Task completion is evidence-gated | Mostly confirmed | Normal task completion calls `_validate_terminal_status_write()` before status writes in `orch_schema.py:2521`; human-review gate has its own path. Caveat: project completion is separate and forceable. |
| C3: Evidence is shape-only, not provenance proof | Confirmed | `orch_schema.py:119-162` validates cheap shape: SHA length/hex, gate ID chars, production observation length. |
| C5: Mutable API tokenless by default | Confirmed | `tasks_api.py:165-183`; auth only enforced when `ORCH_AUTH_TOKEN` is set. |
| C6: Public service is readonly and scrubbed | Partly confirmed | Routes are GET-only, docs disabled, and text scrubbers exist. Raw IDs/owners/supervisors remain visible. |
| C7: Ship gates fail closed | Partly confirmed | No matching gate tasks returns `shippable: False`; completed gates are checked by status only. |
| C8: No operator-specific identity/path defaults | Not fully audited | Tests exist, but full shipped-surface scan was not rerun in this pass. |
| C9: Version source avoids drift | Not audited in this pass | Needs version-specific test run. |
| G1/G2/G3 documented gaps | Confirmed by peer report, not exhaustively reverified | Grok confirmed per-session enforcement gaps and wake flag boundary gaps as documented. |

## Peer Findings Incorporated

`conductor-grok` reported:

- Singleton Redis/Neo4j driver publication order now appears fixed: config is assigned before client/driver publication in `config.py`.
- PR #108 mitigates the immediate context env poisoning class by narrowing the session env allowlist.
- C1/C3/C6 largely match code.
- Gaps G1-G3 are present as documented.
- Exposure posture is risky by design but accurately documented.
- Broad `except Exception` blocks and script/env surfaces remain accountability risks.

`conductor-gemini` later reported:

- Verdict: endorse. C1-C9 confirmed; G1-G3 confirmed.
- Medium: flat PATCH completion payloads reject flat `commit_sha`/`gate_run_id`; callers must nest those under `evidence`.
- Low: `/api/projects/{project_id}/ship` returns a shippability verdict but does not mutate project status to `shipped`.
- Low: disabled loop declaration returns `ok: true, enabled: false`, which can mislead callers that only check `ok`.

## Commands Run

Representative local commands:

```bash
git status --short --branch
git log --oneline -5
rg -n "claim|guarantee|must|never|always|required|acceptance|PASS|gate|evidence|public|readonly|auth|config|wake-packet|dispatch|current_task|Neo4j|Redis|install|CI|shippable|production|health" README.md docs CLAUDE.md CHANGELOG.md AUDIT.md SECURITY.md SETUP.md .github fleet_orchestrator scripts tests -S
find fleet_orchestrator scripts tests docs .github -maxdepth 2 -type f | sort | xargs wc -l
git ls-files '*__pycache__*' '*.pyc' 'build/*' 'fleet_orchestrator.egg-info/*'
rg -n "complete_project|evaluate_shippability|force|ship|_terminal_evidence_from_request|@app\\.(post|patch|put|delete)" fleet_orchestrator/tasks_api.py fleet_orchestrator/orch_schema.py fleet_orchestrator/shippability.py tests docs AUDIT.md README.md -S
redis-cli -h 127.0.0.1 LRANGE taey:conductor-codex:inbox 0 -1
```

## Cleanliness Notes

Observed: generated directories/files such as `__pycache__`, `build`, and `fleet_orchestrator.egg-info` existed on disk during inspection, but `git ls-files` did not show them as tracked.

Observed: this report was created on branch `adversarial-audit-2026-06-16` from `origin/main` so the existing hotfix PR branch is not polluted with audit documentation.

## Recommended Merge Decision

PR #108: merge after normal review/checks. It fixes a real production incident class and is narrowly scoped.

Do not treat the repo as broadly clean yet. The next cleanup pass should address:

1. Project forced completion semantics.
2. Shippability evidence checking in the shippability module itself.
3. Request-time global env mutation removal beyond the immediate allowlist.
4. Non-loopback unauthenticated mutable API startup policy.
5. PATCH evidence contract consistency for flat vs nested evidence payloads.
6. `/ship` and disabled-loop endpoint response semantics.
7. Full rerun of all acceptance scripts listed in `.github/workflows/ship-gate.yml`.
