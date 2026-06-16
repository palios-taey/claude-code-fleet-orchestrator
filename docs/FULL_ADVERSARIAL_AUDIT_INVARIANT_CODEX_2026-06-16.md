# Codex Invariant Audit - Full Adversarial Audit - 2026-06-16

Project: `full-adversarial-audit-2026-06-16`

Task: `full-adversarial-audit-2026-06-16::invariant-audit-codex`

Auditor: `conductor-codex`

Status: Codex local invariant pass. This is an input to reconciliation, not the final report.

## Target Caveat

Observed: the artifact branch `adversarial-audit-2026-06-16` is based on `origin/main` and does **not** contain PR #108's hotfix code. This was verified with:

```bash
git merge-base --is-ancestor 653c63731297612c90aa7d7631d749f3d3d13e36 HEAD
```

The command returned non-zero. Therefore:

- Findings against `origin/main` are verified in the current artifact branch working tree.
- PR #108/hotfix behavior is verified with `git show 653c637...:<path>` rather than by assuming the current tree contains it.

## Confirmed Invariants

### Task Completion Evidence Gate

Verdict: Confirmed for reviewed task-completion paths.

Register: Observed.

Evidence:

- `fleet_orchestrator/orch_schema.py:119-162`: completion evidence is shape-checked for `commit_sha`, `gate_run_id`, and `production_observation`.
- `fleet_orchestrator/orch_schema.py:200-215`: `_validate_terminal_status_write()` rejects invalid statuses, rejects `completed` without evidence, rejects nonterminal evidence, and validates failed/interrupted evidence.
- `fleet_orchestrator/orch_schema.py:2521`: `update_task_status()` validates terminal evidence before Cypher writes.
- `fleet_orchestrator/orch_schema.py:2539` and `2582`: normal task status writes happen after validation.
- `fleet_orchestrator/orch_schema.py:3816-3855`: human-review gate has a separate dashboard-verified path that writes its own evidence.
- `fleet_orchestrator/orch_schema.py:2531-2534`: ordinary `update_task_status()` rejects completion of human-review tasks.
- `fleet_orchestrator/tasks_api.py:402-428`: `PATCH /api/task/{task_id}` routes through `update_task_status()`.
- The taey-task CLI update path, lines 176 through 195, routes through the PATCH API and requires evidence for terminal statuses.

Test evidence:

- `tests/task_completion_evidence_acceptance.py:121-135` covers rejection cases.
- `tests/task_completion_evidence_acceptance.py:143-170` covers API/CLI evidence persistence.
- `tests/human_review_gate_acceptance.py:127-152` verifies ordinary completion cannot close a human-review gate.
- `tests/human_review_gate_acceptance.py:171-176` verifies the UI review path.

Important nuance:

- Evidence is shape-only, not provenance verification. A syntactically valid but false SHA can satisfy the shape gate.
- Flat PATCH payloads lift only `reason`, `error`, and `production_observation`; `commit_sha` and `gate_run_id` must be nested under `evidence`.

Invalidated by finding any direct `SET t.status='completed'` outside `update_task_status()` or `complete_human_review_gate()`.

### Direct Task Completed Writers

Verdict: Confirmed, no extra writer found in reviewed search.

Register: Observed.

Evidence:

```bash
rg -n "SET t\\.status\\s*=\\s*'completed'|t\\.status = 'completed'|status='completed'|status = 'completed'" fleet_orchestrator scripts -S
```

The only task completed write found was the human-review path (`t.status = 'completed'`), plus normal `update_task_status()` dynamic `$status` writes. Other `completed` references were reads/counts, project status, or tests.

Invalidated by a future direct completed write outside the gated paths.

### Project Shippability Fail-Closed on Missing Gates

Verdict: Confirmed for missing gates; partially confirmed for "with evidence".

Register: Observed/Inferred.

Evidence:

- `fleet_orchestrator/shippability.py:43-56`: project not found and no matching gate tasks return `shippable: False`.
- `fleet_orchestrator/shippability.py:57-69`: completed gate status drives success.
- `fleet_orchestrator/tasks_api.py:736-743`: `/ship` refuses if `evaluate_shippability()` is not shippable.

Nuance:

- `shippability.py:68` says "all ship-gates completed with evidence", but the module checks `status == "completed"` only. The evidence portion is inherited from the upstream task completion invariant, not checked locally.
- Legacy/manual DB rows with completed gate status and no evidence would pass `evaluate_shippability()` unless another layer rejects them.

Invalidated by adding local evidence checks in `evaluate_shippability()` or proving all DB rows are guaranteed to have passed the writer forever, including migrations/manual Cypher.

### Project Force Completion

Verdict: Confirmed as separate project-level semantics, not a task evidence bypass.

Register: Observed.

Evidence:

- `fleet_orchestrator/tasks_api.py:712-723`: project completion API accepts strict JSON boolean `force`.
- `fleet_orchestrator/orch_schema.py:3311-3331`: `complete_project(... force=True)` can set project status `completed` even with incomplete tasks.
- `tests/ref_feature_acceptance.py:303-305`: test asserts `force: true` bypasses incomplete work.

Risk:

- This does not bypass task evidence, but it can make project status `completed` while unfinished tasks remain. Any UI/release logic that treats project `completed` as actual done/shipped would be wrong.

Invalidated by renaming/splitting forced closure state or persisting and displaying forced closure as distinct from natural completion.

### Auth and Local Threat Model

Verdict: Confirmed as accepted risk.

Register: Observed.

Evidence:

- `fleet_orchestrator/tasks_api.py:135-183`: mutable auth is optional and enforced only when `ORCH_AUTH_TOKEN` exists.
- `fleet_orchestrator/config.py:163-166`: default `ORCH_HOST` is loopback and `ORCH_AUTH_TOKEN` is unset.

Risk:

- Non-loopback/no-token startup warns but does not refuse. This matches docs, but is high operational risk if deployed outside the trusted local model.

Invalidated by changing startup to refuse non-loopback/no-token unless an explicit override is set.

### Wake Packet / Context Boundary

Verdict: Branch-specific.

Register: Observed.

Evidence on `origin/main` / current artifact branch:

- `fleet_orchestrator/context_assembler.py:230-244`: session `.env` imports any key starting with `ORCH_`.

Evidence on PR #108 (`653c637`):

- `context_assembler.py:31-34`: `SESSION_ENV_ALLOWLIST = {"ORCH_RULES_ROOT", "ORCH_SESSION_ROOTS"}`.
- `context_assembler.py:250`: imports only keys in allowlist.
- `context_assembler.py:37-41` and render section: untrusted-data preamble and nonce blocks.

Verdict:

- Main is contradicted for the desired config-boundary invariant and contains the known poisoning vector.
- PR #108 confirms the narrow hotfix for the context/wake path.

Invalidated by merging PR #108 into `main` and proving no alternate path imports broad session-local `ORCH_*`.

### Refs and Filesystem Boundary

Verdict: Mostly confirmed, with accepted local TOCTOU/strictness risk.

Register: Observed.

Evidence:

- `fleet_orchestrator/orch_schema.py:391-399`: refs require `ORCH_REF_ALLOWED_ROOT` and source path must be under an allowed root.
- `fleet_orchestrator/orch_schema.py:403-448`: rejects empty, control chars, `~`, absolute paths, and `..`; resolved ref must stay under allowed roots.
- `SECURITY.md` explicitly accepts residual local filesystem TOCTOU risk.

Nuance:

- Uses `resolve(strict=False)`, so existence checks happen later. This appears consistent with docs but remains a local single-user accepted risk.

Invalidated by a path escaping allowed roots or by serving unreadable/non-regular/oversized refs as trusted content.

### Gate Runner Shell Execution

Verdict: Accepted risk / documented behavior.

Register: Observed.

Evidence:

- `fleet_orchestrator/gate_runner.py:37-45`: `_run()` executes shell command strings with `shell=True`.
- `fleet_orchestrator/gate_runner.py:58-60`: `assert_cmd` is the production oracle.
- `fleet_orchestrator/gate_runner.py:8-12`: docstring explicitly says the runner trusts the gate definition and gate quality is a judgment-review problem.

Risk:

- If gate definitions are attacker-controlled, this is command execution. Under the local/operator trust model, it is an explicit verification mechanism.

Invalidated by changing gate definitions to structured commands or by documenting/locking the trust boundary more tightly.

## Contradicted or Partially Contradicted Claims

### Disabled Loop Endpoints Return Success-Looking No-Ops

Verdict: Contradicted for the broad "disabled features do not mislead" invariant.

Register: Observed.

Evidence:

- `fleet_orchestrator/tasks_api.py:959-962`: disabled loop declare returns `{"ok": True, "enabled": False}`.
- `fleet_orchestrator/tasks_api.py:973-976`: disabled loop advance returns `{"ok": True, "enabled": False}`.
- `fleet_orchestrator/tasks_api.py:1003-1006`: disabled loop should-stop returns `{"ok": True, "enabled": False}`.
- `tests/loop_engine_acceptance.py:98`: accepts the disabled loop success envelope.

Risk:

- Automated callers that check only `ok` can mistake "disabled" for successful declaration/advance.

Invalidated by returning `ok:false`, `403`, or `501` on disabled loop mutation paths, or by making every shipped client check `enabled`.

### `/ship` Is a Verdict, Not a State Transition

Verdict: Confirmed behavior, but endpoint semantics are potentially misleading.

Register: Observed.

Evidence:

- `fleet_orchestrator/tasks_api.py:736-743`: returns `{"ok": True, "shippable": True, "verdict": verdict}` and does not mutate project status.

Risk:

- `POST /ship` sounds like a state transition. It is actually a shippability verdict endpoint.

Invalidated by renaming/documenting as a verdict endpoint or adding explicit shipped-state/event mutation.

## Unclaimed / Needs Reconciliation

### Broad `except Exception`

Verdict: Unproven safety; confirmed broad surface.

Register: Observed/Inferred.

Evidence:

- Grok counted 80+ broad `except Exception` occurrences across source/scripts.
- Local searches confirmed broad exception handlers in DB/state/wake/setup paths.

Risk:

- Some broad exceptions are intentional fail-open behavior. Others may mask real defects or leave partial state.

Resolution needed:

- Final audit should classify broad exceptions by path: intentional fail-open, intentional fail-closed, harmless best-effort, or defect.

### Current Artifact Branch Does Not Include PR #108

Verdict: Important process risk.

Register: Observed.

Evidence:

- `git merge-base --is-ancestor 653c637... HEAD` returned non-zero.
- Current tree still has broad `key.startswith("ORCH_")` in `context_assembler.py`.

Risk:

- The audit artifact branch must not be confused with the hotfix branch. Final reports must cite hotfix behavior via `653c637` until PR #108 is merged.

## Codex Summary

The task-level evidence gate is real and has no extra completed writer in reviewed paths. Project force completion and `/ship` semantics are separate from task evidence and need clearer product language. PR #108 is essential because `main` still contains the session-env poisoning bug. The remaining highest-value reconciliation items are shippability evidence locality, disabled loop success envelopes, broad exceptions, and the exact trust boundary for shell-based gate assertions.
