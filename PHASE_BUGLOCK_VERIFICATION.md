# Phase Bug-Lock Verification

Date: 2026-05-27

## Scope

- [Observed] This verification exercised the live `lib.dispatch.dispatch()` path in `claude-code-fleet-orchestrator` against production Redis on Mira.
- [Observed] Target worker for the live check: `conductor-codex`
- [Observed] Target product resolved by the new hook: `the-conductor`
- [Observed] Verification task ids:
  - blocked path: `buglock-block-1779898337`
  - bug-fix bypass path: `buglock-fix-1779898337`

## Condition 1 — Active lock raises before worker-state mutation

- [Observed] With `support:product:the-conductor:bug_lock = "true"` and reason `buglock-block-1779898337 - production verification active lock`, calling `dispatch(worker="conductor-codex", task_id="buglock-block-1779898337", ...)` raised:
  - `BUG_LOCK_ACTIVE for the-conductor: buglock-block-1779898337 - production verification active lock`
- [Observed] Immediately after the blocked call:
  - `taey:conductor-codex:current_task = None`
  - `taey:conductor-codex:last_outcome = None`
  - `taey:conductor-codex:last_clear_was_done = None`
  - `taey:orch-watch-stuck:conductor-codex:buglock-block-1779898337 = None`
- [Inferred] Because `current_task` remained absent and the task-specific orch-watch dedup key was never created, the blocked call did not reach `bind_current_task()` or any subsequent mutation path.

## Condition 2 — No stale `current_task` after blocked dispatch

- [Observed] `taey:conductor-codex:current_task` was `None` before the blocked call and remained `None` after it.
- [Observed] The worker inbox did not contain the blocked task id after the blocked call.

## Condition 3 — No stale `last_clear_was_done` / `last_outcome` after blocked dispatch

- [Observed] `taey:conductor-codex:last_outcome` remained `None`.
- [Observed] `taey:conductor-codex:last_clear_was_done` remained `None`.

## Condition 4 — orch-watch does not emit STUCK or UNBLOCK due to blocked dispatch

- [Observed] `/tmp/orch-watch.log` had `252` lines before the blocked call and `252` lines after a 2-second settle window.
- [Observed] No new orch-watch line mentioned `buglock-block-1779898337`.
- [Observed] No new orch-watch line was appended during the blocked-dispatch settle window.
- [Observed] Historical log lines for older `conductor-codex` stuck tasks still exist in the file from May 26, 2026, but the blocked verification call added no new STUCK or UNBLOCK entry.

## Condition 5 — Bug-fix dispatch proceeds under active lock

- [Observed] With the same active lock still present, calling `dispatch(..., task_id="buglock-fix-1779898337", is_bugfix=True)` returned normally and did not raise.
- [Observed] Immediately after that call, `check_previous_task("conductor-codex")` returned:

```json
{
  "description": "bugfix bypass verification",
  "started_at": 1779898339.3521893,
  "supervisor": "conductor",
  "task_id": "buglock-fix-1779898337"
}
```

- [Observed] The live session received the bug-fix dispatch command through the normal notify path:
  - `bugfix verification dispatch should proceed under active lock`
- [Observed] Cleanup then restored:
  - `taey:conductor-codex:current_task = None`
  - `taey:conductor-codex:last_outcome = None`
  - `support:product:the-conductor:bug_lock = None`

## Cleanup

- [Observed] The temporary bug-lock keys for `the-conductor` were deleted after verification.
- [Observed] The temporary `current_task` written by the bug-fix bypass check was cleared after verification.

## Unknowns

- [Unknown] This verification did not exercise concurrent dispatches from multiple supervisors at the same instant. The Family-required production proof here was the five-condition gate in spec §3.1, and those five conditions were observed directly above.
