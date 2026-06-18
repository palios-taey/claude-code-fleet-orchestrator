# Shippability Gate — Definition of Shippable

**STATUS: LIVE & ENFORCED (2026-06-17).** The engine enforcement described under
"Enforcement" is implemented in `fleet_orchestrator/shippability.py` and exposed
through verdict endpoints in `fleet_orchestrator/tasks_api.py`.
This file defines the enforced reference standard and the operator-configurable
gate names. It exists because the failure it prevents was: code marked
"completed" on API-200s + a prose-only external audit, shipped broken.

## The gates are CONFIGURABLE per user — these are the reference standard, not a mandate

WHICH gates a project must pass is **configured by each operator**, not baked in.
Set `ORCH_SHIP_GATES` to a comma-separated list of project-local gate **names**
your standard requires; a task is a gate iff its project-local name (the part after
`<project>::`) EXACTLY equals one of them. The default (`prodtest,audit`) is the
**reference operator's** standard documented below — an example. A different shop
might use `ORCH_SHIP_GATES=ci,review1,review2`.

It is **not optional**: the engine is **fail-closed** — a project whose plan
declares NO matching ship-gate tasks can never receive a successful ship verdict. So any plan
that intends to ship must include its configured gate tasks (the project-template
can auto-inject them; see PLAN_FORMAT). The *exact shape* lives in the plan as
real `### Task:` gate entries, in the author's face.

## Definition (the REFERENCE operator's standard — example to copy or replace)

With the default `ORCH_SHIP_GATES=prodtest,audit`, a change is **SHIPPABLE**
only when **BOTH** gates below have recorded, passing evidence. **There is NO
human-approval step.** The process is the authority. A "clean" judgment by any
agent (including the supervisor) is necessary but never sufficient and never a
substitute for gate evidence. (Replace these with your own gates via
`ORCH_SHIP_GATES`.)

### Gate 1 — Full production test of EVERYTHING (UI + backend), actually executed

Not unit tests. Not API `200`s. Real execution of the real feature on real data.

- **Backend** features: run on real input/workload; output captured and asserted;
  recorded as evidence attached to the feature.
- **UI** features: **AT-SPI browser automation (Taey's Hands) drives the live
  dashboard in a real browser**, as a user would — loads the page, performs the
  action, reads the accessibility tree to assert the rendered result, captures a
  screenshot. Examples:
  - click a ref pointer → assert the content dialog renders with the file lines;
  - type + send in chat → assert the message appears in the thread;
  - toggle the arrow → assert the history actually hides / shows.
  The **machine is the oracle** — not a human's eyes, and not a `curl` of the API.
  (The fleet already has this capability; the UI gate uses it, not a hand-built harness.)
- Evidence: the captured run (assertions + screenshots/output).

### Gate 2 — Full-code Chat audit

- The audit packet given to reviewers MUST contain the **full code from the dev
  branch (inlined)** AND the **main-branch link**. A prose summary or a bare SHA
  is rejected — browser-bound reviewers cannot fetch a SHA or clone a repo.
- Verdicts recorded. Any reviewer **BLOCK or refusal-to-certify HALTS** shippable.

## Enforcement

The orchestrator refuses to return a successful ship verdict unless the configured
gate tasks have evidence records that pass. `POST /api/projects/{id}/ship` is a
verdict endpoint, not a lifecycle mutation: on success it returns `action:"verdict"`
and `shipped:false`, and it does not persist shipped state. Declared per plan,
enforced by the engine. No bypass, no approval override exists to route around it.

## Local pre-merge CONTROL gate

Private repositories cannot rely on GitHub branch protection unless the account
has the required plan. Before merging locally, run the same refusal gate:

```bash
scripts/orch-pre-merge-gate --repo OWNER/REPO --pr <number> --gate-task <audit-or-control-task-id>
```

The helper resolves the PR head SHA, verifies `r5-audit-gate` and
`ship-gate-acceptance` are green on that SHA through `gh api`, then refuses unless
the supplied OrchTask is `completed` with `completion_evidence.commit_sha`
matching the head SHA and either `gate_run_id` or `production_observation`.

Gate-runner command strings (`--clean`, `--boot`, and `--assert`) are trusted
operator-authored local input and are executed through the local shell by design.
They are not sandboxed or safe for untrusted gate definitions. See
[SECURITY.md](../SECURITY.md) for the full gate-runner trust boundary.

## Applies to everything — including this gate itself

This feature (the gate, the harnesses, the enforcement) is itself code in the
orchestrator, so it must pass its own two gates before it is shippable. No
exceptions, no author bypass, no "the gate-builder is trusted."
