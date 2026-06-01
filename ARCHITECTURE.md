# Architecture

`claude-code-fleet-orchestrator` is a local orchestration layer for one operator running their own CLI fleet on one machine. It does not host untrusted users, does not expose a multi-tenant control plane, and does not add an auth layer on top of localhost services. Its job is narrower: keep a supervised set of sessions moving through a shared task graph without requiring the operator to manually re-dispatch work after every stop.

## Runtime shape

The package is split into five cooperating surfaces:

| Surface | Code | Role |
| --- | --- | --- |
| Tasks API | `src/fleet_orchestrator/tasks_api.py` | FastAPI service on `ORCH_API_HOST:ORCH_API_PORT` (defaults `127.0.0.1:5002`). Serves the JSON API used by the CLIs and mounts the static `/ui/` browser view. |
| Graph model | `src/fleet_orchestrator/orch_schema.py` | Owns Neo4j reads/writes for `OrchProject`, `OrchPhase`, `OrchTask`, and `OrchQuestion`, including dependency readiness, task transitions, question links, and project-level recompute. |
| CLI/API helpers | `src/fleet_orchestrator/scripts/taey_plan.py`, `src/fleet_orchestrator/scripts/taey_task.py` | Operator-facing command line tools for plan ingest, task status changes, current/next-ready inspection, and stop-condition management. |
| Stop-discipline helpers | `src/fleet_orchestrator/dispatch.py`, `src/fleet_orchestrator/plan_readiness.py` | Dispatch-side Redis wiring, worker outcome recording, and readiness text generation for `orch-watch`. |
| Daemons | `src/fleet_orchestrator/scripts/orch_watch.py`, `src/fleet_orchestrator/scripts/orch_cron.py` | `orch-watch` reacts to Redis/session state changes; `orch-cron` emits recurring wakes from a registry file. |

## Data flow

### 1. Plan and task state

- The durable source of truth is Neo4j.
- `OrchProject -> OrchPhase -> OrchTask` models the work graph.
- `OrchTask -> OrchTask` via `DEPENDS_ON` models readiness constraints.
- `OrchQuestion -> OrchTask` via `CONCERNS_TASK` links open questions back to work.

`update_task_status()` is the canonical task transition path. It validates the transition, writes the task, and recomputes the parent project inside one managed Neo4j transaction so a task write cannot commit without the corresponding project update.

### 2. Session dispatch state

`dispatch.bind_current_task()` and `dispatch.dispatch()` write the worker-side Redis wire that the companion `claude-code-fleet-notify` hooks already understand:

- `taey:<worker>:current_task`
- `taey:<worker>:last_outcome`
- `taey:<worker>:parent`

That wire is operational state, not the authoritative task graph. Task status itself lives in Neo4j; Redis is used for inbox delivery, idle/active state, and stop-hook coordination.

### 3. Stop discipline

The orchestrator depends on the transport/hook package for enforcement at stop time. The runtime contract is:

1. A session is dispatched or self-claims work.
2. The session records outcome with `record_outcome()` before stopping.
3. The Stop hook in `claude-code-fleet-notify` clears or preserves `current_task` based on the outcome.
4. `orch-watch` sees the resulting Redis changes and decides whether to wake a supervisor, continue the same session, or stay quiet because the task is genuinely blocked.

The differentiator is that stop-time behavior is tied back to the graph: a session with ready owned work is not meant to disappear into idle ambiguity.

## Queueing and priority

Ready work is chosen with ascending priority order. Lower numbers win.

The code consistently sorts ready items with:

- project priority ascending
- task priority ascending
- creation time / phase order as tie-breakers

Any document or operator guidance that says “higher priority number runs first” is wrong for this codebase.

## Browser surface

`/ui/` is intentionally read-mostly. It lets the operator inspect sessions, projects, current work, next-ready work, and send a notify message to a selected session. Task mutation still lives in the CLI/API surfaces rather than the browser.

## Trust boundary

This is a localhost-trust design:

- default API bind is `127.0.0.1`
- Redis and Neo4j are expected to be local operator services
- there is no built-in auth layer because the intended deployment is one operator controlling their own box

If you deliberately expose the API or backing stores over a network, you are changing the trust model yourself and must add your own access controls.
