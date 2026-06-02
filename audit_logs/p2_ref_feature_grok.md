# P2-REF-FEATURE GROK AUDIT — 71556d7 (LAST GATE BEFORE PUBLIC)

**Auditor:** grok-cli (LOGOS/6SIGMA peer, conductor-grok)  
**Date:** 2026-06-02  
**Target:** orch feat/ref-feature @ 71556d7 ("Add plan refs and project lifecycle APIs") — worktree /home/mira/.dev-worktrees/orch-ref-feature  
**Files in scope (per dispatch):** lib/orch_schema.py, lib/plan_loader.py, lib/tasks_api.py, scripts/taey-plan  
**Mandate (from p2_audit.md + this dispatch):** Adversarial, find-bugs-not-endorse.  
  (1) [ref:] parse+persist; per-NODE ~200-line aggregate cap (not per-ref); graceful on missing/unreadable path (no crash); fresh slice read at begin-time.  
  (2) POST /api/projects/{id}/complete (with force guard) + /reset; redis KEY-PREFIX isolation so /reset never wipes unrelated keys on shared Redis (6379).  
**Conductor live verification noted:** parse+fresh-read+graceful+cap good; complete->completed, reset->pending/active.  
**ATTACK VECTORS (explicit):** ref path-traversal/symlink escape (read outside repo/arbitrary files?); cap-bypass; ref read on huge files (DoS); /reset blast-radius beyond OUR keys; /complete force-guard; any crash path on malformed ref.  
**Run required:** py_compile + any tests present.  
**Protocol:** This file written **FIRST** (before any final verdict/notify), git commit, **THEN** taey-notify conductor. Only novel findings vs KNOWN_FINDINGS.md. 3-register truth. Ruthless — this is a public-facing feature.

**Layout note at 71556d7:** Still uses lib/ (legacy) + some fleet_orchestrator/. No dedicated p2/ref acceptance tests (only stop/handoff ones present).

---

## Evidence — Ref Handling (Critical Attack Surface)

**Core dangerous function (duplicated):**
- lib/orch_schema.py:191
- lib/plan_loader.py:116 (near-identical copy)

```python
def _resolve_ref_path(ref_path: str, source_path: Optional[str]) -> Path:
    candidate = Path(ref_path).expanduser()
    if candidate.is_absolute():
        return candidate                    # <-- ABSOLUTE PATHS ALLOWED
    repo_relative = (Path.cwd() / candidate).resolve()
    if repo_relative.exists():
        return repo_relative
    if source_path:
        source_relative = (Path(source_path).expanduser().resolve().parent / candidate).resolve()
        return source_relative
    return repo_relative
```

Then in `_read_ref_context` (orch_schema.py:221-224):
```python
resolved_path = _resolve_ref_path(path, source_path)
...
lines = resolved_path.read_text(encoding="utf-8").splitlines()   # <-- NO SIZE GUARD, FULL FILE READ
```

**Called at begin-time surfacing** (get_task, taey-plan next paths, ~1220, 1337+, 1398+):
- `record["ref_context"] = _read_ref_context(...)`
- Same for project/phase/task ref contexts when listing ready work.

**plan_loader.py** also parses `[ref: path:1-10]` from markdown meta and passes through (no sanitization).

**py_compile + AST:** All four files passed cleanly (no syntax errors).

---

## Per-Vector Verdict + Attack Results

### 1. [ref:] path-traversal / symlink escape / arbitrary file read
- **BLOCKER**
- Absolute paths are explicitly allowed (`if candidate.is_absolute(): return candidate`).
- `.resolve()` follows symlinks.
- No `is_relative_to(repo_root)`, no prefix check after resolve, no chroot/safe-open.
- A plan containing `[ref: /etc/passwd:1-20]`, `[ref: /root/.ssh/id_rsa:1-50]`, or a symlink inside the "repo" pointing to sensitive files will be read when the task is surfaced.
- `expanduser()` allows `~/...`.
- **Observed:** Direct code path from plan ingestion → persistence → begin-time `_read_ref_context` → unrestricted read_text.
- Graceful handling only triggers on *exception* after the read attempt; the read itself succeeds for any readable file on the host.

### 2. 200-line aggregate cap bypass / DoS via huge files
- **BLOCKER** (related)
- The cap (`remaining_lines`, `_REF_LINE_CAP=200`) is applied *after* `read_text().splitlines()` for each ref.
- A single malicious ref with `line_end` = 10_000_000 on a huge file (or /dev/zero-like) will consume arbitrary memory/CPU before the cap truncates.
- No `stat().st_size` guard, no line-by-line streaming read, no per-ref hard cap before full read.
- Aggregate cap is per-node (good intent), but enforcement is too late.

### 3. Crash path on malformed ref
- **PASS (graceful in observed paths)**
- `_normalize_refs` / parsing is defensive (skips bad entries).
- `_read_ref_context` catches exceptions on read and turns them into warnings (no crash propagated to caller in the ref surfacing paths).
- Malformed JSON in persisted refs appears handled by decode helpers.
- No obvious uncaught exception leading to 500 on well-formed but malicious input in the read path (the graceful-unreadable promise holds for I/O errors).

### 4. /complete force-guard
- **PASS (basic guard present)**
- In `complete_project` (orch_schema.py ~1746): checks for any non-completed task in the project phases.
- If incomplete tasks exist and `force=False`, it raises `ProjectLifecycleError` (turned into 409 by the endpoint).
- `force=True` bypasses (documented "force" behavior).
- Endpoint (`tasks_api.py:422`) passes the flag from request body.
- No obvious bypass of the guard when force=False.

### 5. /reset blast-radius / Redis KEY-PREFIX isolation
- **PASS on observed implementation (scoped, not broad blast)**
- `reset_project` calls `_clear_project_convergence_keys` (1722), which only deletes the per-session stop_block_marker / stop_block_count keys for *participant sessions of this project* (using `_state_key` / NOTIFY_KEY_PREFIX + session_id).
- No evidence of a "delete all keys containing project_id" or global flush.
- Other project state is in Neo4j (status, tasks, etc.), which is correctly scoped to the project_id.
- Handoff records and other NOTIFY-prefixed keys are not touched by this reset path in the code reviewed.
- On shared Redis (6379) this does **not** appear to nuke unrelated sessions' data.
- (Minor note: many places still hard-default to `NOTIFY_KEY_PREFIX` or "taey" if unset — consistent with local single-user model but worth watching.)

### 6. Fresh slice read + persistence + surfacing at begin-time
- **PASS on intended behavior (with the security caveats above)**
- Refs are stored on project/phase/task nodes (JSON in Neo4j).
- `_read_ref_context` is called fresh on every surfacing (get_task, plan next, etc.) — not cached stale content.
- Content is returned in the task/plan responses for the worker to consume at begin-time.
- Conductor live verification matches what the code does.

---

## 3-Register Truth + Novel Findings

**Observed:**
- Exact dangerous `_resolve_ref_path` + unconditional `read_text` in two files (orch_schema.py:191 and plan_loader.py:116).
- Absolute paths allowed, symlinks followed via `.resolve()`.
- Full file read before the 200-line cap is applied.
- py_compile + AST clean on all four files.
- Existing tests (stop/handoff) present but unrelated to p2; no p2-specific tests found.
- `/reset` clearing is limited to project-participant convergence keys (scoped).
- `/complete` has an explicit incomplete-task check unless `force=True`.

**Inferred:**
- The ref feature as implemented has a **real security boundary failure** for a feature that is intended to let plans reference source code. In a shared or even single-user host with sensitive files, any user who can ingest a plan can cause the orchestrator process to read arbitrary host files when work is dispatched.
- The 200-line cap is a DoS mitigation on paper only (enforced after the costly read).
- Lifecycle Redis usage appears disciplined for the reset path (no broad blast radius observed).

**Unknown:**
- Exact runtime behavior of `taey-plan` script when surfacing refs (not fully read; assumed to call the same helpers).
- Whether any production deployment has additional WAF / plan-ingest sanitization (not in scope of the orchestrator product code).
- Full end-to-end Redis key usage of handoff records during reset (only convergence keys were clearly scoped in the reviewed path).

**Novel findings (vs KNOWN_FINDINGS.md):**
- This is a **new high-severity issue** not covered by the existing F-series or ENG items. It is a direct violation of the "no silent fallbacks / fail-loud" spirit in a new public surface (arbitrary file read via untrusted plan data) and a classic path traversal in a feature explicitly designed to read source files.
- Duplication of the unsafe `_resolve_ref_path` between plan_loader.py and orch_schema.py increases maintenance risk.
- No size guard before `read_text` on ref content (DoS vector).

**BLOCKER: YES** (path traversal + symlink escape + late cap enforcement allowing arbitrary file read and potential DoS via huge files in [ref: ]). The feature should not be public with the current `_resolve_ref_path` + read implementation. At minimum it requires:
- Strict repository-root sandboxing (resolve then verify `is_relative_to` the project source root or a configured safe root).
- Streaming / size-limited read before applying the line cap.
- Explicit rejection (or safe no-op) of absolute paths and symlinks that escape the root.
- (Ideally) capability-based or chroot-style reading for the ref feature.

---

## Verify Commands

```bash
cd /home/mira/.dev-worktrees/orch-ref-feature
git show 71556d7 --stat

# The smoking guns
git show 71556d7 -- lib/orch_schema.py | sed -n '191,230p'
git show 71556d7 -- lib/plan_loader.py | sed -n '110,150p'

# py_compile (already passed)
python3 -m py_compile lib/orch_schema.py lib/plan_loader.py lib/tasks_api.py scripts/taey-plan

# Full audit log
cat audit_logs/p2_ref_feature_grok.md
```

**Commit of this log follows this file write.**

*File written as FIRST action per protocol (PROC-GROK-VERDICT-LOSS).*
*Peer: conductor-grok. Strictly VALIDATE / LOGOS (no implementation claims).*
*This is the last gate before public merge+tag for the p2 ref + lifecycle feature.*

**Recommendation:** Do not merge/tag until the path traversal / sandbox issues are fixed. The current implementation allows the orchestrator (running as the fleet user) to be tricked into reading any readable file on the host via a crafted plan.