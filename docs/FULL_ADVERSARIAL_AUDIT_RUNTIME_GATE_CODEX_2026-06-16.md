# Codex Runtime Gate Run - Full Adversarial Audit - 2026-06-16

Project: `full-adversarial-audit-2026-06-16`

Task: `full-adversarial-audit-2026-06-16::runtime-gate-run`

Auditor: `conductor-codex`

Status: runtime gate input for reconciliation, not the final audit report.

## Runtime Setup

Observed: the GitHub workflow ports were already occupied locally:

- `6379`: live Redis
- `7687` and `7474`: live Neo4j

To avoid mutating live state, Codex started isolated containers:

- `audit-ship-neo4j`: `neo4j:5`, `bolt://localhost:17687`, browser `localhost:17474`
- `audit-ship-redis`: `redis:7`, `localhost:16379`

The workflow notify stub was recreated under `/tmp/audit-notifystub`. Logs:

- Full first run: `/tmp/full-adversarial-audit-runtime-gate-2026-06-16.log`
- First-run summary: `/tmp/full-adversarial-audit-runtime-gate-2026-06-16.summary`
- Targeted rerun log: `/tmp/full-adversarial-audit-runtime-gate-rerun-2026-06-16.log`
- Targeted rerun summary: `/tmp/full-adversarial-audit-runtime-gate-rerun-2026-06-16.summary`

## First Pass Result

Observed: first pass ran the ship-gate commands from `.github/workflows/ship-gate.yml` against isolated services.

Result: 25 passed, 9 failed.

Initial failures:

- local editable package install step
- env contract acceptance script
- doc CLI drift verifier
- standalone sessions acceptance script
- supervisor-dispatch stop-block acceptance script
- current-work default reconcile acceptance script
- recurring reclaim acceptance script
- human-review gate acceptance script
- human-review stop acceptance script

## Rerun / Classification

### Corrected Runner Namespace

Observed: five failures were caused by Codex's first-pass namespace value, `audit-ship-20260616`, which did not include `test`, `ci`, or `acceptance`. The workflow uses `ship-gate-ci` for these steps.

Rerun with `ORCH_TEST_NAMESPACE=ship-gate-ci` passed:

- `tests/stop_supervisor_dispatch_acceptance.py`
- `tests/current_work_default_reconcile_acceptance.py`
- `tests/recurring_reclaim_acceptance.py`
- `tests/human_review_gate_acceptance.py`
- `tests/human_review_stop_acceptance.py`

Verdict: first-pass failures for these five scripts were runner error, not repo defects.

### Doc Drift

Observed: `scripts/verify-doc-cli-drift.py` initially failed because the new audit artifacts used backtick/path-looking prose that the drift checker interpreted as invalid documented paths or CLI commands.

Codex changed audit-doc wording only. After cleanup:

```bash
python3 scripts/verify-doc-cli-drift.py
```

returned:

```text
documented CLI and repo references match live code
```

Verdict: fixed in audit artifacts. This was a real branch hygiene failure introduced by the audit docs, not product code behavior.

### Local Package Install Step

Observed: `python3 -m pip install -e . httpx redis` failed with Debian/Ubuntu PEP 668 `externally-managed-environment`.

Verdict: local-run blocker. GitHub Actions uses its own Python environment where this command is expected to work. A local exact workflow run should use a venv or `pipx`, but Codex did not change the workflow for this audit.

### Env Contract

Observed: `tests/env_contract_acceptance.py` fails locally:

```text
FAIL minimal generic config defaults dashboard URL -> ... 'dashboard_url': 'http://10.0.0.163:5002'
```

Code evidence:

- `fleet_orchestrator/config.py:161` documents default `ORCH_DASHBOARD_URL` as `http://127.0.0.1:5002`.
- `fleet_orchestrator/config.py:269` implements the same default.
- `fleet_orchestrator/config.py:20-52` auto-loads `.env` from the current working directory or repository root.
- This checkout contains a local `.env` with `ORCH_DASHBOARD_URL=http://10.0.0.163:5002`.

Verdict: local environment contamination from the repo-local `.env`, not the literal code default. Audit relevance: the product auto-loads repo `.env`, so "minimal config" tests are not minimal when a deployment `.env` is present in the checkout.

### Self-Contained Install E2E

Observed: `tests/standalone_sessions_acceptance.py` failed only:

```text
FAIL gate repo default is NOT the baked /home/mira literal
```

Code evidence:

- `fleet_orchestrator/gate_runner.py:29`: `DEFAULT_REPO = os.environ.get("ORCH_GATE_REPO") or str(repo_root())`.
- `fleet_orchestrator/paths.py:38`: `repo_root()` resolves the install/repo root.
- This local checkout is `/home/mira/claude-code-fleet-orchestrator`, which equals the historical banned literal in the acceptance script.

Verdict: local path caveat. The code is not hardcoding the path, but this checkout lives at the same path the test uses as the banned sentinel. In a clean non-Mira checkout this should pass; in this checkout it cannot distinguish dynamic repo root from a baked literal.

## Confirmed Runtime Coverage

Observed passes in the first pass plus corrected reruns confirm these major invariants against isolated Neo4j/Redis:

- local bind boundary
- task identity
- task completion evidence gate
- bare task-id resolver
- active stop resolver
- dispatch wake atomicity
- supervisor-dispatch stop-block
- non-binding peer liveness
- owner preservation
- missing-parent no-fallback behavior
- migration priority
- state integrity
- current work reconciliation
- claim atomicity
- recurring reclaim
- question creation parameterization / no injection
- human-review gate completion path
- version identity
- singleton initialization race
- plan loader multi-dependency behavior
- human-review stop state
- ready definition
- ws2 family amendments
- wake packet behavior
- lane state
- project detail API identity
- forced sub-role gate template
- decision receipts
- loop engine

## Runtime Gate Verdict

Register: Observed.

The authoritative acceptance suite was substantially run against isolated services. After correcting Codex runner mistakes and audit-doc drift, the remaining local failures are:

1. Local package install blocked by PEP 668 in the system Python.
2. Env-contract test polluted by this checkout's local Mira `.env`.
3. Self-contained install test has a local false-positive path caveat because this checkout lives at the historical banned `/home/mira/claude-code-fleet-orchestrator` path.

No runtime probe in this pass found a new direct evidence-gate bypass or hidden task-completion writer. The local `.env` contamination and path-sentinel caveat should be carried into final reconciliation because they explain why a clean GitHub ship-gate result and a local exact run can diverge.
