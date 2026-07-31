# Orchestrator Clean Reconciliation - 2026-07-31

## Observed refs

- Current `origin/main`: `8a73e594be0abb19f8d7693a348c4e93ce7fa758`.
- Running checkout `main` is six commits ahead of `origin/main`:
  - `4e244ec09d53a1c44ffa1e70491c4de7e8ce0a5d` - `fix(kb-context): support negative task tag selectors`
  - `7618565227e7bf6c37438ff85da2832f959c0e5e` - `identity: add an ENABLEMENT role - Taey executes, the seat enables`
  - `dfee50e522db2be61a4422ef09cf42ca37bf0815` - `identity: enablement role carries the 3-ROUND RULE and the governing skills`
  - `9aee5927c05fc2fee0df93378b588de28868f213` - `identity: the unblock path is OUTSOURCE, and "taking too long" is a trigger too`
  - `08a15f92b5a808f9aa5124391dee021d6a3fcc03` - `orchestrator: commit live self-start binding behavior + test + doc`
  - `9ca280f3c4ed36f0e58ce3a618838f81a44d32e9` - merge of `origin/main` into the live-ahead line.
- PR 300, `reconcile/orch-unify-diverged`, points at `9ca280f3c4ed36f0e58ce3a618838f81a44d32e9`; GitHub reports it open, mergeable, and blocked by gate state.

## PR relationship

- PR 300 supersedes PR 296. PR 296 head `1e3bd9f0ee136fcf1f8ff141a5c34720b4a07fbd` has the same stable patch-id as PR 300 commit `4e244ec09d53a1c44ffa1e70491c4de7e8ce0a5d`: `d017c114efd11e79491027a411833d932122fa22`. `git cherry -v origin/pr/300 origin/pr/296` reports it as already applied.
- PR 300 does not supersede PR 259. PR 259 head `a229dce72e9573e7327ff6825aa9cb23b6378473` remains an independent reset-hold fix. `git merge-tree --write-tree origin/pr/300 origin/pr/259` produced clean tree `256ae53bcc61d7040353549f53571877e3236a53`.
- PR 300 does not supersede PR 260. PR 260 head `921006f9c78f0cf99990da30404e40a7ef733f60` remains an independent completion-evidence fix. `git merge-tree --write-tree origin/pr/300 origin/pr/260` reported content conflicts in `README.md`, `docs/CAPABILITIES.md`, `docs/CONFIGURATION.md`, and `fleet_orchestrator/evidence_verification.py`; `tests/completion_evidence_verification_acceptance.py` auto-merged.

## Land-through-gate plan

1. Keep the running checkout unchanged until CONTROL chooses a deployment point.
2. Run the normal independent gates against PR 300 head `9ca280f3c4ed36f0e58ce3a618838f81a44d32e9`, plus full secret scans for current tree and history.
3. If gates pass, land PR 300 first. Close PR 296 as superseded-by-PR-300 rather than merging its duplicate commit.
4. Rebase or merge PR 259 after PR 300; the merge-tree probe found no conflict, but it still needs its required checks on the refreshed head.
5. Rework PR 260 after PR 300; resolve the four documented conflicts, rerun the completion-evidence acceptance suite, and then rerun required checks.
6. Only after conductor CONTROL merges and observes the production service should a systemd install/reload happen. This branch does not restart services.
