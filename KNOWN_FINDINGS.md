# Known Findings

## 2026-06-24 — Issue #195: non-done peer outcomes did not wake the supervisor

Status: remediation in progress on `task-25e2a138`.

Observed:
- GitHub issue #195 reports a dispatched peer task reverted to `pending` after a real peer error/interruption path, while the supervisor inbox stayed empty.
- Current `fleet_orchestrator.dispatch.record_outcome()` notifies the supervisor only inside the `outcome == "done"` branch.
- The `error` and `interrupted` branch writes `last_outcome` and reverts the task claim to `pending`, but does not call the supervisor `response_ready` notifier.

Inferred:
- A peer that correctly calls `record_outcome(..., "error" | "interrupted", ...)` can still leave the supervisor asleep until some later mechanism notices the task, because the record-time wake is missing.
- Dispatch-time worker-liveness registration should be guarded by acceptance coverage so a peer that dies before calling `record_outcome()` remains expirable by `WORKER_LIVENESS_EXPIRED`.

Unknown:
- Whether every historical CLI peer process had a working Stop hook at the time of the seven-hour idle incident.
- Whether the original incident's missing heartbeat was from a pre-fix branch, a non-canonical dispatch path, or a runtime registration failure.
