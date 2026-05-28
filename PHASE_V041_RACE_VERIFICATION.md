[Observed] Verification date: 2026-05-28 UTC.

[Observed] Repo under verification: `claude-code-fleet-orchestrator` with the v1.0.5 race-condition patch in `lib/orch_schema.py`, `lib/tasks_api.py`, `lib/plan_loader.py`, and `lib/dispatch.py`.

[Observed] The live `tasks-api` process was restarted from `/path/to/repo` so `conductor.tasks_api:app` re-imported the updated orchestrator library before the zero-dep verification was run.

## Zero-dep wake proof

[Observed] I prepared an idle synthetic owner session in live Redis:

```bash
redis-cli -h 127.0.0.1 DEL taey:v105zero:inbox taey:v105zero:current_task
redis-cli -h 127.0.0.1 SET taey:v105zero:idle 1
```

[Observed] I then hit the live production endpoint:

```bash
curl -s -X POST http://127.0.0.1:5002/api/task/create \
  -H 'Content-Type: application/json' \
  -d '{"description":"v1.0.5 zero-dep wake verification","owner":"v105zero","from":"conductor-codex","priority":61}'
```

[Observed] Response:

```json
{"ok":true,"task_id":"task-87f2205f","from":"conductor-codex","owner":"v105zero","task_type":"standard"}
```

[Observed] Within the next second, the owner inbox contained exactly one wake:

```json
{"from": "orch-create", "type": "wake", "body": "WAKE: task=task-87f2205f (\"v1.0.5 zero-dep wake verification\") has zero dependencies and is ready now. Pick it up with `taey-plan next` or dispatch a worker.", "timestamp": 1779929114.4995418, "priority": "normal", "msg_id": "12e2e8eabdd1"}
```

[Observed] I replayed the same `create_task()` call for the same task id (`task-87f2205f`) inside the dedup window. `LLEN taey:v105zero:inbox` remained `1`, proving the `taey:orch-wake-fired:<task_id>` dedup key suppressed a second wake.

## Dispatch ready-claim proof

[Observed] I created a fresh production graph:
- project `v105-race-e2d666`
- phase `v105-race-e2d666-main`
- upstream dependency task `v105-race-e2d666-dep`
- downstream task `v105-race-e2d666-down`
- edge `v105-race-e2d666-down -> v105-race-e2d666-dep`

[Observed] First dispatch attempt happened before the upstream dependency was completed. The patched `dispatch()` returned:

```text
ORCH_TASK_NOT_READY task=v105-race-e2d666-down status=pending incomplete_deps=1
```

[Observed] After that failed attempt:
- downstream task status was still `pending`
- `redis-cli -h 127.0.0.1 GET taey:v105dispatch:current_task` returned empty

[Observed] I then completed `v105-race-e2d666-dep` and immediately re-ran `dispatch()` for `v105-race-e2d666-down`.

[Observed] The second dispatch succeeded. Evidence:
- downstream task status became `in_progress`
- `taey:v105dispatch:current_task` became:

```json
{"task_id": "v105-race-e2d666-down", "description": "downstream gated dispatch", "supervisor": "conductor", "started_at": 1779929257.8901956}
```

[Inferred] This proves the dispatch path no longer trusts an earlier readiness snapshot. The same function that writes the worker's live dispatch state now re-checks the OrchTask dependency condition and claims the task only when the latest graph state says it is dispatchable.

[Unknown] This verification did not exercise concurrent dispatches of the same OrchTask from two different supervisors at the same instant. The shipped guard is a single conditional Neo4j write on `status='pending'` plus "all deps completed", which is the intended contention boundary, but I did not stage a dual-supervisor race in this production proof.
