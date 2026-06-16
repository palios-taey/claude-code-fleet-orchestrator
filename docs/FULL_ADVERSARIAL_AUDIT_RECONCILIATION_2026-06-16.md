# Full Adversarial Audit Reconciliation - 2026-06-16

Project: `full-adversarial-audit-2026-06-16`

Task: `full-adversarial-audit-2026-06-16::reconcile-peer-results`

Supervisor: `conductor-codex`

Inputs:

- `docs/FULL_ADVERSARIAL_AUDIT_SURFACE_MAP_CODEX_2026-06-16.md`
- `docs/FULL_ADVERSARIAL_AUDIT_SURFACE_MAP_GEMINI_2026-06-16.md`
- `docs/FULL_ADVERSARIAL_AUDIT_SURFACE_MAP_GROK_2026-06-16.md`
- `docs/FULL_ADVERSARIAL_AUDIT_INVARIANT_CODEX_2026-06-16.md`
- `docs/FULL_ADVERSARIAL_AUDIT_INVARIANT_GEMINI_2026-06-16.md`
- `docs/FULL_ADVERSARIAL_AUDIT_INVARIANT_GROK_2026-06-16.md`
- `docs/FULL_ADVERSARIAL_AUDIT_RUNTIME_GATE_CODEX_2026-06-16.md`

Status: reconciliation input for the final report, not the final report.

## Target Resolution

Observed: the audit artifact branch is based on `origin/main` and does not contain PR #108's hotfix commit. Codex verified that the hotfix commit is not an ancestor of the artifact branch, and the current artifact tree still imports broad session-local `ORCH_*` keys in `context_assembler.py`.

Reconciled target:

- Findings against `main` apply to the artifact branch unless explicitly marked hotfix-only.
- PR #108 behavior is valid evidence only when cited from commit `653c63731297612c90aa7d7631d749f3d3d13e36`.
- Final audit must not say "fixed on this branch" for the session `.env` poisoning vector. It must say "mitigated by PR #108, not yet present on this audit artifact branch."

## Consensus Findings

### C1 - Task Completion Evidence Gate Holds

Register: Observed.

Auditor agreement: Codex, Gemini, and Grok agree.

Resolution: confirmed.

Evidence:

- `orch_schema.py` validates terminal task evidence before normal task status writes.
- Human-review gates have a separate completion path.
- No direct task `completed` writer was found outside the validated normal path and the human-review path in reviewed searches.
- Runtime acceptance covered the task completion evidence path against isolated Neo4j/Redis.

Caveat: evidence is shape-only. A syntactically valid false SHA can satisfy the gate. This is not a hidden bypass; it is the documented trust boundary.

### C2 - Main Branch Session `.env` Poisoning Is Real

Register: Observed.

Auditor agreement: Codex, Gemini, and Grok agree on the class; Codex and Grok emphasize branch distinction.

Resolution: critical/high on `main`; mitigated by PR #108 for the wake/context path.

Evidence:

- Main/artifact branch imports all session-local `ORCH_*` keys during context assembly.
- PR #108 changes that path to `SESSION_ENV_ALLOWLIST = {"ORCH_RULES_ROOT", "ORCH_SESSION_ROOTS"}`.

Final report treatment: merge PR #108, then continue with issue #109 for removal of request-path global env mutation rather than relying forever on an allowlist.

### C3 - Mutable API Auth Posture Is As Claimed, But Risky

Register: Observed.

Auditor agreement: Codex, Gemini, and Grok agree.

Resolution: accepted-risk finding.

Evidence:

- Mutable auth is enforced only when `ORCH_AUTH_TOKEN` is set.
- Non-loopback/no-token startup warns rather than refusing startup.

Final report treatment: not a hidden backdoor under the stated local single-user threat model, but high operational risk if deployed beyond loopback/trusted LAN.

### C4 - Project `force` Completion Is Separate From Task Evidence

Register: Observed.

Auditor agreement: Codex, Gemini, and Grok agree.

Resolution: confirmed semantic risk.

Evidence:

- Project completion can set project status `completed` with `force: true` while tasks remain incomplete.
- This does not bypass task-level evidence because it is project state, not task state.

Final report treatment: high if project `completed` is consumed as proof of done/ship; medium if it is explicitly manual close/abandon bookkeeping. Recommended fix is to distinguish forced closure from natural completion in persisted state and UI.

### C5 - Shippability Fails Closed on Missing Gates, But Does Not Check Evidence Locally

Register: Observed/Inferred.

Auditor agreement: Codex and Gemini agree; Grok's task-completion review supports the upstream invariant.

Resolution: partially confirmed.

Evidence:

- No matching configured gate tasks means not shippable.
- Completed gate status drives shippable success.
- The "with evidence" part is inherited from task completion writers, not checked in the shippability evaluator.

Final report treatment: medium finding. Add local evidence checks in shippability to remove dependence on legacy/manual DB assumptions.

### C6 - `/ship` Is A Verdict Endpoint, Not A Ship State Transition

Register: Observed.

Auditor agreement: Codex and Gemini agree; Grok classifies as state-transition semantic risk.

Resolution: confirmed semantics mismatch.

Evidence:

- `/ship` returns shippability verdict data and does not mutate project status to `shipped`.

Final report treatment: low/medium depending on documentation and client usage. Rename or document as "ship verdict" unless product intent is to persist a shipped event.

### C7 - Disabled Loop Endpoints Return Success-Looking No-Ops

Register: Observed.

Auditor agreement: Codex, Gemini, and Grok agree on behavior; severity differs.

Resolution: confirmed misleading-success risk.

Evidence:

- Disabled loop declaration/advance/should-stop endpoints return success envelopes with `ok: true` and `enabled: false`.
- Runtime loop acceptance confirms this is the expected current behavior.

Severity reconciliation: low if all clients check `enabled`; medium/high if any automation checks only `ok`.

Final report treatment: return explicit disabled errors or document the envelope contract and audit clients.

### C8 - Gate Runner Shell Execution Is Intentional But Trust-Boundary Sensitive

Register: Observed/Inferred.

Auditor agreement: all auditors found the same surface. Gemini classifies it as accepted risk; Codex and Grok keep the trust boundary open.

Resolution: accepted risk only if gate definitions are trusted local/operator-authored input.

Evidence:

- `gate_runner.py` executes string gate commands with `shell=True`.
- The module docstring says gate definitions are trusted and gate quality is a judgment-review problem.

Final report treatment: not a hidden backdoor under trusted gate definitions. If untrusted users can author gates, it becomes command execution.

### C9 - Broad Exceptions Are A Real Audit Gap

Register: Observed/Inferred.

Auditor agreement: Codex and Grok call it unresolved; Gemini treats several fail-open paths as documented.

Resolution: unproven safety.

Evidence:

- Grok counted 80+ broad `except Exception` sites.
- Codex confirmed broad handlers in state/wake/setup paths.

Final report treatment: not one defect by itself, but a required follow-up: classify critical broad handlers as intentional fail-open, intentional fail-closed, harmless best-effort, or defect.

### C10 - Public Identifier Exposure Is Accepted For This Audit

Register: Observed/Accepted Risk.

Auditor agreement: Gemini and Codex originally surfaced public UI exposure. User explicitly accepted public identifiers/owners/supervisors as visible UI data for this audit.

Resolution: remove as a defect. Keep only the existing readonly-surface assertions: separate app, GET-only routes, docs disabled, pointer-only refs.

## Disagreements Resolved

### Defaults Are Generic vs Local `.env` Contamination

Gemini claim: defaults are generic and not operator-specific.

Codex runtime finding: local env-contract acceptance failed because this checkout contains a Mira-specific `.env` that config auto-loads.

Resolution:

- Code defaults in `config.py` are generic loopback defaults.
- A repo-local deployment `.env` changes runtime defaults before tests instantiate config.
- Clean GitHub checkout likely passes; local exact run in this checkout does not.

Final report treatment: distinguish code default from working-tree local deployment state. This is not a code-default contradiction, but it is a reproducibility risk for local "minimal config" tests.

### Self-Contained Install Test Failure

Gemini claim: de-umbilical/default-path acceptance confirms generic installability.

Codex runtime finding: standalone sessions acceptance failed because the current checkout path equals the historical banned `/home/mira/claude-code-fleet-orchestrator` sentinel.

Resolution:

- `gate_runner.py` uses dynamic `repo_root()`, not a hardcoded path.
- This checkout path makes the test unable to distinguish dynamic repo root from the old baked literal.

Final report treatment: local false positive / test sentinel caveat, not a confirmed product defect.

### Feature Toggles

Gemini claim: feature toggles match docs and no hidden off-switch for core gates.

Codex/Grok finding: disabled loop and wake packet endpoints can return success-looking disabled envelopes.

Resolution:

- No hidden core evidence/stop gate off-switch was found.
- Some disabled optional feature endpoints use success envelopes that can mislead clients.

Final report treatment: split these concepts instead of treating them as one claim.

## Runtime Gate Reconciliation

Observed: Codex ran the workflow acceptance scripts against isolated Neo4j/Redis. After correcting runner namespace mistakes and doc artifact drift, major runtime invariants passed, including evidence gate, stop resolver, wake atomicity, human-review gate path, current-work reconciliation, state integrity, claim atomicity, refs/loop/wake behavior, and decision receipts.

Remaining local blockers/caveats:

- System Python blocks local editable install via PEP 668.
- Repo-local `.env` pollutes env-contract minimal-default test.
- Local checkout path creates a false positive in the standalone install sentinel check.

Resolution: runtime evidence supports the main invariants, but the final report must not claim a completely clean local exact ship-gate run.

## Final Report Carry-Forward

Confirmed or accepted findings to carry forward:

1. Task completion evidence gate holds in reviewed paths.
2. Evidence is shape-only, not provenance proof.
3. Main branch session `.env` poisoning is real; PR #108 mitigates the wake/context path.
4. Project `force` completion can mark projects completed while tasks remain incomplete.
5. Shippability does not locally inspect gate evidence.
6. Mutable API tokenless/warn-only posture is accepted but operationally risky.
7. `/ship` is a verdict endpoint, not a state transition.
8. Disabled loop endpoints return success-looking disabled envelopes.
9. Gate runner shell execution is accepted only under trusted gate definitions.
10. Broad exception safety remains unproven and needs classification.
11. Local `.env` auto-loading affects reproducibility of "minimal config" tests.
12. Public IDs/owners/supervisors are accepted visible UI data for this audit.

No auditor found:

- hidden hardcoded credentials
- hidden direct unauthenticated DB backdoor
- hidden direct task-completed writer outside the two reviewed completion paths
- hidden untrusted eval path in the frozen reviewed tree

## Reconciliation Verdict

Register: Observed/Inferred.

The three-auditor comparison substantially converges. The repo is not clean enough to call "fully audited and risk-free", but the most dangerous suspected backdoor class is concrete and already has a narrow PR (#108). The remaining problems are mostly semantic trust-boundary issues, local deployment contamination, and audit gaps rather than discovered secret exfiltration or hidden direct task completion bypasses.
