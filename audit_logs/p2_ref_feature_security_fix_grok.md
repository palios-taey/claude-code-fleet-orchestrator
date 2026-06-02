# P2-REF-FEATURE SECURITY FIX RE-VALIDATE — a9906db

**Auditor:** grok-cli (LOGOS/6SIGMA peer, conductor-grok)  
**Date:** 2026-06-02  
**Target:** orch-ref-feature @ **a9906db** ("Sandbox plan refs and stream bounded reads")  
**Previous BLOCKER commit:** 71556d7 (path-traversal + symlink escape + late cap)  
**Previous audit:** audit_logs/p2_ref_feature_grok.md (bb2addc)  
**Mandate:** Re-validate the security fix. Confirm previous BLOCKER closed. Attack residual vectors (TOCTOU, root-resolution edges, partial-read leaks, symlink races, etc.). Run py_compile + new security tests. Report CLEARED or remaining issues.  
**Protocol:** This file written **FIRST**, git commit, **THEN** taey-notify. 3-register. Novel only vs KNOWN_FINDINGS + prior p2 audit.

---

## Fix Summary (Observed at a9906db)

**Centralized sandboxed resolver:**
- New public `resolve_ref_path(ref_path, source_path) -> (Optional[Path], Optional[str])` in lib/orch_schema.py:199.
- `_ref_allowed_root(source_path)` computes the plan root from the plan file's parent (or cwd fallback).
- Rejects:
  - Leading `~`
  - Absolute paths
  - Anything that fails `resolved.relative_to(root)` after `.resolve(strict=False)`
- Returns warning string on rejection (no Path).

**Bounded streaming read + early caps (in `_read_ref_context`):**
- `_REF_READ_BYTE_CAP = 1024*1024` (1MB).
- `stat_result.st_size > _REF_READ_BYTE_CAP` check **before** open.
- Uses `with resolved_path.open(...) as handle: for line_no, line in enumerate(itertools.islice(handle, l_end), ...)` — streamed, only reads up to the requested end line.
- Line cap (200) applied during accumulation with `remaining_lines`.

**Deduplication:**
- `lib/plan_loader.py` now imports and delegates to `resolve_ref_path` from orch_schema (single implementation).
- All ref context reads go through the new safe path in `_read_ref_context`.

---

## Test Execution

**py_compile:**
- lib/orch_schema.py, lib/plan_loader.py, lib/tasks_api.py, scripts/taey-plan, tests/ref_feature_acceptance.py → all **PASS** (clean).

**New security tests (`tests/ref_feature_acceptance.py`):**
```
PASS absolute-path-rejected
PASS dotdot-escape-rejected
PASS symlink-escape-rejected
PASS in-root-fresh-read
PASS oversize-file-refused
```
Exit: 0. All adversarial cases from the original dispatch (and more) now rejected. In-root reads remain fresh and correct.

---

## Adversarial Residual Analysis

### 1. Previous BLOCKER vectors (path-traversal, symlink escape, oversize DoS)
**CLEARED.**
- Absolute, `../`, `~`, and symlink escapes that would leave the plan root are rejected at `resolve_ref_path` time via the `relative_to` check after resolve.
- Oversize files (>1MB) are refused at stat time before any read.
- Streaming `islice` ensures we never read more lines than requested (early bounding).

### 2. TOCTOU (stat → open race for size)
**Residual (low severity in declared threat model).**
- Stat for byte cap happens, then open + islice.
- An attacker who can race-replace the target file between stat and open could in theory cause the size check to pass while a larger file is opened.
- However: `islice(handle, l_end)` still limits the number of lines read to the declared range. No unbounded read occurs. Content is only for the task's own ref context.
- In the documented **LOCAL single-user** product model, the attacker would need to be the same user racing their own plan's ref files. Practical impact is limited.

### 3. Root-resolution edge cases (source_path=None or untrusted)
**Residual (edge case, not full escape).**
- When `source_path` is None or falsy: `_ref_allowed_root` falls back to `Path.cwd().resolve()`.
- If the orchestrator process is ever invoked with a cwd outside the plan directory tree, the effective sandbox root becomes the process cwd rather than the plan's directory.
- This is a configuration/operational edge rather than a logic bypass. Plans that declare relative refs assume the source_path is meaningful.

### 4. Symlink TOCTOU after resolve check
**Residual (theoretical, narrow race).**
- The `relative_to` check is performed on the target returned by `.resolve()` at validation time.
- Between the check and the later `open()`, a symlink target could theoretically be changed.
- On the subsequent open, the kernel follows whatever the link points to *at open time*.
- This is a classic TOCTOU on symlink targets. In practice, for a local single-user orchestrator processing plans it controls, the window is tiny and the attacker would need concurrent write access to the filesystem in a way that affects their own task's refs.

### 5. Partial-read leak on error
**CLEARED.**
- The read loop is inside a `try`. Any exception (including mid-islice) jumps to the `except` block, which only sets a warning and appends the entry **without** attaching any `content`.
- No partial content from a failed read is ever returned.

### 6. Other potential issues checked
- No other raw `open()` / `read_text()` paths for ref content outside the new safe functions (grep confirmed only the one inside `_read_ref_context`).
- `itertools` is imported at module level.
- `resolve_ref_path` is the single canonical implementation; plan_loader delegates correctly.
- No evidence of partial content leakage or information disclosure on rejection paths (rejections return only the warning string).

---

## 3-Register + Verdict

**Observed:**
- Exact implementation at a9906db: centralized `resolve_ref_path` with absolute/~ rejection + post-resolve `relative_to` check; 1MB stat cap before open; `islice` streaming bounded by declared l_end; all call sites (including plan_loader) go through the safe path.
- New test file `tests/ref_feature_acceptance.py` exercises exactly the original attack cases + oversize + freshness. All 5 cases PASS.
- py_compile clean on all in-scope files + test.
- Previous dangerous duplicated `_resolve_ref_path` + full `read_text()` before cap is gone.

**Inferred:**
- The original BLOCKER (arbitrary file read via absolute/symlink/../ paths + DoS via late cap) is **closed** by construction.
- The fix is a clear improvement: sandbox at resolution time + early byte cap + streaming line reads.
- Deduplication succeeded.

**Unknown / Residual (documented for completeness):**
- TOCTOU windows between stat and open, and between resolve check and open (symlink target swap). These are classic and hard to eliminate without more expensive primitives (e.g., open + fstat + fd-based relative operations). In the declared LOCAL single-user threat model they are low severity.
- cwd-dependent root when source_path is not supplied.
- These are acceptable residuals for the product shape described in the p2 packet and SECURITY.md; they do not restore the original "read any file on the host" attack class.

**BLOCKER STATUS: CLEARED.**
The specific path-traversal + symlink-escape + oversize-DoS vectors that caused the BLOCKER at 71556d7 are closed. The implementation now rejects the adversarial cases Conductor re-verified (/etc/passwd, ../, symlink, oversize).

No new BLOCKER or high-severity regression introduced by the fix. Residual TOCTOU/cwd edges are noted but do not rise to BLOCKER level for this local single-user orchestrator.

**Novel findings:** None beyond the confirmation that the prior BLOCKER is resolved (consistent with the 5/5-era focus on fail-loud / no silent dangerous operations).

---

## Verify Commands

```bash
cd /home/mira/.dev-worktrees/orch-ref-feature
git show a9906db --stat
git show a9906db -- lib/orch_schema.py | sed -n '193,280p'
python3 tests/ref_feature_acceptance.py
python3 -m py_compile lib/orch_schema.py lib/plan_loader.py tests/ref_feature_acceptance.py
cat audit_logs/p2_ref_feature_security_fix_grok.md
```

**Commit of this log follows this file write.**

*File written as FIRST action per protocol.*
*Strictly VALIDATE / LOGOS. No code changes by this peer.*

**Recommendation:** BLOCKER from 71556d7 is **CLEARED**. The security fix is sound for the declared threat model. The noted residual TOCTOU/cwd edges can be tracked as lower-severity follow-ups if desired. Ready for public merge+tag from the security perspective.