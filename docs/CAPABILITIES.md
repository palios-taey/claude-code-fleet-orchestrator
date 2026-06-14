# Live Capability Ledger

This page is a claims ledger for the current `claude-code-fleet-orchestrator`
main branch. Each row states whether the capability is live by default,
flag-gated, or delegated to `claude-code-fleet-notify`, and gives a direct way
to observe the behavior.

| Capability | Current status | How to observe it |
|---|---|---|
| Tasks | Live by default. `OrchTask` nodes are served by the FastAPI task routes; completed writes require completion evidence. | `taey-task --help`; `GET /api/tasks/ranked`; `PATCH /api/task/{id}` with `{"status":"completed","evidence":...}`. |
| Plans | Live by default. Markdown plans ingest to Neo4j projects, phases, tasks, dependencies, owners, priorities, refs, and user stop conditions. | `taey-plan ingest <plan.md>`; `docs/PLAN_FORMAT.md`; `GET /api/projects/{id}`. |
| Stop engine | Live by default. Session stop decisions are computed from active supervised projects, ready work, `blocked_on`, peer liveness, and user stop conditions. | `GET /api/sessions/{session}/stop-decision`; `fleet_orchestrator/orch_schema.py:get_session_stop_decision`; ship-gate stop acceptance tests. |
| Dispatch | Live by default. Dispatch claims a task, writes Redis `current_task`, sends the worker prompt through `taey-notify`, and rolls back the claim if the wake fails. | `fleet_orchestrator.dispatch.dispatch(...)`; Redis key `${NOTIFY_KEY_PREFIX:-taey}:<worker>:current_task`; `tests/dispatch_wake_atomic_acceptance.py`. |
| Chat | Flag-gated off by default. When `ORCH_CHAT_ENABLED=1`, the API mounts chat routes and stores per-lineage history in Redis. | Set `ORCH_CHAT_ENABLED=1`; dashboard chat bar; `fleet_orchestrator/chat_layer.py`; `tests/decision_receipt_acceptance.py` chat receipt checks. |
| Refs | Live but fail-safe gated by `ORCH_REF_ALLOWED_ROOT`. Plan refs are stored as structured metadata and rendered as file-line pointers when allowed. | Add `[ref: path:Lx-Ly]` to a plan, set `ORCH_REF_ALLOWED_ROOT`, then inspect `GET /api/projects/{id}` ref_context. |
| Wake packet | Flag-gated off by default. `ORCH_WAKE_PACKET_ENABLED=1` exposes a provenance-bound packet endpoint for notify hooks. Empty ref tiers mean no refs were attached or selected for that tier; the assembler renders attached refs when present. | `GET /api/sessions/{session}/wake-packet?cli=claude`; `docs/DYNAMIC_CONTEXT_REFS.md`; `tests/wake_packet_acceptance.py`; packet fields `packet_id` and `provenance_hash`. |
| Decision receipts | Flag-gated off by default. `ORCH_DECISION_RECEIPTS_ENABLED=1` writes content-hashed receipts to Redis stream `orch:streams:decision_receipts`. | `fleet_orchestrator.decision_receipt.RECEIPT_STREAM`; `tests/decision_receipt_acceptance.py`; `redis-cli XREVRANGE orch:streams:decision_receipts + - COUNT 5`. |
| Gate template | Flag-gated off by default. `ORCH_GATE_TEMPLATE_ENABLED=1` applies the scout/code/audit/review/approval gate scaffold during plan ingest; `ORCH_GATE_OWNERS` can map those stages to local sessions. | `fleet_orchestrator/orch_template.py`; `tests/orch_template_acceptance.py`; ingest a plan with the flag on and inspect created gate tasks. |
| Loop engine | Flag-gated off by default. `ORCH_LOOPS_ENABLED=1` enables additive loop declaration/advance API routes backed by `fleet_orchestrator.loop_engine`; no `orch-loop` CLI is shipped on main. | `POST /api/loops/declare`; `POST /api/loops/{loop_id}/advance`; `GET /api/loops/{loop_id}/should-stop`; `tests/loop_engine_acceptance.py`. |
| Notify | Delegated to `claude-code-fleet-notify`. Dispatch honors `ORCH_NOTIFY_CLI` through `OrchConfig().notify_cli_path`; the session notify API and loop wake routing invoke `taey-notify` on `PATH` in current main. | `ORCH_NOTIFY_CLI`; `fleet_orchestrator.dispatch.dispatch(...)`; `POST /api/sessions/{target}/notify`; `fleet_orchestrator.loop_engine.send_loop_wake`; notify repo README. |
| Daemon | Delegated to `claude-code-fleet-notify`. The notify daemon injects Redis inbox pointers into idle tmux sessions; orchestrator `scripts/install` may start that delegated daemon when notify is available. | `scripts/install`; `claude-code-fleet-notify/scripts/start_notify_daemons.sh status`; orchestrator `scripts/install` can wire notify when available. |
| Handoff | Split. Orchestrator stores dispatch/task state; explicit handoff records and passive receipts live in `claude-code-fleet-notify`. | Orchestrator: `fleet_orchestrator.dispatch.record_outcome(...)`; notify: `taey-handoff`, `notifications/handoff.py`. |
| Trace | Split. Orchestrator emits optional decision receipts; notification delivery trace lives in notify Redis stream `taey:notify_trace`. | Orchestrator: `ORCH_DECISION_RECEIPTS_ENABLED=1`; notify: `taey-trace` and `notifications/trace.py`. |

Documentation rule: if a row cannot be observed by the command/file named in
the right column, treat it as a bug in either the docs or the implementation.
