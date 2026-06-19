# AI-Native Surface Coherence Gate Design

Status: design only. This document specifies the gate that should be implemented by the follow-on `anc-implement` task. It does not add the verifier, migrate the registry, or wire CI.

## Problem

`docs/ai_native_surface_audit.md` is currently a hand-maintained ledger. That was enough for the first audit pass, but it has three failure modes:

- Completeness drift: a new agent-facing error or wake surface can be added without a ledger row.
- Stale rows: a removed surface can remain in the ledger and make the audit look complete.
- Sampling gaps: a reviewer can miss rows when reading a long ledger, as happened with the truncated-read miss on the remaining no-next-step surfaces.

The fix is an F9-style coherence gate: enumerate the actual code surfaces by AST, key them by structural identity rather than line number, and require a machine-readable registry row for every current surface.

## Goal

Every agent-facing surface in the scoped source set must be either:

- `teaches`: the emitted message carries an actionable next step.
- `exempt`: the surface legitimately should not teach, with a required rationale.

The gate must fail for:

- a current code surface with no registry row.
- a registry row whose surface no longer exists.
- a duplicate surface id.
- a `teaches` row whose statically readable message has no actionable next step.
- an `exempt` row without a rationale.
- a next-step command that names a non-existent repo CLI.

## Source Set

The first implementation should enumerate these agent-facing surfaces:

| Area | Files | Surface kind |
| --- | --- | --- |
| HTTP API rejections | `fleet_orchestrator/tasks_api.py` | `HTTPException` `detail` payloads. |
| Domain validation errors | `fleet_orchestrator/orch_schema.py` | `return None, <message>` validation returns. |
| Stop and wake decision reasons | `fleet_orchestrator/orch_schema.py` | return payloads from `*_block_reason`, `*_stop_reason`, and explicit stop-decision reason builders. |
| Wake packet Operating affordances | `fleet_orchestrator/context_assembler.py` | lines emitted by `_render_operating_section`. |
| CLI failure messages | `fleet_orchestrator/cli_taey_plan.py`, `fleet_orchestrator/cli_taey_task.py`, and any future `fleet_orchestrator/cli_taey_*.py` module | failure output only, not ordinary success output. |

`docs/ai_native_surface_audit.md` currently includes a few dispatch rows. The implementation should either add explicit dispatch enumeration rules or migrate those rows into a separately named manual section that is not allowed to satisfy the scoped AST completeness invariant. The preferred follow-on is to include dispatch surfaces once there is a precise sink rule for prompt/completion text builders; do not silently let old dispatch rows count as current AST-covered surfaces.

## Structural Surface Id

Each discovered surface gets this identity:

```text
<file>::<enclosing_qualname>::<kind>::<ordinal>
```

- `file`: repo-relative file path.
- `enclosing_qualname`: nearest lexical function/class path, using the same parent-walk approach as `scripts/verify-exception-classification.py`.
- `kind`: one of the enumerated surface kinds, for example `http_exception_detail`, `orch_return_none_error`, `orch_block_reason`, `wake_operating_line`, or `cli_failure_message`.
- `ordinal`: 1-based AST order among surfaces with the same `(file, enclosing_qualname, kind)`.

Line number is only a diagnostic hint:

```text
| Surface Id | File | Function | Kind | Ordinal | Line Hint | Classification | Teaching Evidence | Rationale |
```

The verifier matches on `(File, Function, Kind, Ordinal)`, not on `Line Hint`. A pure line shift must pass. Adding or removing a surface in a same-function group can shift ordinals; that is acceptable because it forces review of that group, just like the F9 exception-handler registry.

## Inclusion Rules

### `tasks_api.py` HTTPException details

Include every AST `Call` whose function is `HTTPException` and whose call appears in `fleet_orchestrator/tasks_api.py`.

- If a `detail=` keyword exists, that expression is the candidate message.
- If `detail` is passed positionally, use the FastAPI constructor position for `detail` if present.
- If no detail can be located, still enumerate the call as `http_exception_detail`; it must be classified `exempt` or fixed.
- If `detail` calls a local helper such as `_project_not_found_detail(...)`, the call site is still the primary surface id. The static extractor may optionally resolve simple local helper returns, but an unresolved helper is not a pass; it requires registry teaching evidence.
- If `detail` is a dict/list literal, recursively inspect string values and keys.

### `orch_schema.py` validation returns

Include every `return` in `fleet_orchestrator/orch_schema.py` whose returned value is a tuple/list with first element literal `None` and a second element present.

- The second element is the candidate message.
- If the second element is a dict/list, recursively inspect it.
- If the second element is a variable, function call, or dynamically built object that cannot be statically read, classify it through the registry rather than passing it silently.

This rule intentionally starts with `return None, <message>` because those are the historical validation-error shape. If later code introduces `return False, <message>` as an agent-facing validation surface, the source set must be extended and existing rows migrated in the same PR.

### Stop and wake reason builders

Include every return payload in `fleet_orchestrator/orch_schema.py` from functions whose name matches:

- `*_block_reason`
- `*_stop_reason`
- `_raw_stop_decision`
- `get_session_stop_decision`
- `get_session_stop_status`

For `_raw_stop_decision` and the stop-status functions, enumerate returned dict/list payloads that contain one of `reason`, `next_action`, `next_step`, `block_reason`, `wake_reason`, or `detail`.

This keeps ordinary internal helpers out of scope while covering the surfaces the Stop hook and supervisor status actually show to agents.

### Wake Operating affordances

In `fleet_orchestrator/context_assembler.py`, include message emissions inside `_render_operating_section`.

The first implementation should include:

- `lines.append(<expr>)`
- `lines.extend([<expr>, ...])`
- local list literals returned or extended into `lines`

Only the `## Operating` section is in scope. Identity, refs, memory, rules, and provenance sections have separate contracts and are not part of this AI-native next-step gate unless future design explicitly adds them.

### CLI failure messages

In `fleet_orchestrator/cli_taey_plan.py`, `fleet_orchestrator/cli_taey_task.py`, and future `fleet_orchestrator/cli_taey_*.py` modules, include failure sinks:

- `print(..., file=sys.stderr)`
- `raise SystemExit(<message>)`
- `parser.error(<message>)`
- local failure helpers that write stderr or exit non-zero
- branches that print an error body and return a non-zero status

Do not include ordinary success output unless the output is a negative or empty state that an agent can mistake for completion, such as `next: none`, `no in-progress work`, `No projects.`, or `No pending tasks.`. Those empty-state messages are in scope as `cli_empty_state_message` because they are operationally equivalent to a failure affordance.

## Teaching Assertion

A surface teaches when the candidate message carries an actionable next step. The assertion is falsifiable and mechanical.

### Static pass conditions

A statically readable message is `teaches` if any of these is true:

1. It names a real repo CLI command.
2. It names a real repo API endpoint.
3. It carries a structured next-step field.

### Real CLI detection

Detect command tokens matching:

```text
\btaey-[a-z0-9-]+\b
```

Then validate the command is real by checking at least one source of truth:

- the `pyproject.toml` console scripts.
- executable scripts under `scripts/`.
- installed repo CLI entrypoints if the verifier runs after editable install.

This catches the exact class of bug where a message teaches a non-existent command. A literal `taey-queue` style command is not enough unless that command exists in one of the repo command sources.

The implementation should seed tests with known commands including `taey-plan`, `taey-task`, `taey-receipts`, and `taey-notify`, but it should not hard-code only those four if the repo already exposes additional valid `taey-*` entrypoints.

### Endpoint detection

Detect endpoint tokens matching:

```text
\b(?:GET|POST|PATCH)\s+/api/[A-Za-z0-9_./{}<>:-]+
```

Then validate the method/path against the FastAPI route table where feasible. Path parameters may use `{id}`, `{task_id}`, or `<task-id>` placeholders; the validator should normalize placeholder names before comparing to route templates.

If a row relies on `DELETE /api/...` or another method, it should fail until the design is explicitly extended. The current requested assertion is GET/POST/PATCH.

### Structured field detection

For dict/list AST literals and JSON-like string payloads, recursively search for any non-empty key:

- `next_step`
- `next_action`
- `enable_with`

If a structured field exists but its value is empty, null, or a generic phrase like `see docs`, the static assertion fails.

### Non-teaching examples

The following are not sufficient:

- A bare enum list with no command, endpoint, or next-step field.
- `see docs`, `ask the operator`, or `try again`.
- A path like `/ui/` without an API endpoint, command, or structured next action.
- A command token that does not exist in the repo.
- A raw exception string with no in-band next step.

## Dynamic Message Handling

The AST cannot always prove teaching:

- f-strings may contain runtime-only variables.
- helper functions may compose a dict across several branches.
- exception text may come from a caught exception.
- CLI wrappers may print server JSON that is only known at runtime.

The rule is: unknown dynamic content must be registry-classified, never silently passed.

For a dynamic surface:

- `Classification=teaches` requires `Teaching Evidence` that names the exact command, endpoint, or structured field expected at runtime.
- `Classification=exempt` requires a rationale.
- If the static extractor can read the whole message and prove it lacks a next step, `Classification=teaches` must fail even if the registry claims it teaches. Registry evidence is only for dynamic or helper-composed surfaces, not for overriding a readable bad message.

## Machine Registry

Use a parseable companion registry rather than overloading the current prose ledger:

```text
docs/ai_native_surface_registry.md
```

`docs/ai_native_surface_audit.md` should link to the registry and remain the human audit narrative. The registry should have one table between marker comments:

```markdown
<!-- ai-native-surfaces:start -->
| File | Function | Kind | Ordinal | Line Hint | Classification | Teaching Evidence | Rationale |
| --- | --- | --- | ---: | ---: | --- | --- | --- |
| fleet_orchestrator/tasks_api.py | session_current | http_exception_detail | 1 | 890 | teaches | GET /api/sessions/{session}/current; taey-plan current | Empty-current API response teaches the current and next-ready probes. |
<!-- ai-native-surfaces:end -->
```

Allowed classifications:

- `teaches`
- `exempt`

Registry rules:

- `actual - registered` fails as missing registry row.
- `registered - actual` fails as stale registry row.
- duplicate `(File, Function, Kind, Ordinal)` fails.
- unknown classification fails.
- empty `Rationale` fails for every row.
- `exempt` requires rationale text that states why teaching would be wrong or impossible.
- `teaches` requires either a static pass or non-empty `Teaching Evidence` for a dynamic surface.
- `Teaching Evidence` must itself contain a real CLI, real endpoint, or structured field name.

## Exemptions

Exemptions are allowed but narrow. Valid examples:

- internal-only fail-open 500s where exposing repair instructions would be misleading.
- health/dependency failures that intentionally avoid claiming a recovery command when the recovery is outside this product.
- diagnostic sidecar failures where the user-facing action is to continue with degraded context and no command exists.

Invalid exemptions:

- ordinary task/project/question validation failures.
- stop decisions.
- wake Operating states.
- CLI empty-state messages.
- any surface that can name a real `taey-*` command or `/api/...` endpoint.

An exempt row must say what makes the surface non-teachable. "Not needed" is not a rationale.

## Edge Cases

### Multiple messages in one function

Use ordinals per `(file, function, kind)`. If a function has three `HTTPException` calls, they are `#1`, `#2`, and `#3`. If one is removed, the registry must be reviewed for the whole group.

### F-strings

The extractor should preserve constant fragments from `ast.JoinedStr`. If a fragment contains a real command, endpoint, or structured key, it can pass. If all actionable content is in runtime variables, the surface is dynamic and must be registry-classified.

### Helper-composed messages

The verifier can resolve simple local helpers later, but the safety rule does not depend on that. A helper-composed message is dynamic unless statically resolved; dynamic rows require teaching evidence.

### Non-Exception surfaces

This gate is not an exception-handler gate. It covers return payloads, rendered wake text, stderr prints, parser errors, and structured HTTP details. The `kind` field distinguishes these surfaces so they do not collide.

### Same text emitted through multiple endpoints

Each emission site gets its own surface id. Shared helper text does not collapse rows, because a helper can be safe in one route and misleading in another route. A later implementation may add helper provenance, but completeness is keyed by emitted surface.

### Blank strings and generic raw passthrough

An empty string, raw `str(exc)`, or raw response body is a surface if it reaches an agent. It must either be fixed to teach or explicitly exempted with rationale. Raw passthrough is not exempt merely because the upstream might include a next step.

### Registry row labels from the old ledger

Human labels such as `OS-03`, `API-14`, or `CLI-06` may remain as optional aliases, but they must not be the registry key. The machine key is structural.

## Implementation Shape

Add:

- `scripts/verify-ai-native-surface-coherence.py`
- `tests/ai_native_surface_coherence_acceptance.py`
- `docs/ai_native_surface_registry.md`
- a ship-gate step after the existing doc/flag and exception-classification gates

The verifier should expose reusable functions:

- `discover_surfaces(root: Path) -> list[Surface]`
- `parse_registry(path: Path) -> list[RegistryEntry]`
- `check(root: Path = ROOT, registry_path: Path | None = None) -> list[str]`

`Surface` should carry:

- `file`
- `function`
- `kind`
- `ordinal`
- `line`
- `source`
- `static_message`
- `static_state`: `teaches`, `does_not_teach`, or `dynamic_unknown`

## Acceptance Tests

The follow-on implementation should prove:

- Current registry passes.
- Adding a blank/comment before a surface does not fail the gate.
- Adding a new `HTTPException` without a registry row fails as missing.
- Removing a registered surface fails as stale.
- Duplicating a registry key fails.
- A statically readable `teaches` row with no command, endpoint, or structured next-step field fails.
- A dynamic f-string without a registry row fails.
- A dynamic f-string with `Classification=teaches` and valid `Teaching Evidence` passes.
- An `exempt` row without rationale fails.
- A fake CLI command fails even if it matches `taey-*`.
- A real endpoint with placeholder spelling differences passes after route normalization.
- A CLI empty-state message like `next: none` is included and cannot disappear from the registry.
- Reverting to line-key matching fails the line-shift fixture.

## Migration Plan

1. Implement the AST enumerator and registry parser with the same line-hint-only matching discipline as `scripts/verify-exception-classification.py`.
2. Generate an initial registry from `origin/main` and map each current `docs/ai_native_surface_audit.md` row to the structural surface id where possible.
3. For any old ledger row that has no AST surface, either add an explicit enumeration rule or move it to a non-gated historical section. Do not let it remain in the machine registry.
4. Run the verifier. Fix static non-teaching surfaces or classify true exemptions with rationale.
5. Wire `python scripts/verify-ai-native-surface-coherence.py` and `python tests/ai_native_surface_coherence_acceptance.py` into ship-gate.
6. Keep `docs/ai_native_surface_audit.md` as the narrative ledger, but treat `docs/ai_native_surface_registry.md` as the source of truth for completeness/no-stale enforcement.

## Non-Goals

- This gate does not prove runtime behavior for every branch. It proves the source-level contract that every scoped surface is known and either teaches or is intentionally exempt.
- This gate does not replace acceptance tests that trigger representative errors.
- This gate does not classify broad exception handlers; F9 remains the source for that.
- This gate does not require every success message to teach. It focuses on rejections, empty states, stop/wake affordances, and failure outputs that affect autonomous operation.
