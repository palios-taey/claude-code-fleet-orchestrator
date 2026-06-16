# Full Adversarial Audit Final Report - 2026-06-16

Repository: `claude-code-fleet-orchestrator`

Branch: `adversarial-audit-2026-06-16`

Supervisor: `conductor-codex`, filling in for conductor/Claude

Workers: `conductor-codex`, `conductor-gemini`, `conductor-grok`

Project: `full-adversarial-audit-2026-06-16`

## Executive Verdict

Register: Observed/Inferred.

This repository is not a hidden-backdoor disaster, but it is also not clean enough to call fully settled. The core task-completion evidence gate is real in the reviewed paths, and the runtime acceptance scripts substantially support that. The strongest concrete bug is the session `.env` poisoning vector on `main`; PR #108 mitigates it for the wake/context path, but that hotfix is not present on this audit artifact branch.

No auditor found hidden hardcoded credentials, a secret-exfiltration path, an untrusted eval backdoor, or a direct task `completed` writer outside the normal evidence-gated path and the human-review gate path.

The remaining serious issues are semantic and operational:

- Project `force` completion can mark a project completed while tasks remain incomplete.
- Shippability claims evidence but checks only completed gate status locally.
- Mutable API auth is tokenless by default and only warns on non-loopback exposure.
- `/ship` is a verdict endpoint, not a state transition.
- Disabled loop endpoints return success-looking disabled responses.
- Gate execution uses shell commands and is safe only if gate definitions are trusted.
- Broad exception handlers remain an unproven safety surface.
- Local `.env` auto-loading makes "minimal config" tests and local ship-gate runs sensitive to deployment state.

## Scope And Target

Observed target inputs:

- `origin/main`: `19a45d052ca54a22ef98ea898e244d99884ebfb6`
- PR #108 hotfix branch `context-env-boundary-hotfix`: `653c63731297612c90aa7d7631d749f3d3d13e36`
- Audit artifact branch: `adversarial-audit-2026-06-16`

Important branch caveat:

- The artifact branch is based on `origin/main`.
- It does not contain PR #108.
- Hotfix behavior in this report is cited from commit `653c63731297612c90aa7d7631d749f3d3d13e36`, not assumed from the artifact branch.

## Supporting Artifacts

Primary audit artifacts:

- `ADVERSARIAL_AUDIT_2026-06-16.md`
- `docs/FULL_ADVERSARIAL_AUDIT_PLAN_2026-06-16.md`
- `docs/FULL_ADVERSARIAL_AUDIT_PROTOCOL_2026-06-16.md`
- `docs/FULL_ADVERSARIAL_AUDIT_SURFACE_MAP_CODEX_2026-06-16.md`
- `docs/FULL_ADVERSARIAL_AUDIT_SURFACE_MAP_GEMINI_2026-06-16.md`
- `docs/FULL_ADVERSARIAL_AUDIT_SURFACE_MAP_GROK_2026-06-16.md`
- `docs/FULL_ADVERSARIAL_AUDIT_INVARIANT_CODEX_2026-06-16.md`
- `docs/FULL_ADVERSARIAL_AUDIT_INVARIANT_GEMINI_2026-06-16.md`
- `docs/FULL_ADVERSARIAL_AUDIT_INVARIANT_GROK_2026-06-16.md`
- `docs/FULL_ADVERSARIAL_AUDIT_RUNTIME_GATE_CODEX_2026-06-16.md`
- `docs/FULL_ADVERSARIAL_AUDIT_RECONCILIATION_2026-06-16.md`

Runtime logs preserved under `/tmp`:

- `/tmp/full-adversarial-audit-runtime-gate-2026-06-16.log`
- `/tmp/full-adversarial-audit-runtime-gate-2026-06-16.summary`
- `/tmp/full-adversarial-audit-runtime-gate-rerun-2026-06-16.log`
- `/tmp/full-adversarial-audit-runtime-gate-rerun-2026-06-16.summary`

## Method

Observed:

- Codex froze the target branch/SHA and wrote an audit protocol.
- Gemini and Grok independently mapped surfaces and audited invariants.
- Codex performed a local invariant audit and a runtime gate pass.
- Codex reconciled disagreements into one matrix.
- All committed audit artifacts were checked with the repo's doc-drift verifier.

Search and review covered:

- FastAPI mutable/read routes
- CLI scripts
- setup/install/uninstall scripts
- Redis and Neo4j write chokepoints
- subprocess and shell surfaces
- env loading and feature flags
- refs/filesystem boundaries
- public readonly service
- workflow gates
- acceptance scripts

Truth register:

- Observed means verified in code, committed artifact, or command output.
- Inferred means likely from the evidence but not directly proven end-to-end.
- Unknown means not proven and should not be sold as safe.

## Confirmed Findings

### F1 - Main Branch Session `.env` Poisoning

Severity: Critical/High on `main`; mitigated by PR #108 for the wake/context path.

Register: Observed.

Observed:

- On `main`, `context_assembler.py` imports every session-local `ORCH_*` key into process-global environment during context assembly.
- PR #108 changes this to an allowlist: `ORCH_RULES_ROOT` and `ORCH_SESSION_ROOTS`.

Why it matters:

- A session repo `.env` can poison shared process config for Redis/Neo4j/API/auth-related environment.
- This matches the recent production failure class.

Disposition:

- Merge PR #108.
- Keep issue #109 for the larger fix: remove request-path global env mutation rather than relying indefinitely on a narrow allowlist.

### F2 - Task Completion Evidence Gate Holds

Severity: Positive control, with caveat.

Register: Observed.

Observed:

- Normal task terminal writes validate evidence before status writes.
- Human-review gates use a separate dashboard-verified path.
- Reviewed searches did not find a separate direct task `completed` writer.
- Runtime acceptance covered the task completion evidence path.

Caveat:

- Evidence is shape-only. A syntactically valid false SHA can pass the shape check.
- This is not a hidden bypass; it is the current trust boundary.

### F3 - Project `force` Completion Is A Semantic Bypass

Severity: High if project `completed` is treated as done/ship evidence; Medium if treated as manual close bookkeeping.

Register: Observed.

Observed:

- Project completion accepts strict boolean `force`.
- Forced project completion can set project status `completed` while tasks remain incomplete.
- This does not bypass task-level evidence because it is project state, not task state.

Recommended fix:

- Persist forced closure distinctly from natural completion.
- Require reason/actor.
- Ensure UI, ship logic, and reporting do not treat forced closure as evidence-backed completion.

### F4 - Shippability Does Not Check Evidence Locally

Severity: Medium.

Register: Observed/Inferred.

Observed:

- Missing configured gate tasks fail closed.
- Completed gate status drives success.
- Shippability does not locally inspect completion evidence.

Why it matters:

- The "with evidence" claim depends on upstream task-completion writers.
- Legacy/manual rows with completed status and missing evidence could pass unless another layer rejects them.

Recommended fix:

- Make the shippability evaluator reject completed gate tasks that lack valid completion evidence.

### F5 - Mutable API Auth Is Accepted Risk, Not A Hidden Backdoor

Severity: Medium/High operational risk.

Register: Observed.

Observed:

- Mutable auth is enforced only when `ORCH_AUTH_TOKEN` is set.
- Non-loopback/no-token startup warns but does not refuse.

Disposition:

- This matches the local single-user threat model.
- It is risky if deployed beyond loopback/trusted LAN.

Recommended fix:

- Require an explicit override for unauthenticated non-loopback mutable API exposure.

### F6 - `/ship` Is A Verdict Endpoint

Severity: Low/Medium.

Register: Observed.

Observed:

- `/ship` returns shippability verdict data.
- It does not mutate project status to `shipped`.

Recommended fix:

- Rename/document as a ship verdict endpoint, or persist a shipped/ship-attempt event if mutation is intended.

### F7 - Disabled Loop Endpoints Return Success-Looking Disabled Responses

Severity: Low if all clients check `enabled`; Medium/High if clients check only `ok`.

Register: Observed.

Observed:

- Disabled loop declaration/advance/should-stop endpoints return success envelopes with `ok: true` and `enabled: false`.
- Runtime loop acceptance confirms this as current expected behavior.

Recommended fix:

- Return explicit disabled errors, or document the envelope contract and audit every client for `enabled` checks.

### F8 - Gate Runner Shell Execution Is Trust-Boundary Sensitive

Severity: Accepted risk under trusted gate definitions; High if untrusted users can author gate commands.

Register: Observed/Inferred.

Observed:

- `gate_runner.py` executes string gate commands with `shell=True`.
- The module documents that gate definitions are trusted and gate quality is a review problem.

Recommended fix:

- Keep as accepted risk only if gate authors are trusted operators.
- If gate definitions can come from untrusted input, switch to structured/list-form command execution or sandbox execution.

### F9 - Broad Exceptions Remain Unproven

Severity: Medium-High audit gap.

Register: Observed/Inferred.

Observed:

- Grok counted 80+ broad `except Exception` sites.
- Codex confirmed broad handlers in state, wake, and setup paths.

Disposition:

- Not every broad exception is a defect.
- Safety is not proven until critical handlers are classified by intent and effect.

Recommended follow-up:

- Classify each critical broad handler as intentional fail-open, intentional fail-closed, harmless best-effort, or defect.

### F10 - Local `.env` Auto-Loading Affects Test Reproducibility

Severity: Medium for reproducibility; not a shipped-code default contradiction.

Register: Observed.

Observed:

- `config.py` auto-loads `.env` from the current working directory or repo root.
- This checkout contains a Mira-specific `.env` with non-loopback dashboard URL.
- Env-contract acceptance fails locally because the test subprocess loads that `.env`.
- Code defaults still define loopback dashboard URL.

Disposition:

- Clean GitHub checkout likely passes.
- Local exact ship-gate runs in deployment checkouts can diverge from clean CI.

Recommended fix:

- Make acceptance tests explicitly suppress repo-local deployment `.env` where they claim minimal generic defaults, or document local-run caveats.

### F11 - Self-Contained Install Test Has A Local Sentinel Caveat

Severity: Low/Medium testing caveat.

Register: Observed.

Observed:

- `gate_runner.py` uses dynamic `repo_root()`.
- This checkout lives at the historical banned path used by the standalone install test.
- The test cannot distinguish dynamic repo root from a baked literal when run from this path.

Disposition:

- Not a confirmed product hardcoding defect.
- It is a local-run caveat for Mira's checkout path.

## Accepted Risks

Accepted by product posture or user decision:

- Local single-user trusted-operator model.
- Tokenless mutable API on loopback by default.
- Public UI may show project/session identifiers, owners, and supervisors.
- Gate command execution can use shell when gate definitions are trusted.
- Wake packet context can fail open for wake reliability.
- Evidence is shape validation, not cryptographic provenance.

## Claim Matrix

| Claim Family | Verdict | Notes |
|---|---|---|
| Local/single-user threat model | Confirmed / Accepted Risk | Tokenless default is intentional local posture. |
| Install/uninstall safety | Mostly Confirmed | Gemini confirmed dry-run, atomic writes, ownership markers, surgical uninstall. Local standalone test has path caveat. |
| Configuration/env boundaries | Partially Confirmed | Code defaults are generic; main has session `.env` poisoning; PR #108 mitigates context path; local `.env` affects tests. |
| Task lifecycle/evidence | Confirmed | Evidence gate holds in reviewed paths; evidence is shape-only. |
| Project/shippability | Partially Confirmed | No gate tasks fail closed; shippability lacks local evidence check; project force completion is semantic risk. |
| Stop/liveness | Mostly Confirmed | Runtime scripts support stop resolver, supervisor-dispatch stop-block, human-review stop; fail-open gaps are documented but sharp. |
| Wake/context | Partially Confirmed | PR #108 improves boundary; wake packet remains intentionally fail-open. |
| Refs/filesystem | Mostly Confirmed | Allowed-root checks and disabled-by-default posture hold; residual local TOCTOU risk accepted. |
| Public readonly | Confirmed with accepted exposure | GET-only/pointer-only posture confirmed; identifiers accepted as visible UI data. |
| Scripts/subprocess | Accepted Risk / Unproven | Shell gate runner depends on trusted gate definitions; broad exceptions need classification. |
| CI/release gates | Mostly Confirmed | Runtime gate substantially ran; local exact run has PEP 668, `.env`, and path caveats. |

## Runtime Gate Summary

Observed:

- Isolated temporary Neo4j and Redis containers were used to avoid touching live state.
- First pass: 25 passed, 9 failed.
- Five failures were runner namespace mistakes and passed on rerun with workflow namespace.
- Doc-drift failure was introduced by audit artifacts and fixed.
- Final doc-drift check passed.

Remaining local blockers/caveats:

- System Python blocks editable install due PEP 668.
- Repo-local `.env` pollutes env-contract minimal-default acceptance.
- Local checkout path causes standalone install sentinel false positive.

Runtime conclusion:

- Runtime evidence supports the core invariants.
- Do not claim a clean local exact ship-gate run from this checkout.

## What Should Be Merged

Observed/Inferred:

- PR #108 should be merged before advertising the repo as cleaned up. It fixes the concrete env-poisoning bug class on the wake/context path.
- The audit artifact branch should not be merged into `main` as product code; it is a documentation/audit branch.
- Audit findings should become issues or implementation PRs before claiming the repo is fully clean.

## Issue Queue

Open/update issues for:

1. Merge PR #108 and track broader request-path env mutation removal under issue #109.
2. Distinguish forced project closure from natural project completion.
3. Add local evidence checks to shippability.
4. Clarify or change `/ship` verdict semantics.
5. Change disabled loop endpoints to explicit disabled errors or audit every client for `enabled`.
6. Add explicit override requirement for unauthenticated non-loopback mutable API.
7. Classify broad `except Exception` handlers in critical paths.
8. Fix local acceptance reproducibility around repo-local `.env`.
9. Adjust standalone install sentinel so dynamic repo root is not a false positive on Mira.
10. Document the trusted gate-definition boundary for shell execution.

## Final Answer

Register: Observed/Inferred.

The orchestrated Codex/Gemini/Grok audit found real issues, but not the worst-case hidden-backdoor scenario. The biggest immediate action is to merge PR #108, then address the semantic/accountability problems around forced project completion, shippability evidence, and ambiguous success responses. The repo can be made clean, but it should not be represented as fully audited or risk-free until those issues are filed and resolved, and until broad exception paths are classified.
