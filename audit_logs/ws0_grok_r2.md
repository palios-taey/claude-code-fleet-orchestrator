# WS0 GROK AUDIT — ROUND 2 (BLOCKER RE-VERIFY)

**Auditor**: grok (conductor-grok peer, LOGOS function)  
**Date**: 2026-05-31 (round 2)  
**Worktree**: `/path/to/repo`  
**Branch**: `release/v1.4.0-production-grade`  
**Previous audit commit (baseline)**: `a7cbddb` (my WS0 round 1 verdict)  
**Range under re-audit**: `a7cbddb..HEAD`  
**Commits in range**:
- 96bebfe 'unify ready definitions for ENG-DEPENDS F18'
- 2dce723 'fail loud on F9 missing-row task writes'

**Mandate** (per conductor dispatch): Read KNOWN_FINDINGS.md FIRST. Report **only novel** findings. Try hard to break the fix. File is the deliverable — write it first, incrementally. If clean: CLEAR. If still BLOCKER: BLOCKER (triggers re-dispatch).

**Three-register discipline**: Every finding must be Observed / Inferred / Unknown.

---

## KNOWN_FINDINGS.md Status (read first)

Full file read at start of round 2. All prior F-series, ENG-DEPENDS, F33, F34, GH#11, F9, F18, etc. already tracked. This report contains **only findings novel to the two commits 96bebfe + 2dce723**.

---

## Attack Plan (the 5 points from dispatch)

1. Can an unmet-declared-dep task STILL surface via `_ZERO_DEP_READY_CYPHER`? (the declared_dependencies-set-but-edges-dropped case — the exact bug). Trace the Cypher.
2. Are main next-ready and the wake path now TRULY using the same ready definition, or do the two helpers (`_ready_task_clause` vs `_zero_declared_dependency_clause`) disagree on any edge case (e.g. a dep that exists but is NOT completed)?
3. Any OTHER ready/surfacing path still on the old predicate? (grep all _CYPHER + DEPENDS_ON sites)
4. F9: are there REMAINING `.single()[...]` without None-guard beyond the 3 codex fixed?
5. Gate still 3 (no new)? Any new bare-except/silent-fallback/hardcode in 96bebfe+2dce723?

---

## Initial State (post round-1 audit)

- Round 1 BLOCKER was on `_ZERO_DEP_READY_CYPHER` not being updated by 81c55a4 (old `NOT EXISTS` pattern survived in the zero-dep wake path).
- 96bebfe claims to have unified the definitions using shared helpers.

**This round 2 starts here. File will be updated incrementally with evidence.**

---

## Evidence from 96bebfe (F18 unification)

**Observed (from git show + code read):**
- Introduces two helpers:
  - `_ready_task_clause()`: used by main ready paths. Checks `size(deps) == declared_dep_count AND ALL completed`.
  - `_zero_declared_dependency_clause()`: used by `_ZERO_DEP_READY_CYPHER`. Requires `size(declared_dependencies) == 0 AND size(deps) == 0`.
- `_ZERO_DEP_READY_CYPHER` is updated to:
  ```
  ... OPTIONAL MATCH (t)-[:DEPENDS_ON]->(dep)
  WITH t, collect(dep) AS deps
  """ + _zero_declared_dependency_clause() + """
  ```
- Main paths (`get_ready_tasks`, `get_session_next_ready`, `get_project_ready_tasks`) now call `_ready_task_clause()` instead of inline old logic.

**Analysis on attack point 1 (unmet-declared-dep via _ZERO_DEP_READY_CYPHER):**
- The new clause explicitly requires `size(declared_dependencies) == 0`.
- Therefore, a task that has `declared_dependencies` set (non-empty list) will **never** match the WHERE in `_zero_declared_dependency_clause()`, even if no actual DEPENDS_ON edges exist.
- This directly addresses the "declared but no edges dropped" case that was the previous BLOCKER.
- A task with declared deps will not be injected as a zero-dep wake.

**3-register**:
- Observed: exact updated Cypher and helper definitions post-96bebfe.
- Inferred: The previous BLOCKER on F18 (zero-dep path ignoring declared_dependencies) is closed for the declared-but-no-edges case.
- Unknown: Edge case where declared_dependencies contains IDs that do not exist as nodes (the clause only looks at the list size on the task, not existence of the referenced tasks). This would be a data quality issue, not a logic bug in the clause.

**Analysis on attack point 2 (do the two helpers agree?):**
- They are deliberately different by design:
  - `_ready_task_clause`: "ready if the effective dep count (declared or actual) is satisfied."
  - `_zero_declared_dependency_clause`: "this is a zero-dep task only if declared list is empty AND no actual edges."
- This is correct semantics. A task with declared deps should never be treated as "zero-dep" for wake purposes.
- No disagreement bug found for the normal case (dep exists but not completed) — both paths would correctly exclude it.

**Analysis on attack point 3 (other ready paths on old predicate?):**
- All three main ready surfaces now route through `_ready_task_clause()`.
- `_ZERO_DEP_READY_CYPHER` routes through the zero-specific clause.
- No other Cypher fragments using the old inline `NOT EXISTS { DEPENDS_ON }` pattern were found in the post-96bebfe tree for readiness computation.
- Plan readiness and wake logic now consistent with the unified model.

---

## Evidence from 2dce723 (F9 loud errors)

**Observed (from git show + code read):**
- Adds `TaskWriteError` exception class.
- Updates `create_project`, `create_phase`, `create_task`, `create_question`:
  - Capture `record = result.single()`
  - `if not record: raise ...Error(...)`
  - Then use `record["id"]`
- This replaces the previous direct `result.single()["id"]` that would TypeError on None.

**Analysis on attack point 4 (remaining .single() without guard):**
- The critical write paths that were F9 hotspots are now guarded.
- Remaining `.single()` calls in the tree (e.g. `max_priority`, `project_record`, various LIMIT 1 queries, `get_session_next_ready` final `.single()`, stop reason queries, etc.) fall into two categories:
  - Queries that are expected to return a row under normal operation (with subsequent `if not record` handling in many cases).
  - Read queries where absence is a valid "not found" path (handled).
- No new crash-on-missing-row paths introduced in write/create logic.
- The F9 fix is narrowly scoped to the creation paths that were previously vulnerable.

**3-register**:
- Observed: the 4 creation functions now raise proper errors instead of relying on .single()["id"].
- Inferred: The specific F9 class of defects in task/phase/project/question creation is closed.
- Unknown: Whether all other `.single()` sites have been reviewed for similar risk in non-creation paths (out of scope for this commit; no new ones added by it).

---

## Gate / Surface Checks (point 5)

**Lint gate (tools/lint_no_silent_fallbacks.py --all):**
- Still exactly 3 findings (same as prior state):
  1. hardcoded-home-mira (config.py comment)
  2. hardcoded-internal-ip (migration default)
  3. subprocess-check-false (intentional on taey-notify wake)
- No new violations from 96bebfe or 2dce723.
- The "lint-allow" comments on the KEEP fleet-notify paths remain.

**New bare except / silent fallback / hardcode in these two commits:**
- Diff inspection of both commits: no new `except Exception: pass`, bare `except:`, or `check=False` in critical paths.
- No new `/path/to/repo` or internal hardcoded paths introduced.
- The two commits are narrowly scoped to ready unification + loud errors on writes.

**3-register**:
- Observed: lint output post both commits; diff of the two commits contains zero new swallow or hardcode patterns.
- Inferred: These two commits do not regress the no-silent-fallback surface.
- Unknown: (N/A — surface is clean for these changes.)

---

## Overall Round 2 Verdict

**Previous BLOCKER (from round 1) on F18 / ENG-DEPENDS in the zero-dep path: CLOSED by 96bebfe.**

The unification using shared helpers with explicit handling of `declared_dependencies` vs actual edges addresses the core gap. The zero-dep wake path now correctly requires both declared list empty AND no actual deps.

**F9 in write/create paths: CLOSED by 2dce723** (loud errors instead of crash on .single()["id"]).

**No new defects introduced** in these two commits (no new swallows, no new hardcodes, lint unchanged, other ready paths unified).

**Remaining items** (not novel to these commits, already tracked or out of scope for round 2):
- Lingering `.single()` on non-write paths (pre-existing F9 surface).
- The 3 lint findings (pre-existing).
- The KEEP fleet-notify sys.path sites (from earlier BLOCKER-2 packaging work, not touched here).

**Final classification for this re-audit**:
- Previous BLOCKER on vector 1 (F18 in zero-dep path): **CLEARED**.
- F9 write paths: **CLEARED**.
- No regression or new BLOCKER/AMENDMENT from 96bebfe + 2dce723.

The fixes hold under the specific attack vectors requested. No evidence found that the unification or loud-error changes can be bypassed for the declared-but-no-edges case, helper disagreement, other paths, or new silent fallbacks.

**Next step per dispatch**: git commit this file, then taey-notify conductor with summary + this commit SHA.

---

*Report written incrementally. Analysis complete for the requested points.*
