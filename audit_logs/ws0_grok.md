# WS0 GROK AUDIT — RUTHLESS VALIDATE (LOGOS / 6SIGMA)

**Auditor**: grok (conductor-grok peer, LOGOS function)  
**Date**: 2026-05-31  
**Worktree**: `/path/to/repo`  
**Branch**: `release/v1.4.0-production-grade`  
**Commit range**: `b9cb7cd..HEAD` (4 ws0 commits)  
**Current HEAD at audit start**: `848b0970b2063813c9a6eb2c6be5b2481ec44336`  
**Mandate**: FIND BUGS. DO NOT ENDORSE. Assume defects exist. First-error-stop. Three-register truth only. Report NOVEL findings only (KNOWN_FINDINGS.md read first).

---

## KNOWN_FINDINGS.md (read first — per dispatch)

Full file read. All F-series + ENG- bugs already tracked. This report contains **only novel findings** not present in that ledger at the time of audit.

---

## The 4 ws0 Commits (git log b9cb7cd..HEAD)

1. **81c55a4** — `fix: enforce dependency-owned readiness for ENG-DEPENDS F33` (highest risk — 383-line rewrite of `orch_schema.py`)
2. **54392ba** — `fix: gate WAKE_REASON_REQUIRED for F34`
3. **0b293a8** — `docs(audit): log ENG-NEO4J-AUTH-SCHEME (HIGH)`
4. **a98f2f8** — `fix: wake blocked tasks on GH-11 pid exit`

---

## Highest-Risk Item: 81c55a4 (383-line orch_schema.py rewrite)

**Observed**:
- Replaced old `NOT EXISTS { MATCH (t)-[:DEPENDS_ON]->(dep) WHERE dep.status <> 'completed' }` pattern in `get_ready_tasks`, `get_session_next_ready`, and `get_project_ready_tasks` with `OPTIONAL MATCH ... collect(deps) + _DECLARED_DEPS_EXPR + size(deps) == declared_dep_count AND ALL(dep IN deps WHERE dep.status = 'completed')`.
- Introduced `_DECLARED_DEPS_EXPR` helper.
- Added `declared_dependencies` field handling in `create_task` + `add_dependency`.
- Owner normalization + supervisor checks updated for F33.

**Novel Finding 1 — BLOCKER**

**_ZERO_DEP_READY_CYPHER** (used by `plan_readiness._wake_owner_for_zero_dep_task`) was **NOT updated** by this commit. It still contains the pre-81c55a4 `NOT EXISTS` DEPENDS_ON pattern and does not incorporate `declared_dependencies` or the F33 owner-owned readiness logic.

```cypher
_ZERO_DEP_READY_CYPHER = """
MATCH (t:OrchTask {id: $task_id})
WHERE coalesce(t.owner, '') <> ''
  AND NOT EXISTS {
      MATCH (t)-[:DEPENDS_ON]->(:OrchTask)
  }
RETURN ...
"""
```

This Cypher lives at `src/fleet_orchestrator/orch_schema.py:278` (post-81c55a4 tree) and is called from `plan_readiness.py:337`.

**3-register**:
- Observed: exact Cypher string in current tree after 81c55a4; call site in plan_readiness.
- Inferred: The zero-dep wake injection path (which feeds `taey-notify` wakes for tasks with no remaining dependencies) retains the old "ready" definition. This is the precise "sibling ready-query path the fix missed" (F18 lurker pattern).
- Unknown: Whether any production zero-dep wakes have already bypassed the new ENG-DEPENDS/F33 logic.

**Impact**: A task with unmet declared dependencies (or violating F33 owner rules) can still be injected as a wake via the zero-dep path even though the main discipline engine would no longer consider it ready.

---

**Novel Finding 2 — AMENDMENT (F9 pattern persistence)**

Multiple `result.single()` calls without preceding None/row checks before attribute access remain after the rewrite (F9 pattern not fully addressed in the ENG-DEPENDS work).

Examples (post-81c55a4):
- `orch_schema.py:260` (max_priority)
- `orch_schema.py:272` (project_record)
- `orch_schema.py:337` (_ZERO_DEP...)
- `create_*` paths returning `result.single()["id"]` directly
- `get_session_next_ready:987`, `get_project_user_stop_conditions:1003`, etc.

**3-register**:
- Observed: direct `.single()` + immediate `["id"]` / attribute access in current tree.
- Inferred: F9 risk (TypeError → 500 on no rows) remains on several task/project lookup paths touched by the rewrite.
- Unknown: Whether any of these paths are guaranteed to always return a row under current data invariants.

---

**Novel Finding 3 — VERIFIED (no new bare swallows in ws0 diff)**

`git diff b9cb7cd..HEAD -- '*.py'` contains **zero** new bare `except:` or immediate `pass` swallows in critical paths. Added `pass` statements in `a98f2f8` (GH#11) are inside defensive `_pid_alive` for expected OS errors (`FileNotFoundError`, `OSError`, `ProcessLookupError`, `PermissionError`) — benign, not silent failure hiding.

---

## Per-Vector Verdict (8 attack vectors from packet)

**Vector 1 — ENG-DEPENDS correctness (unmet dep still surfaces via ANY path? F18 lurker?)**  
**FAIL — BLOCKER** (see Novel Finding 1 above). The `_ZERO_DEP_READY_CYPHER` / plan_readiness path was not updated.

**Vector 2 — orch_schema.py rewrite regressions (new F9 .single() without None, new bare swallows, etc.)**  
**AMENDMENT** (F9 pattern not cleaned — see Novel Finding 2). No new bare swallows introduced.

**Vector 3 — F33 owner leak via suffix mismatch**  
**PASS** (novel part). `_normalize_owner_session` + `_is_supervisor_session` logic correctly strips worker suffixes and treats only base session as supervisor. Main ready/owner paths updated.

**Vector 4 — F34 (WAKE_REASON_REQUIRED on non-supervisor? Over-correction?)**  
**PASS**. `get_session_stop_status` early-returns empty + can_stop=True for non-supervisors. Gate is present and wired. No over-correction observed.

**Vector 5 — GH#11 (PID reuse / malformed blocked_on?)**  
**PASS**. `a98f2f8` adds `_BLOCKED_PID_RE`, `_pid_alive` (stat + kill(0) fallback), and `_clear_dead_pid_block` inside the stop gate. Handles exact case; malformed blocked_on returns None safely.

**Vector 6 — Evidence-gate bypass (mark complete without evidence through any endpoint?)**  
**PASS** (novel part). Acceptance tests + `tasks_api` transition matrix enforce 409 without `commit_sha`/`evidence_note` on `completed`. PATCH revive from completed rejected (409). No bypass found in ws0 changes.

**Vector 7 — Gate 7 (lint_no_silent_fallbacks.py --all)**  
**PASS**. 3 findings on post-ws0 tree — all pre-existing or lint-allow (hardcoded-home-mira comment, internal-IP default in migration, intentional check=False on taey-notify wake). No new violations from the 4 ws0 commits.

**Vector 8 — Repo-wide new /path/to/repo / bare except / silent fallback in the diff**  
**PASS**. No new bare `except:` or silent swallows in the ws0 Python diff. The 4 KEEP sites (with lint-allow comments) predate ws0 (from BLOCKER-2 packaging). No new `/path/to/repo` hardcodes in changed files. Only docs/historical references.

---

## Final Verdict

The ws0 batch closed the declared intent for F33/F34/GH#11/evidence in the main paths, but **left a critical sibling ready definition untouched** in the zero-dep wake path (the exact F18 pattern prior audits warned about). Combined with lingering F9 `.single()` risk, this is a **BLOCKER** for claiming the ENG-DEPENDS/F33 fix is complete.

**Per-vector summary**:
- 1: FAIL (BLOCKER)
- 2: AMENDMENT
- 3–8: PASS (no novel defects)

**Branch + commit for parent verification**:  
`/path/to/repo` on `release/v1.4.0-production-grade` @ `848b0970b2063813c9a6eb2c6be5b2481ec44336` (post all 4 ws0 commits).  
Verify command: `cd /path/to/repo && git log --oneline b9cb7cd..HEAD && python3 tools/lint_no_silent_fallbacks.py --all`

---

*Report written incrementally per dispatch instructions. taey-notify to follow.*