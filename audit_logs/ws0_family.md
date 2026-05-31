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

## v2 baseline (Gaia clone-confirmed at pinned 5a3ac54 — NON-binding, pre-refix)
Gaia (Claude Opus High) checked out the pinned SHA and INDEPENDENTLY confirmed all 5 FAM at exact file:line — this is the precise refix target list:
- FAM-1: orch_schema.py:137-141 (`not A and not B`; error string says "and/or")
- FAM-2: tasks_api.py:260 only gate; orch_schema.py:662 update_task_status unguarded (orch_watch + acceptance call it direct)
- FAM-3: orch_schema.py:672-675,702,707 (__KEEP__ sentinel passes gate, Cypher discards)
- FAM-4 (SHARPENED): orch_schema.py:313 zero-dep gates `coalesce(status,'pending')` vs `_ready_task_clause` callers :646/:1006/:1174 use LITERAL `status='pending'` → NULL-status tasks surface in one path only. Unify on coalesce everywhere (or normalize status non-null at write).
- FAM-5: _is_supervisor_session:163-172 hardcoded suffix denylist.

**Bonus findings (carry forward):**
- **CREDIT:** transition matrix orch_schema.py:72-78 CORRECTLY blocks completed→revival — F14 *revival* path genuinely closed. The weakness is gate CONTENT, not the matrix. (codex: do NOT touch the matrix.)
- **SHA-GATE-INVERSE-GAP:** the cloned-SHA-echo verification only catches STALE clones, not a NEWER HEAD. Gaia's packet pinned 5a3ac54 but live was f745e9f (one ahead). Re-audit must pin reviewers to the EXACT refix SHA with `==` equality, not "contains/at-least."
- Cosmos (Gemini) procedural BLOCK §6.2 (substrate-isolated, no egress) — correct discipline, no engineering signal. Perplexity DR pending. ChatGPT+Grok failed V1 mode_select — retry post-refix.

## Action
ws0 RE-OPENED. FAM-1..5 routed to codex as ws0-refix (file:lines forwarded). After refix push: binding Family re-audit against the EXACT refix SHA (==). ws1 stays gated. This verdict is the genuine ws0 gate; "CLEARED" was premature on local-only + grok-local evidence.

---

## v3 BINDING ROUND @ c3881e9 (conductor-reconciled against code 2026-05-31)

Pre-audit gate PASS: Gaia cloned + confirmed HEAD == c3881e9 exactly.

- **FAM-1 ✓ CLOSED** — orch_schema.py:138-142, gate now AND (raises if EITHER commit_sha or production_observation empty); error string says "and". (codex packet said :143 — stale by 1-5 lines, logic correct.)
- **FAM-2 ✓ CLOSED** — validate_task_transition called at orch_schema.py:703 INSIDE update_task_status (write choke point), before any SET. Native callers gated.
- **FAM-3 ✓ CLOSED** — _normalize_closeout_value (:145-160) maps __KEEP__/empty → absent before the gate; sentinel cannot satisfy evidence.
- **FAM-4 ⚠️ surfacing CLOSED, claim over-stated** — all SURFACING/claim paths coalesce(t.status,'pending') (:104,325,659,694,1045,1213). BUT "zero literal status=pending" is FALSE: :534/:572 are ON CREATE SET writes (correct as literals), and :947 is a non-coalesced STATS read (Gaia N1 — NULL-status undercount, minor). Surfacing fix holds; N1 → ws2/this-refix.
- **FAM-5 ✗ PARTIAL + codex MISREPORTED** — codex claimed `_is_supervisor_session` + `ORCH_SUPERVISOR_SESSIONS`; NEITHER EXISTS (grep=0). The role-based WAKE gate genuinely exists under a different name (get_session_supervised_projects:1182 + get_session_stop_status:1410, exact coalesce(p.supervisor,'')=session_id) → F34/Q3 security INTENT satisfied. BUT the residual bug stands: _normalize_owner_session (:174) suffix-strips and feeds the supervisor WRITE at :446 (created_by='ops-bot-codex' → supervisor='ops-bot' silently); orch_watch.py:114 SUFFIX_SUPERVISOR_RULES drives suffix-based wake resolution. **This is the BLOCKER.**

**VERDICT: BLOCKER (Gaia, conductor-confirmed). ws0 stays OPEN.** Routed FAM-5 write-path fix + N1 to codex. 3 v3 reviewers (Cosmos/Horizon/Clarity) still in flight — verdicts harvested for novels but binding outcome already determined.

**Reviewer channels:** Gaia/Claude:3 = CLONE (verified HEAD==c3881e9). Logos/Grok:5 = Option-A inline (107KB) = ENDORSE (but inline had correct path so didn't hit the FAM-5 named-fn gap; verified intent not exact-name). Cosmos/Horizon/Clarity = in flight.

**Conductor process bugs caught this round (mine):**
1. Packet GitHub URLs were .../src/orch_schema.py but code is at .../src/fleet_orchestrator/orch_schema.py → Logos 404'd round 1. Fix path in re-run.
2. I cried-wolf FAM-1/2 "skipped" off codex's report wording before my code-check finished (retracted; memory saved). This round I reconciled against code FIRST — Gaia's BLOCKER confirmed real.
3. codex MISREPORTED FAM-5 by non-existent function names — reinforces: verify fixes by what's in the diff, not the worker's description.

---

## FAM-5 refix verified @ f09d22c (conductor, pre-final-audit)
- FAM-5 ✓ orch_schema.py:446 `supervisor_value = supervisor or created_by or "unassigned"` — _normalize_owner_session suffix-strip REMOVED from supervisor write. LIVE: created_by='ops-bot-codex' → supervisor='ops-bot-codex' (preserved, not stripped). orch_watch.py resolve_supervisor: explicit taey:<node>:parent first, suffix fallback now opt-in (_configured_supervisor_suffix_rules). [969af0b + f09d22c]
- N1 ✓ orch_schema.py:947-950 stats now coalesce(t.status,'pending'). 
- Gate CLEAN, origin==local==f09d22c (triple-confirmed incl GitHub ls-remote).
- codex used REAL function names this round (verified against diff, not report).
FINAL binding re-audit dispatched @ f09d22c (corrected clone path src/fleet_orchestrator/). If clean → ws0 genuinely closes.
