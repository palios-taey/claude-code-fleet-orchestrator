# Full Adversarial Audit Protocol - 2026-06-16

Project: `full-adversarial-audit-2026-06-16`

Supervisor: `conductor-codex`

Workers:

- `conductor-codex`: supervisor, claims registry, local audit pass, runtime probes, reconciliation.
- `conductor-gemini`: independent claims/surface audit. Do not rely on Codex's registry as complete.
- `conductor-grok`: independent code/backdoor audit. Do not rely on Codex's registry as complete.

## Frozen Target

Primary audit target: post-hotfix code state represented by:

- `origin/main`: `19a45d052ca54a22ef98ea898e244d99884ebfb6`
- `origin/context-env-boundary-hotfix`: `653c63731297612c90aa7d7631d749f3d3d13e36`
- audit artifact branch: `adversarial-audit-2026-06-16` at `5180a67`

Reason: `main` alone still contains the known request-time session `.env` poisoning defect. The full audit should evaluate the intended near-term merge state with PR #108 included, while clearly marking anything that is still only on the hotfix branch.

Local generated artifacts observed but not in tracked scope unless a test claims otherwise:

- `build/`
- `fleet_orchestrator.egg-info/`
- `__pycache__/`
- `.ipynb_checkpoints/` when generated locally

## Truth Register

Every claim and finding must be marked:

- **Observed**: verified directly in code, command output, runtime probe, or committed artifact.
- **Inferred**: likely from evidence but not directly proven.
- **Unknown**: not proven. High-risk Unknowns stay blockers until resolved or accepted by Jesse.

## Verdict Labels

For each claim:

- **Confirmed**: code and tests/probes match the claim.
- **Contradicted**: code or runtime behavior disproves the claim.
- **Unproven**: plausible but not established by code/probe/test.
- **Accepted Risk**: true risk, explicitly accepted as product posture.
- **Out of Scope**: not a shipped claim or not part of the frozen target.

## Severity Rubric

- **Critical**: hidden mutation/backdoor, secret leak, unauthenticated remote mutation outside stated posture, evidence/shipping bypass, or install path destructive behavior.
- **High**: state-integrity bypass, cross-session poisoning, claim that materially misleads operators, or fail-open behavior where docs promise fail-closed.
- **Medium**: sharp API contract mismatch, risky accepted posture, missing local enforcement that depends on upstream invariants, broad exception masking on important paths.
- **Low**: naming/semantics mismatch, confusing success response, docs drift that does not create immediate unsafe behavior.

## Bootstrap Claim Families

The final audit must expand this list into a claim-by-claim matrix with file:line evidence.

1. **Local/single-user threat model**
   - API and infrastructure assume a trusted local operator.
   - Mutable API is tokenless unless `ORCH_AUTH_TOKEN` is set.
   - Non-loopback unauthenticated exposure logs a warning rather than refusing startup.

2. **Install and uninstall safety**
   - Dry-run writes nothing.
   - Installer writes settings atomically.
   - Hook/settings ownership is tracked.
   - Uninstall is surgical by default.
   - Explicit restore is destructive only after preflight.

3. **Configuration and env boundaries**
   - Required env fails loud.
   - Defaults are generic and not operator-specific.
   - Session `.env` cannot poison shared process config after PR #108.
   - Feature toggles match docs and have no hidden off-switch for core gates.

4. **Task lifecycle and evidence gates**
   - Terminal task completion requires evidence.
   - Evidence is shape-only, not provenance proof.
   - Human-review gates have a separate completion path.
   - Direct or alternate writers cannot set completed without evidence.

5. **Project and shippability semantics**
   - No gate tasks means not shippable.
   - Ship gates must be complete before `/ship` returns success.
   - Shippability's evidence claim depends on upstream task evidence unless locally checked.
   - Project `force` completion is distinct from evidence-gated task completion and must not be mistaken for shipped/done evidence.

6. **Stop discipline and liveness**
   - Ready work blocks stop where enforcement applies.
   - Handoff/stop enforcement gaps G1-G3 are accurately documented.
   - Redis/liveness failure modes match fail-open/fail-closed docs.

7. **Wake packet and context safety**
   - Dynamic context is optional and fail-open for wake reliability.
   - Untrusted refs/memory/rules are nonce-wrapped.
   - Session env loading cannot import unsafe `ORCH_*` keys after PR #108.

8. **Refs and filesystem boundary**
   - Refs are disabled until `ORCH_REF_ALLOWED_ROOT` is set.
   - Ref paths cannot escape source root or allowed root.
   - Non-regular/oversized/unreadable refs do not silently become trusted content.

9. **Public readonly surface**
   - Separate app, GET-only routes, docs disabled, pointer-only refs.
   - Public identifiers/owners/supervisors are accepted as visible UI data by Jesse for this audit.

10. **Scripts, CLIs, subprocess, and shell surfaces**
    - CLI help and docs match behavior.
    - Subprocess calls do not introduce shell injection.
    - Disabled features do not return misleading success unless documented/accepted.

11. **CI/release gates**
    - Ship gate workflow name matches actual coverage.
    - R5 audit gate enforces required status contexts as documented, including its known limitation.
    - Version-tag consistency prevents release version drift.

## Mandatory Backdoor Search Terms

Every worker must search and account for:

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

## Worker Report Format

Each worker report must include:

- audited SHA/branch
- commands run
- source files reviewed
- claims confirmed
- claims contradicted
- unproven/unknown claims
- backdoor candidates found and disposition
- severity, exact file:line evidence, and what would invalidate each finding

## Reconciliation Rule

If Codex, Gemini, and Grok disagree, the final report must not vote by majority. The reconciler must resolve with code/runtime evidence, or mark the point Unknown.
