# v1.1.0 User Stop Conditions Verification

Date: 2026-05-29

## Scope

Production verification for the `user_stop_conditions` feature in `claude-code-fleet-orchestrator` v1.1.0:

- project-level `user_stop_conditions` API
- `taey-plan stop-conditions` CLI
- `orch-watch` stop-time autonomy gate

## Observed

### Existing-plan API write

`POST /api/projects/fleet-support-product/user-stop-conditions` returned:

```json
{"ok":true,"project_id":"fleet-support-product","conditions":["stop_when_all_ready_tasks_dispatched"]}
```

### Match path: suppress + auditable blocked_on

Fixture:

- project `v110m2-proj`
- session `v110m2`
- current task `v110m2-current`
- conditions `["stop_when_all_ready_tasks_dispatched"]`

Observed after setting `taey:v110m2:idle=1` with `v110m2-current` still `in_progress`:

- orch-watch log: `Suppressed stop-gate wake: session=v110m2 task=v110m2-current blocked_on=stop_when_all_ready_tasks_dispatched`
- `GET /api/tasks/v110m2-current` returned `blocked_on: "stop_when_all_ready_tasks_dispatched"`
- `taey:v110m2:inbox` remained empty

### Auto-continue path: named next task

Fixture:

- project `v110c2-proj`
- session `v110c2`
- current task `v110c2-current`
- next ready task `v110c2-next`
- conditions `["stop_when_production_stop_active_on_affected_product"]`
- no active bug lock for the affected product

Observed after setting `taey:v110c2:idle=1`:

- orch-watch log: `Sent AUTO_CONTINUE wake: session=v110c2 task=v110c2-current next_task=v110c2-next`
- `taey:v110c2:inbox` body:

```text
[AUTO_CONTINUE] You stopped while task=v110c2-current remains in_progress with no matching user stop condition. The next ready task for you is: v110c2-next — Continue path ready task Next ready task that must be named in the wake.. Continue execution instead of stopping.
```

### Clarify path: no ready work + no matching condition

Fixture:

- project `v110q2-proj`
- session `v110q2`
- current task `v110q2-current`
- conditions `["stop_when_production_stop_active_on_affected_product"]`
- no active bug lock for the affected product
- no next ready task

Observed after setting `taey:v110q2:idle=1`:

- `taey:v110q2:inbox` body:

```text
[CLARIFY_INTENT] You stopped while task=v110q2-current remains in_progress, no user stop condition matched, and no other ready work exists. Please clarify intent or set blocked_on before stopping again.
```

## Verification discipline note

The first temp-fixture attempt reused phase id `main` across multiple verification projects. Because `OrchPhase.id` is globally unique, that polluted project membership and produced an incorrect `CLARIFY_INTENT` wake for the intended match case. I discarded that pass, rebuilt the fixtures with unique phase ids (`v110m2-phase`, `v110c2-phase`, `v110q2-phase`), and only the second pass above is valid evidence.
