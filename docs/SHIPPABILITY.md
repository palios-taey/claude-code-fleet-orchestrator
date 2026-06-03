# Shippability Gate — Definition of Shippable

**STATUS: SPEC / TARGET (2026-06-03).** The engine enforcement described under
"Enforcement" is being built in validated stages. Until a stage lands AND has
itself passed both gates below, this document is the target, not an enforced
reality. Nothing in this file is "done" until it is production-tested + full-code
Chat-audited. (This file exists because the failure it prevents was: code marked
"completed" on API-200s + a prose-only Family audit, shipped broken.)

## The gates are CONFIGURABLE per user — these are the reference standard, not a mandate

WHICH gates a project must pass is **configured by each operator**, not baked in.
Set `ORCH_SHIP_GATES` to a comma-separated list of task-id **suffixes** your
standard requires; the engine enforces exactly those. The default
(`-prodtest,-audit`) is the **reference operator's** standard documented below —
an example. A different shop might use `ORCH_SHIP_GATES=-ci,-review1,-review2`.

It is **not optional**: the engine is **fail-closed** — a project whose plan
declares NO matching ship-gate tasks can never be marked shippable. So any plan
that intends to ship must include its configured gate tasks (the project-template
can auto-inject them; see PLAN_FORMAT). The *exact shape* lives in the plan as
real `### Task:` gate entries, in the author's face.

## Definition (the REFERENCE operator's standard — example to copy or replace)

With the default `ORCH_SHIP_GATES=-prodtest,-audit`, a change is **SHIPPABLE**
only when **BOTH** gates below have recorded, passing evidence. **There is NO
human-approval step.** The process is the authority. A "clean" judgment by any
agent (including the conductor) is necessary but never sufficient and never a
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

- The audit packet given to the Family MUST contain the **full code from the dev
  branch (inlined)** AND the **main-branch link**. A prose summary or a bare SHA
  is rejected — browser-bound reviewers cannot fetch a SHA or clone a repo.
- Verdicts recorded. Any reviewer **BLOCK or refusal-to-certify HALTS** shippable.

## Enforcement (target the engine must implement)

The orchestrator refuses to transition anything to `shippable` unless both gates
have evidence records that pass — the same shape as depends-gating, but for the
ship transition. Declared per plan, enforced by the engine. No bypass, no
approval override exists to route around it.

## Applies to everything — including this gate itself

This feature (the gate, the harnesses, the enforcement) is itself code in the
orchestrator, so it must pass its own two gates before it is shippable. No
exceptions, no author bypass, no "the gate-builder is trusted."
