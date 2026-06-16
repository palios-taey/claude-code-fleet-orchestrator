# Codex Surface Map - Full Adversarial Audit - 2026-06-16

Project: `full-adversarial-audit-2026-06-16`

Task: `full-adversarial-audit-2026-06-16::surface-map-codex`

Status: initial Codex surface inventory for the later invariant-audit phase. This is not the final verdict.

## Target Note

Observed: the local audit branch `adversarial-audit-2026-06-16` is based on `origin/main`, so its working tree still shows the pre-PR #108 `_load_session_env` behavior. The frozen audit target includes `origin/context-env-boundary-hotfix` at `653c637`, so env-boundary findings must be marked branch-specific.

## HTTP Surfaces

### Mutable API: `fleet_orchestrator/tasks_api.py`

Observed route families:

- Tasks: `GET /api/tasks`, `GET /api/tasks/ranked`, `GET /api/tasks/{task_id}`, `POST /api/task/create`, `PATCH /api/task/{task_id}`.
- Human review/questions: `POST /api/human-review-gates`, `POST /api/questions/{question_id}/answer`, `POST /api/ui/questions/{question_id}/answer`.
- Projects/plans: `GET /api/projects`, `GET /api/projects/{project_id}`, `POST /api/projects`, `POST /api/projects/load-md`, `POST /api/projects/{project_id}/phases`, `POST /api/projects/{project_id}/complete`, `POST /api/projects/{project_id}/ship`, `POST /api/projects/{project_id}/reset`.
- Stop/priority/conditions: `GET/POST /api/projects/{project_id}/user-stop-conditions`, `POST/DELETE /api/projects/{project_id}/stop-reason`, `PATCH /api/projects/{project_id}`, `POST/PATCH /api/projects/{project_id}/conditions`.
- Sessions: `GET /api/sessions`, `GET /api/sessions/{session_id}/current`, `GET /api/sessions/{session_id}/next-ready`, `GET /api/sessions/{session_id}/projects`, `GET /api/sessions/{session_id}/stop-status`, `GET /api/sessions/{session_id}/stop-decision`, `POST/DELETE /api/sessions/{session_id}/pause`, `POST /api/sessions/{target}/notify`, `GET /api/sessions/{session_id}/wake-packet`.
- Loops: `POST /api/loops/declare`, `POST /api/loops/{loop_id}/advance`, `GET /api/loops/{loop_id}/should-stop`.
- Health: `GET /health`.

Initial risk hooks:

- Auth is optional and enforced only when `ORCH_AUTH_TOKEN` is set; non-loopback/no-token only warns (`tasks_api.py:165-183` in earlier local inspection).
- `POST /api/projects/{project_id}/complete` accepts strict JSON boolean `force` and passes it into project completion.
- Disabled loop endpoints return `{"ok": true, "enabled": false}`.
- Wake-packet endpoint is intentionally fail-open on disabled/assembler error and requires consumers to inspect body fields, not HTTP status.
- `POST /api/sessions/{target}/notify` shells out via subprocess argument list to the notify CLI.

### Public readonly API: `fleet_orchestrator/public_readonly.py`

Observed routes are GET-only:

- `GET /health`
- `GET /api/projects`
- `GET /api/projects/{project_id}`
- `GET /api/sessions/{session_id}/projects`
- `GET /api/sessions/{session_id}/current`
- `GET /api/sessions/{session_id}/next-ready`
- `GET /`, `GET /ui/`, static CSS/JS.

Accepted risk: public IDs/owners/supervisors are visible UI data per Jesse's explicit acceptance in this audit.

### Chat API: `fleet_orchestrator/chat_layer.py`

Observed: chat routes are included only when `ORCH_CHAT_ENABLED` is truthy in `tasks_api.py`. Needs route-specific audit in invariant phase because chat is explicitly described as an injection vector in docs.

## CLI and Script Surfaces

Observed shipped scripts:

- `scripts/install`
- `scripts/orch`
- `scripts/orch-cron`
- `scripts/orch-gate-run`
- `scripts/orch-pre-merge-gate`
- `scripts/orch-public`
- `scripts/orch-watch`
- `scripts/taey-dispatch`
- `scripts/taey-plan`
- `scripts/taey-question`
- `scripts/taey-task`
- `scripts/verify-doc-cli-drift.py`
- `scripts/verify-public-readonly.py`

Initial risk hooks:

- Most `taey-*` scripts call the local API selected by `ORCH_DASHBOARD_URL`.
- `scripts/install` creates a venv, installs the package, may run post-install commands, and calls notify companion scripts.
- `scripts/orch` lifecycle commands call subprocesses for API/service and notify daemon control.
- `scripts/orch-cron` reads trigger JSON/prompt files and launches commands; it needs separate shell/argument handling audit.
- `scripts/orch-pre-merge-gate` shells out to `gh`.
- `scripts/verify-*` are test/verification tools but still exercise subprocess/file/HTTP surfaces.

## Subprocess Surfaces

Observed subprocess call sites include:

- `fleet_orchestrator/gate_runner.py:42`: `subprocess.run(cmd, shell=True, ...)`.
- `fleet_orchestrator/tasks_api.py:929`: notify endpoint calls notify CLI.
- `fleet_orchestrator/dispatch.py:466`: dispatch wake path calls notify CLI.
- `fleet_orchestrator/context_assembler.py:747`: helper subprocess call in context assembly CLI path.
- `fleet_orchestrator/orch_schema.py:453`, `723`, `3669`: helper/notify paths.
- `fleet_orchestrator/loop_engine.py:527`: loop command execution.
- `fleet_orchestrator/easy_setup.py`: docker compose, service start, notify hook installer, and process checks.
- `scripts/install`, `scripts/orch`, `scripts/orch-watch`, `scripts/orch-cron`, `scripts/orch-pre-merge-gate`, `scripts/taey-*`.

Initial risk hooks:

- `gate_runner.py` uses `shell=True`; invariant audit must determine whether the command string is operator-authored plan data, trusted config, or attacker-reachable input.
- Notify subprocesses appear to pass argument lists rather than shell strings, but target/session/message validation must be audited.
- Setup scripts intentionally execute external tools and companion repo scripts; threat model is trusted local operator.

## State Mutation Chokepoints

Observed Neo4j status mutations:

- `fleet_orchestrator/orch_schema.py:update_task_status`: central task terminal/nonterminal writer, evidence gate expected here.
- `fleet_orchestrator/orch_schema.py:complete_human_review_gate`: alternate task completion path for human-review gates.
- `fleet_orchestrator/orch_schema.py:complete_project`: project status completion, with `force`.
- `fleet_orchestrator/orch_schema.py:reset_project`: resets project/phases/tasks to active/pending.
- `fleet_orchestrator/orch_schema.py:set_project_stop_reason` / `clear_project_stop_reason`: stopped/active transitions.
- `fleet_orchestrator/orch_schema.py:check_phase_complete`: phase completion.
- `fleet_orchestrator/dispatch.py`: task claim/reclaim transitions to `in_progress`, rollback to `pending`.
- `fleet_orchestrator/worker_liveness.py`: stale liveness can set tasks back to `pending`.
- `fleet_orchestrator/plan_loader.py`: ingest-held tasks released to `pending` after dependency wiring.

Initial risk hooks:

- Any direct `SET t.status = 'completed'` outside `update_task_status` and `complete_human_review_gate` is a potential evidence bypass.
- Project `completed` is not task `completed`; force semantics must be clearly separated from ship/done claims.
- Shippability checks gate task status; evidence is an upstream invariant unless locally enforced.

## Env and Config Surfaces

Observed major env families:

- Connection/config: `ORCH_REDIS_*`, `ORCH_NEO4J_*`, `ORCH_DOTENV`, `ORCH_DATA_DIR`.
- Auth/network: `ORCH_HOST`, `ORCH_PORT`, `ORCH_AUTH_TOKEN`, `ORCH_API_BASE`, `ORCH_DASHBOARD_URL`.
- Notify/session: `ORCH_NOTIFY_*`, `NOTIFY_KEY_PREFIX`, `ORCH_SESSION_IDS`, `ORCH_DASHBOARD_SESSIONS`.
- Refs/context: `ORCH_REF_ALLOWED_ROOT`, `ORCH_SESSION_ROOTS`, `ORCH_RULES_ROOT`, `ORCH_WAKE_PACKET_ENABLED`.
- Gates/features: `ORCH_SHIP_GATES`, `ORCH_GATE_TEMPLATE_ENABLED`, `ORCH_AWAIT_SIGNAL_GATES`, `ORCH_LOOPS_ENABLED`, `ORCH_CHAT_ENABLED`, `ORCH_DECISION_RECEIPTS_ENABLED`, `ORCH_WORKER_TASK_LIVENESS`.
- Handoff/stop: `CF_HANDOFF_*`, `CF_STOP_INPROGRESS*`.
- Setup/runtime paths: `ORCH_STATE_DIR`, `CLAUDE_SETTINGS_PATH`, `NOTIFY_DAEMON_PIDFILE`, `TAEYS_HANDS_ROOT`.

Initial risk hooks:

- On `origin/main`, `_load_session_env` imports all `ORCH_*` from a session repo `.env` into process env. PR #108 constrains this; final audit must verify the hotfix branch and any alternate context paths.
- `NOTIFY_KEY_PREFIX` affects Redis namespaces across many modules and scripts.
- `CF_*` enforcement is per-session/allowlist-driven by design.
- `ORCH_SESSION_IDS` is a target filter, not a complete auth boundary.

## File IO and Ref Surfaces

Observed file read/write surfaces:

- Ref/context reads: `context_assembler.py`, `orch_schema.py` ref helpers, `rules_tier.py`.
- Rules writes: `rules_tier.py`.
- Chat/accountability writes: `chat_layer.py`, `accountability_ledger.py`.
- Setup state and Claude settings writes: `easy_setup.py`, `scripts/install`.
- Plan ingestion reads markdown via `scripts/taey-plan` and API load-md path.
- Public UI reads static assets in `public_readonly.py`.

Initial risk hooks:

- Ref sandbox must prove source path and ref path both stay under allowed roots.
- Setup writes must preserve atomicity and ownership-tracking claims.
- Chat/rules write paths need input validation and scope review.

## Immediate Backdoor Candidate Queue for Invariant Audit

These are not final findings; they are surfaces requiring proof:

1. `gate_runner.py` shell execution: determine trust boundary of gate command strings.
2. Project `force` completion: determine all consumers of project `status='completed'`.
3. Shippability evidence: determine whether missing `completion_evidence` on completed gate rows can pass.
4. Disabled loops returning `ok: true`: determine docs/clients and whether this is accepted posture.
5. Wake-packet fail-open body semantics: ensure all shipped consumers check `ok`, `enabled`, and `packet`.
6. Session `.env` loading: verify PR #108 allowlist and prove no alternate path imports broad `ORCH_*`.
7. Chat enabled path: route map, role validation, Redis key scope, injection boundaries.
8. All broad `except Exception` in stop/liveness/dispatch paths: classify as fail-open/fail-closed and compare docs.
9. Direct status writers: prove no task-completion bypass outside the two intended gated paths.
10. Setup/uninstall settings writes: prove dry-run and surgical uninstall claims.

## Commands Run

```bash
taey-plan current conductor-codex
taey-plan next conductor-codex
redis-cli -h 127.0.0.1 LRANGE taey:conductor-codex:inbox 0 -1
rg -n "@app\\.(get|post|patch|put|delete)" fleet_orchestrator/tasks_api.py fleet_orchestrator/public_readonly.py fleet_orchestrator/chat_layer.py -S
rg -n "subprocess\\.run|subprocess\\.Popen|shell=True" fleet_orchestrator scripts -S
rg -n "SET .*status|SET [a-z]+\\.status|status = 'completed'|status = 'pending'|status = 'active'|status = 'stopped'|force|enabled.: False|ok.: True" fleet_orchestrator -S
rg -n "os\\.environ\\.get|os\\.environ\\[|os\\.environ\\.setdefault|ORCH_|CF_|NOTIFY_KEY_PREFIX|REDIS_HOST|REDIS_PORT" fleet_orchestrator scripts -S
```
