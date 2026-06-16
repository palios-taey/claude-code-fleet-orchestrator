# Independent Code/Backdoor Surface Map - conductor-grok

Project: `full-adversarial-audit-2026-06-16`

Task: `full-adversarial-audit-2026-06-16::surface-map-grok`

Auditor: `conductor-grok`

Source: `/tmp/surface-map-grok-2026-06-16-independent.md`, delivered by `taey-notify`.

## Target and Method

Frozen targets:

- `origin/main`: `19a45d052ca54a22ef98ea898e244d99884ebfb6`
- PR #108 hotfix: `653c63731297612c90aa7d7631d749f3d3d13e36`

Grok reported using Observed/Inferred/Unknown, mandatory search terms, branch-specific marking, severity, file:line, commands, and invalidation criteria.

Reviewed files included:

- `fleet_orchestrator/config.py`
- `fleet_orchestrator/tasks_api.py`
- `fleet_orchestrator/orch_schema.py`
- `fleet_orchestrator/dispatch.py`
- `fleet_orchestrator/plan_loader.py`
- `fleet_orchestrator/context_assembler.py`
- `fleet_orchestrator/public_readonly.py`
- `fleet_orchestrator/evidence_contract.py`
- `fleet_orchestrator/easy_setup.py`
- `fleet_orchestrator/gate_runner.py`
- scripts
- workflows
- no tracked standalone audit doc was present in this checkout
- `docs/CONFIGURATION.md`

## Auth Surfaces

Observed:

- `tasks_api.py:135` reads `_auth_token()` from `ORCH_AUTH_TOKEN`.
- `_optional_mutable_auth` enforces only when token is set and method is mutable.
- `_warn_if_mutable_api_exposed` logs only if non-loopback and no token; it does not refuse startup.

Inferred:

- Tokenless default matches C5 and configuration docs.

Severity: High operational risk, as-claimed.

Evidence: `fleet_orchestrator/tasks_api.py:135-183`.

Invalidated by unconditional token requirement or startup refusal on non-loopback without token.

## Force / Hidden State Transitions

Observed:

- `orch_schema.py` contains `forced_continuation_count` and release valves after repeated stop attempts.
- `dispatch.py` handles `current_task` bind/nonce behavior.
- `plan_loader.py` dependency wiring errors loudly if a dependency would be missing and ungated.

Inferred:

- Force valves are mostly liveness release mechanisms, but project force completion remains semantic risk if treated as "done".

Severity: Medium.

Evidence examples: `orch_schema.py:1667`, `orch_schema.py:1718`, `orch_schema.py:2014`, `dispatch.py:105`, `dispatch.py:134`, `plan_loader.py:579`.

Invalidated by a hidden force path that marks task/project done without documented gate semantics.

## Env / `os.environ` / Config Poisoning

Observed on `main`:

- `context_assembler.py:230-244` imports every `ORCH_*` key from session `.env` via `os.environ.setdefault`.

Observed on hotfix:

- `SESSION_ENV_ALLOWLIST = {"ORCH_RULES_ROOT", "ORCH_SESSION_ROOTS"}`.
- Only allowlisted keys are imported.
- Untrusted context is wrapped with a preamble and `<<UNTRUSTED-DATA ...>>` blocks.

Severity: Critical on `main`, Medium after hotfix until alternate paths are proven.

Evidence: `context_assembler.py:230-244` on `19a45d0`; allowlist around `context_assembler.py:31-34` and import check around line 250 on `653c637`.

Invalidated by proof that no broad `ORCH_*` from session `.env` reaches process env on `main`, and by proof that all context/wake paths use the hotfix allowlist.

## Subprocess / Shell / Exec Surfaces

Observed:

- `gate_runner.py:42` uses `subprocess.run(cmd, shell=True, ...)`.
- `easy_setup.py:64` executes `version.py` text to derive package version.
- install/lifecycle scripts call venv, pip, uvicorn, notify scripts, Docker, and related external tools.

Inferred:

- `shell=True` is the main command-injection surface if `cmd` is attacker-controlled.
- `easy_setup.py` `exec` appears local/trusted.

Severity: Medium-High.

Invalidated by converting shell execution to list-form or proving command string is strictly trusted/local-only.

## Direct Cypher Writes / DB State

Observed:

- `orch_schema.py:2539` and `orch_schema.py:2582` set task status through `update_task_status` after `_validate_terminal_status_write`.
- `complete_human_review_gate` provides the separate human-review completion path.
- `plan_loader.py` uses schema helper calls and reports missing dependency wiring as an error rather than silently leaving tasks ungated.

Severity: High if bypass exists.

Invalidated by finding `SET t.status='completed'` outside validation/human-review paths.

## Fail-Open / Disabled Feature Success

Observed:

- Wake-packet endpoint is fail-open by design: errors return HTTP 200 with `ok:false`, disabled returns `ok:true, enabled:false`.
- Consumers must inspect body fields.
- Public readonly behavior is fail-closed.
- G3 remains: wake flag gates the `/wake-packet` endpoint, not `send_wake`.

Severity: High for wake/context if consumers are sloppy.

Evidence: `tasks_api.py:1017-1038`.

Invalidated by hard failure on wake error or by proving all consumers check `ok`, `enabled`, and non-empty packet.

## Filesystem / Refs / Untrusted Injection

Observed:

- `context_assembler.py` and `plan_loader.py` parse refs and read text.
- Hotfix wraps refs/memory/rules with untrusted-data blocks and preamble.
- Ref boundary uses `ORCH_REF_ALLOWED_ROOT`.

Severity: High before hotfix for context injection; Medium after hotfix until all paths are proven.

Invalidated by proving all refs/memory/rules are wrapped and no broad env/context path remains.

## Broad Exception Paths

Observed:

- 83+ `except Exception` occurrences across `fleet_orchestrator/` and scripts.

Inferred:

- These can mask errors or leave partial state in stop/liveness/dispatch/setup paths.

Severity: Medium-High.

Invalidated by classifying all critical broad exceptions as intentional fail-open/fail-closed with logging and no state corruption.

## Scripts and Hidden Surfaces

Observed:

- `taey-task` mutates through API and evidence.
- `orch-cron` uses recurring state JSON/files.
- `gate-run` uses clean room and shell assert command.
- No hardcoded passwords/secrets found post de-umbilical cleanup.

Severity: Medium.

## Backdoor Candidate Disposition

- Main session env poisoning: Critical on `main`, mitigated by hotfix.
- Fail-open wake/G3: High, mitigated by hotfix preamble but still requires consumer discipline.
- Broad exceptions and gate shell execution: Medium.
- Force valves and project force: Medium semantic risk.
- No hidden eval, secret exfiltration, or obvious credential backdoor found in the frozen tree.
- Tokenless mutable API with warning-only exposure is as-claimed but high operational risk.

## Summary Verdict

Grok verdict: surfaces mapped per protocol. PR #108 closes the primary env poisoning backdoor. Remaining high/medium risks are either documented posture or unclaimed risks that should be reflected in `AUDIT.md`: broad exceptions, wake fail-open semantics, tokenless warn-only exposure, force semantics, scripts, and shell-based gate commands.
