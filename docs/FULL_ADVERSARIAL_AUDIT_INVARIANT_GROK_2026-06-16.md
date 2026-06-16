# Grok Invariant Audit - Full Adversarial Audit - 2026-06-16

Project: `full-adversarial-audit-2026-06-16`

Task: `full-adversarial-audit-2026-06-16::invariant-audit-grok`

Auditor: `conductor-grok`

Source: `/tmp/invariant-audit-grok-2026-06-16.md`, delivered by `taey-notify`.

## Method

Grok reported auditing the frozen target pair:

- `origin/main`: `19a45d052ca54a22ef98ea898e244d99884ebfb6`
- PR #108 hotfix: `653c63731297612c90aa7d7631d749f3d3d13e36`

Grok used `git show`, `git diff`, `git grep`, and `python -c` checks against frozen code, continuing from its surface map.

## Auth Invariants

Verdict: confirmed as accepted risk.

Register: Observed.

Severity: High operational risk.

Evidence:

- `fleet_orchestrator/tasks_api.py:135`: `_auth_token()` reads `ORCH_AUTH_TOKEN`.
- `fleet_orchestrator/tasks_api.py:165`: non-loopback/no-token warning path.
- `fleet_orchestrator/tasks_api.py:178`: mutable auth middleware enforces only when token exists.

Finding:

- Mutable API is tokenless by default.
- Non-loopback without token warns but does not refuse startup.
- No hidden auth bypass beyond the documented accepted posture.

Invalidated by unconditional token enforcement or startup refusal on non-loopback without token.

## Force and Hidden State Transitions

Verdict: mostly confirmed, with semantic risk.

Register: Observed/Inferred.

Severity: Medium.

Evidence examples:

- `fleet_orchestrator/orch_schema.py:2521`: terminal write validation before task status write.
- `fleet_orchestrator/orch_schema.py:2539` and `2582`: normal task status writes.
- `fleet_orchestrator/orch_schema.py:3816` and `3843`: human-review completion path.
- `fleet_orchestrator/orch_schema.py:1667`, `1718`, `2014`: forced continuation / force-allow liveness release paths.
- `fleet_orchestrator/dispatch.py:105` and `134`: current-task bind/nonce behavior.
- `fleet_orchestrator/plan_loader.py:579`: missing dependency emits explicit ungated error.

Finding:

- C1 task-completion gate holds in reviewed code.
- No silent task `completed` writer was found outside normal validation and human-review gate paths.
- Force valves exist for liveness and project semantics; they must not be confused with evidence-backed completion.

Invalidated by a direct `SET t.status='completed'` without validation/human-review path, or by silent dependency success on missing target.

## Env / `os.environ` Poisoning

Verdict: critical on `main`, mitigated by hotfix.

Register: Observed.

Severity: Critical on `main`; Medium after hotfix until all alternate paths are proven.

Evidence:

- `19a45d0` `context_assembler.py:230-244`: imports every `ORCH_*` key from session `.env`.
- `653c637` `context_assembler.py:31-34`: `SESSION_ENV_ALLOWLIST = {"ORCH_RULES_ROOT", "ORCH_SESSION_ROOTS"}`.
- `653c637` `context_assembler.py:250`: imports only allowlisted keys.

Finding:

- `main` contains a cross-session config poisoning vector.
- PR #108 mitigates the context/wake path by refusing broad session-local `ORCH_*` imports.
- Other global env/feature flags remain normal operator configuration and require separate classification.

Invalidated by showing that `main` no longer imports broad session-local `ORCH_*`, or by proving every context path uses the hotfix allowlist.

## Subprocess / Shell Hazards

Verdict: documented but risky.

Register: Observed/Inferred.

Severity: Medium-High.

Evidence:

- `fleet_orchestrator/gate_runner.py:42`: `subprocess.run(cmd, shell=True, ...)`.
- `fleet_orchestrator/easy_setup.py:64`: executes `version.py` text for package version.
- Scripts call venv, pip, uvicorn, Docker, notify scripts, and gate commands.

Finding:

- `gate_runner.py` is the primary shell-execution hazard.
- It is documented as executing gate definitions; risk depends on gate definition trust.
- `easy_setup.py` `exec` appears scoped to local repo source.

Invalidated by removing `shell=True`, using list-form subprocesses, or proving `assert_cmd` is strictly trusted/local-only.

## Direct Cypher Writes / DB State

Verdict: core task completion invariant holds in reviewed paths.

Register: Observed.

Severity: High if bypass exists.

Evidence:

- `fleet_orchestrator/orch_schema.py:2521`: validation before normal task status writes.
- `fleet_orchestrator/orch_schema.py:2539` and `2582`: normal status writes.
- `fleet_orchestrator/orch_schema.py:3816`: human-review completion path.
- `fleet_orchestrator/plan_loader.py:579`: loud dependency error.

Finding:

- No task-completion bypass found in reviewed Cypher writes.
- Plan loader relies on schema helpers and reports dependency wiring failure loudly.

Invalidated by finding a direct task-completed write outside validated paths.

## Fail-Open / Disabled Feature Success

Verdict: confirmed documented behavior with high-risk semantics.

Register: Observed.

Severity: High.

Evidence:

- `fleet_orchestrator/tasks_api.py:1017-1038`: wake-packet endpoint fail-open contract.
- Disabled wake packet returns `{"ok": true, "enabled": false}`.
- Assembler errors return HTTP 200 with body `{"ok": false, "enabled": true, "error": ...}`.

Finding:

- Wake-packet fail-open behavior matches docs.
- G3 remains: `ORCH_WAKE_PACKET_ENABLED` gates endpoint context only, not session waking.
- Disabled success envelopes can mislead sloppy clients.

Invalidated by hard 5xx behavior on wake errors, by `ORCH_WAKE_PACKET_ENABLED` gating `send_wake`, or by changing disabled endpoints to explicit error responses.

## Filesystem / Refs / Injection

Verdict: high-risk surface, mitigated by hotfix.

Register: Observed/Inferred.

Severity: High before hotfix; Medium after hotfix until all paths are proven.

Evidence:

- `context_assembler.py` uses `Path.resolve(strict=False)` in several places.
- `_read_text(... errors="replace")` reads context material.
- Hotfix adds untrusted-data preamble and blocks around refs/memory/rules.
- `ORCH_REF_ALLOWED_ROOT` gates refs.

Finding:

- Pre-hotfix refs/context plus broad env loading created high-risk injection/config surface.
- Hotfix substantially improves prompt/data boundary.
- Full path proof remains part of final reconciliation.

Invalidated by proving every context read is allowed-root constrained and every rendered untrusted source is wrapped.

## Broad Exception Paths

Verdict: unclaimed risk.

Register: Observed/Inferred.

Severity: Medium-High.

Evidence:

- Grok counted 80+ `except Exception` occurrences across `fleet_orchestrator/` and scripts.

Finding:

- Broad exception handling can mask failures in DB/state/wake/parser paths.
- This should be tracked as an audit gap even where individual fail-open choices are intentional.

Invalidated by classifying all critical broad exceptions as explicit, documented fail-open/fail-closed behavior with no state corruption.

## State Transition Invariants

Verdict: mostly confirmed.

Register: Observed.

Severity: High if broken.

Evidence:

- Task completion gate: `orch_schema.py:2521`, `2539`, `2582`, `3816`.
- Dependency accumulation after #106: `plan_loader.py:164-172`.
- Current-task bind/nonce: `dispatch.py:105`.

Finding:

- C1 task evidence gate holds.
- Dependency accumulation and loud dependency failures appear to close the earlier under-gating class.
- Forced release/liveness paths remain semantic risks, not hidden task-completion writers in reviewed code.

Invalidated by depends overwrite behavior, completed writes outside gate paths, or current-task/hold bypasses.

## Backdoor Candidate Disposition

- Main env poisoning: Critical, fixed/mitigated by PR #108.
- Fail-open wake/G3: High, mitigated by hotfix prompt/data boundary but still requires consumer discipline.
- Broad exceptions and force releases: Medium.
- Warn-only auth exposure and script surfaces: High operational risk, accepted/documented posture.
- No hidden untrusted `eval`/`exec`, hardcoded credentials, or direct unauthenticated DB backdoor found in reviewed frozen tree.

## Grok Summary Verdict

Most listed invariants hold in code. PR #108 is essential because `main` contains the critical env poisoning vector. Remaining risks are tokenless/warn-only mutable API posture, G1-G3 gaps, fail-open wake behavior, broad exceptions, and script/gate command surfaces. Grok recommends adding unclaimed broad-exception/hotfix/shell surfaces to `AUDIT.md`.
