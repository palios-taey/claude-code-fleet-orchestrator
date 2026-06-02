# P2-REF-FEATURE RESIDUAL-3 RE-VALIDATE (FINAL) — 9e35826

**Auditor:** grok-cli (LOGOS/6SIGMA, conductor-grok)  
**Target:** orch-ref-feature @ 9e35826 ("Fail loud when ref sandbox root is undefined")  
**Context:** Follow-up to residual #3 flagged at a9906db (cwd fallback when source_path=None widened sandbox to process cwd).  
**Previous:** BLOCKER at 71556d7 (path-traversal etc.); CLEARED at a9906db (sandbox + early cap + streaming).  
**This change:** Removes the cwd fallback. source_path=None now explicitly fails loud with message "ref has no plan-source root (sandbox undefined)", no content, no silent widening.

**Protocol:** Log written FIRST, then commit, then taey-notify. 3-register. Novel only.

---

## Code Change (Observed)

```python
# _ref_allowed_root
def _ref_allowed_root(source_path: Optional[str]) -> Optional[Path]:
    if not source_path:
        return None
    return Path(source_path).resolve(strict=False).parent

# resolve_ref_path
root = _ref_allowed_root(source_path)
if root is None:
    return None, "ref has no plan-source root (sandbox undefined)"
...  # proceed to relative_to check only if root defined
```

In _read_ref_context:
- resolve_warning path sets warning + appends entry with NO "content".
- Exact message surfaces to the ref_entry and context.

**Deduplication:** plan_loader continues to delegate to the single resolve_ref_path (and supplies source_path when available from load_plan_from_text).

---

## Test Execution at 9e35826

py_compile (orch_schema, plan_loader, ref_feature_acceptance): 0 (clean).

`python3 tests/ref_feature_acceptance.py`:
```
PASS absolute-path-rejected
PASS no-source-root-rejected
PASS dotdot-escape-rejected
PASS symlink-escape-rejected
PASS in-root-fresh-read
PASS oversize-file-refused
```
**6/6 PASS.** The new case explicitly exercises source_path=None → the exact fail-loud warning, no content.

Conductor re-verified: no-source-root rejected + full prior attack suite still rejected + fresh in-root reads.

---

## Confirmation on Requested Points

- **no-fallback shape correct:** Yes. Explicit fail-loud on undefined sandbox root. No silent fallback to cwd (or any widening). Matches the "no silent fallbacks / fail-loud" discipline (F5-F10 lineage, PROC-GROK-VERDICT-LOSS context).
- **no regression to cleared vectors:** No. The test suite still covers (and passes) absolute, ../, symlink-escape, oversize, and in-root correctness/freshness. The sandbox + early byte cap + islice streaming logic is unchanged.
- **Any new issues introduced:** None observed in the resolver path. The change is a pure removal of the fallback + early return with clear error.

**3-register:**
- **Observed:** Exact updated _ref_allowed_root + resolve_ref_path + test case + 6/6 PASS + clean py_compile at 9e35826.
- **Inferred:** The residual #3 (silent cwd widening) is now closed with proper fail-loud shape. No regression on the security boundary established at a9906db.
- **Unknown:** Operational impact of plans that legitimately have no source_path at runtime (they will now surface with the warning and no ref content — expected per the fail-loud intent).

**Novel findings:** None (this is the direct closure of the exact residual I flagged previously; consistent with prior p2 audits).

**BLOCKER on this residual:** None. The shape is correct.

---

## Verify Commands

```bash
cd /home/mira/.dev-worktrees/orch-ref-feature
git show 9e35826 -- lib/orch_schema.py | sed -n '193,220p'
python3 tests/ref_feature_acceptance.py
python3 -m py_compile lib/orch_schema.py lib/plan_loader.py tests/ref_feature_acceptance.py
cat audit_logs/p2_ref_feature_residual3_grok.md
```

*File written FIRST per protocol. This is the final p2 commit before Family gate.*
*Strictly VALIDATE / LOGOS.*
