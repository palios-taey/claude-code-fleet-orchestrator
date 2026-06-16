# Project: full-adversarial-audit-2026-06-16 - Full Claims and Backdoor Audit
> Thorough adversarial audit of claude-code-fleet-orchestrator against README/docs/AUDIT claims, with Codex supervising and also auditing, and Gemini/Grok providing independent cross-checks.

## Phase: define - Freeze Scope and Build Claims Registry [order: 1]

### Task: freeze-target - Freeze target SHA and audit rules [priority: 100] [owner: conductor-codex]
Record the exact audited SHA, branches in scope, excluded local/generated artifacts, accepted risks, truth registers, severity rubric, and output paths.

### Task: extract-claims - Extract every claim from shipped docs and operator surfaces [priority: 98] [owner: conductor-codex] [depends: freeze-target]
Build a claims registry from README, AUDIT, SECURITY, SETUP, CHANGELOG, docs, workflow names, CLI help, public API docs, and comments that promise externally meaningful behavior.

### Task: classify-claims - Classify claims by invariant and risk [priority: 95] [owner: conductor-codex] [depends: extract-claims]
Classify each claim as security, state integrity, installability, shippability, privacy/public surface, config/env, wake/context, task lifecycle, or test/gate claim.

## Phase: measure - Enumerate Code and Runtime Surfaces [order: 2]

### Task: surface-map-codex - Map all mutable/read surfaces locally [priority: 95] [owner: conductor-codex] [depends: classify-claims]
Inventory FastAPI routes, CLI commands, scripts, subprocess calls, env vars, file IO, Redis/Neo4j writes, workflow gates, and feature flags.

### Task: surface-map-gemini - Independent claims/surface map [priority: 94] [owner: conductor-gemini] [depends: freeze-target]
Independently map claims and public/mutable/runtime surfaces. Return Observed/Inferred/Unknown, file:line evidence, and omitted surfaces to conductor-codex.

### Task: surface-map-grok - Independent code/backdoor map [priority: 94] [owner: conductor-grok] [depends: freeze-target]
Independently map state mutation chokepoints, env/config boundaries, shell/process execution, broad exception paths, auth boundaries, and hidden force/fail-open behavior.

## Phase: analyze - Prove or Break Invariants [order: 3]

### Task: invariant-audit-codex - Codex claim-by-claim invariant audit [priority: 96] [owner: conductor-codex] [depends: surface-map-codex]
For every claim, trace enforcing code, bypass writers, tests, and runtime behavior. Mark Confirmed, Contradicted, Unproven, Accepted Risk, or Out of Scope.

### Task: invariant-audit-gemini - Gemini adversarial claims audit [priority: 94] [owner: conductor-gemini] [depends: surface-map-gemini]
Audit every claim against implementation and tests. Focus on docs/README/API/CLI claims, install/release claims, and evidence that would invalidate each claim.

### Task: invariant-audit-grok - Grok adversarial backdoor audit [priority: 94] [owner: conductor-grok] [depends: surface-map-grok]
Audit for bypasses/backdoors independent of docs: unauthenticated mutation, force paths, direct DB writes, env poisoning, fail-open behavior, disabled features returning success, subprocess hazards, filesystem escapes, and silent fallback paths.

### Task: runtime-gate-run - Run authoritative acceptance and targeted adversarial probes [priority: 90] [owner: conductor-codex] [depends: invariant-audit-codex]
Run or explicitly document blockers for the authoritative acceptance scripts in .github/workflows/ship-gate.yml plus targeted negative probes for the highest-risk invariants.

## Phase: improve - Reconcile Findings and Produce Audit Artifacts [order: 4]

### Task: reconcile-peer-results - Compare Codex/Gemini/Grok results [priority: 100] [owner: conductor-codex] [depends: invariant-audit-gemini] [depends: invariant-audit-grok] [depends: runtime-gate-run]
Merge all findings into one matrix. Any disagreement must be resolved with code evidence, a reproducer, or marked Unknown.

### Task: write-final-audit - Write final full audit report [priority: 98] [owner: conductor-codex] [depends: reconcile-peer-results]
Produce a markdown report with claims matrix, surface map, confirmed invariants, contradictions, accepted risks, unknowns, backdoor analysis, command log, and recommended fixes.

### Task: file-fix-issues - File or update GitHub issues for confirmed defects [priority: 85] [owner: conductor-codex] [depends: write-final-audit]
Open/update issues only for confirmed defects or unproven high-risk invariants that need implementation work. Include exact evidence and invalidation criteria.

## Phase: control - Human Review and Completion [order: 5]

### Task: human-review - Human review of final audit verdict [priority: 100] [owner: conductor-codex] [depends: write-final-audit]
Present the final verdict to Jesse and record accepted risks versus required fixes before marking the audit project complete.

## User Stop Conditions
- stop_when_all_ready_tasks_dispatched
