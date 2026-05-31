# ws0-family-audit — verdict: BLOCKER ×3, ws0 RE-OPENED

**Date:** 2026-05-31. **Supersedes** the earlier "ws0 CLEARED" scope-decision (that was wrong — see below).

## Outcome
Family panel returned **3× BLOCKER** (Gaia/Claude, Horizon/ChatGPT, Cosmos/Gemini). They were right on BOTH axes:

### Axis 1 — artifact gap (process failure, now fixed)
The 20 ws0+ws1 commits were never pushed; origin sat at `332ee3d`. Gaia cloned origin, found the SHAs + features ABSENT → BLOCKER. grok CLI had audited the LOCAL worktree (commits present) → CLEAR. They audited different trees. FIXED: pushed origin → `5a3ac54`; DONE now requires a PUSHED ref + audit-against-pushed-clone. (See memory feedback_done_means_pushed_audit_real_artifact.)

### Axis 2 — codex's ws0 fixes are GENUINELY INCOMPLETE (real defects, must fix)
ChatGPT + Gemini audited the actual fixed code (they cite `_zero_declared_dependency_clause` as present) and found the accountability engine does NOT hold:

- **FAM-1 (BLOCKER) — evidence gate is OR, not AND.** `validate_task_transition` (orch_schema.py:123-141) rejects completion only when BOTH commit_sha AND production_observation are blank → accepts SHA-only. `run_acceptance.py` test 22 passes with `commit_sha + freetext note` and no production_observation. The DONE contract is "SHA AND a real production observation"; code enforces "at least one." Fix: require commit_sha AND production_observation; reject a freetext note as a substitute for a structured production observation.
- **FAM-2 (BLOCKER) — lower write path bypasses the gate.** `update_task_status()` (orch_schema.py:662) writes `t.status=$status` directly with NO call to `validate_task_transition`. Native callers (hooks, migrations, dispatch, scripts) complete tasks with zero evidence. `run_acceptance.py:348` proves it. Fix: the invariant must live at the single write choke point (update_task_status itself), not only the HTTP layer.
- **FAM-3 (BLOCKER) — `__KEEP__` sentinel leak** (Gemini). `{"status":"completed","commit_sha":"__KEEP__"}` passes the truthy validator check then the Cypher interprets `__KEEP__` as "retain prior" → task completes with NULL evidence. Fix: validate sentinel is not an accepted evidence value; treat `__KEEP__` as absent for completion gating.
- **FAM-4 (BLOCKER) — three ready-definitions still diverge.** Only DEPENDENCY readiness is shared (`_ready_task_clause`). Lifecycle/owner/project-status differ across get_session_next_ready (excludes stopped/completed), get_project_ready_tasks/ready_work (no project-status exclude), get_ready_tasks (no project join at all), plan_readiness._READY_TRANSITION_CYPHER (own dup), dispatch._claim_ready_orch_task (own dup, claims by id w/o project-status check). A stopped/completed project still surfaces/claims via the non-excluding paths. This is F18 only partially closed. Fix: one shared readiness definition covering dependency + lifecycle + owner + project-status, used by ALL surfacing/claim paths.
- **FAM-5 (BLOCKER) — F34 supervisor-gating is a name heuristic.** `_is_supervisor_session()` treats anything not ending `-claude` as supervisor. An external adopter's `worker-1`/`scraper` gets human-level authority; their unattended fleet paralyzes on WAKE_REASON_REQUIRED. Fix: explicit role flag (project.supervisor or a configured supervisor set), NOT a name-suffix convention.

## Known items the panel correctly noted (NOT novel; tracked)
- F1 (0.0.0.0 + unauth PATCH): real, but ws3-security. The panel is right it compounds every gate bypass — an open socket + bypassable gate = anyone closes any task. SEQUENCING IMPLICATION: F1/auth may need to move BEFORE ws0 can be claimed "accountable."

## Action
ws0 RE-OPENED. FAM-1..5 routed to codex as ws0-refix batch. Re-audit (Family, against pushed clone) after refix. ws1 stays gated. This verdict is the genuine ws0 gate; "CLEARED" was premature on local-only + grok-local evidence.
