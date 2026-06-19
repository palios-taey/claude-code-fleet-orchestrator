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
- `needs-fix`: known baseline debt that is intentionally carried into the gate so the registry can merge before the follow-on remediation pass.
- `exempt`: the surface legitimately should not teach, with a required rationale.

The gate must fail for:

- a current code surface with no registry row.
- a registry row whose surface no longer exists.
- a duplicate surface id.
- a `teaches` row whose statically readable message has no actionable next step.
- a `needs-fix` row added after the gate baseline rather than carried from the reviewed baseline debt list.
- an `exempt` row without a rationale.
- a next-step command that names a non-existent repo CLI.
- a next-step endpoint that names a non-existent repo API route.
- a future API module with agent-facing error sinks that is neither included nor explicitly excluded.

## Source Set

The first implementation should enumerate these agent-facing surfaces by sink reachability, not by a hand-picked file list:

| Area | Files | Surface kind |
| --- | --- | --- |
| API rejection sinks | every fleet orchestrator API module classified as in scope | `HTTPException` `detail` payloads and error `JSONResponse` payloads/statuses. |
| API module guard | every fleet orchestrator module with `HTTPException`, `JSONResponse` status >= 400, `FastAPI`, `APIRouter`, or route decorators | module must be classified `in-scope` or `excluded` with rationale; unclassified modules fail. |
| Domain validation errors | `fleet_orchestrator/orch_schema.py` | `return None, <message>` validation returns and `raise <Exception>(message)` surfaces. |
| Stop and wake decision reasons | `fleet_orchestrator/orch_schema.py` | return payloads from `*_block_reason`, `*_stop_reason`, and explicit stop-decision reason builders. |
| Wake packet Operating affordances | `fleet_orchestrator/context_assembler.py` | lines emitted by `_render_operating_section`. |
| CLI failure messages | `fleet_orchestrator/cli_taey_plan.py`, `fleet_orchestrator/cli_taey_task.py`, and any future fleet orchestrator CLI module whose name starts with `cli_taey_` | failure output only, not ordinary success output. |

The current API module classification starts with these decisions:

| Module | Decision | Rationale |
| --- | --- | --- |
| `fleet_orchestrator/tasks_api.py` | in scope | Primary mutable Tasks API used by agents and CLIs. |
| `fleet_orchestrator/chat_layer.py` | in scope | Chat routes are mounted under `/api/chat`; `_http_error` currently passes through `str(exc)`, which is an agent-facing raw exception surface. |
| `fleet_orchestrator/public_readonly.py` | in scope | Public read-only API still emits HTTP/JSON error responses that an agent or browser automation session can hit; the 404/503 surfaces must teach or be explicitly exempted row by row. |

No API module may be silently uncovered. A future router file that imports or calls `HTTPException`, returns `JSONResponse` with status >= 400, creates `FastAPI`/`APIRouter`, or defines route decorators must either be scanned for surfaces or added to an explicit module-exclusion registry with rationale.

`docs/ai_native_surface_audit.md` currently includes a few dispatch rows. The implementation should either add explicit dispatch enumeration rules or migrate those rows into a separately named manual section that is not allowed to satisfy the scoped AST completeness invariant. The preferred follow-on is to include dispatch surfaces once there is a precise sink rule for prompt/completion text builders; do not silently let old dispatch rows count as current AST-covered surfaces.

## Structural Surface Id

Each discovered surface gets this identity:

```text
<file>::<enclosing_qualname>::<kind>::<ordinal>
```

- `file`: repo-relative file path.
- `enclosing_qualname`: nearest lexical function/class path, using the same parent-walk approach as `scripts/verify-exception-classification.py`; module-level surfaces use `<module>`.
- `kind`: one of the enumerated surface kinds, for example `http_exception_detail`, `orch_return_none_error`, `orch_block_reason`, `wake_operating_line`, or `cli_failure_message`.
- `ordinal`: 1-based AST order among surfaces with the same `(file, enclosing_qualname, kind)`.

Line number is only a diagnostic hint:

```text
| Surface Id | File | Function | Kind | Ordinal | Line Hint | Fingerprint | Classification | Teaching Evidence | Rationale | Review |
```

The verifier matches on `(File, Function, Kind, Ordinal)`, not on `Line Hint`. A pure line shift must pass. Adding or removing a surface in a same-function group can shift ordinals; that is acceptable because it forces review of that group, just like the F9 exception-handler registry.

`Fingerprint` is a non-identity guard against dynamic-row misattribution. It is a stable hash of the normalized AST expression plus readable literal fragments, helper callee names, and sink kind, excluding line/column numbers. If two dynamic surfaces in the same function are reordered, the id set may remain the same but the fingerprint changes; the gate must fail until the registry is updated and the row is re-reviewed.

## Inclusion Rules

### API rejection sinks

First classify API sink modules. Scan every Python file under `fleet_orchestrator/` and flag a module if it has any of:

- `HTTPException` import or call.
- `JSONResponse` call with a literal or statically readable `status_code` >= 400.
- `FastAPI(` or `APIRouter(`.
- route decorators such as `@app.get`, `@app.post`, `@router.get`, or `@router.post`.

Every flagged module must be listed as in scope or explicitly excluded with rationale. The current implementation should include `fleet_orchestrator/tasks_api.py`, `fleet_orchestrator/chat_layer.py`, and `fleet_orchestrator/public_readonly.py`.

For in-scope modules, include every AST `Call` whose function is `HTTPException`.

- If a `detail=` keyword exists, that expression is the candidate message.
- If `detail` is passed positionally, use the FastAPI constructor position for `detail` if present.
- If no detail can be located, still enumerate the call as `http_exception_detail`; it must be classified `exempt` or fixed.
- If `detail` calls a local helper such as `_project_not_found_detail(...)`, the call site is still the primary surface id. The static extractor may optionally resolve simple local helper returns, but an unresolved helper is not a pass; it requires registry teaching evidence.
- If `detail` is a dict/list literal, recursively inspect string values and keys.

Also include in-scope `JSONResponse` calls whose status is >= 400 or whose payload has `error`, `detail`, `reason`, `next_step`, `next_action`, or `enable_with` keys. This covers public read-only 503-style responses that are not raised as `HTTPException`.

### `orch_schema.py` validation returns

Include every `return` in `fleet_orchestrator/orch_schema.py` whose returned value is a tuple/list with first element literal `None` and a second element present.

- The second element is the candidate message.
- If the second element is a dict/list, recursively inspect it.
- If the second element is a variable, function call, or dynamically built object that cannot be statically read, classify it through the registry rather than passing it silently.

Also include every `raise` statement in `fleet_orchestrator/orch_schema.py` whose exception expression carries a message or can reach API/CLI callers as a validation error. Current covered examples include `PauseValidationError`, `CompletionEvidenceError`, `ConditionValidationError`, `ReadyWorkConflictError`, `PriorityAuditError`, `TaskParentNotFoundError`, `TaskIdCollisionError`, `ProjectNotFoundError`, and `ValueError` raises that are part of public task/project/chat/pause/question operations.

- The first positional argument or formatted message expression is the candidate message.
- If the raise has no message, enumerate it anyway; it must be classified `exempt` or fixed.
- A new `CompletionEvidenceError("bad")` or `PauseValidationError("bad")` without the next-step constants must fail the teaching assertion.
- If a raised message is caught and wrapped by an API route, keep the `orch_schema` raise site and the API wrap site as separate surfaces; each emission site has its own row.

This rule intentionally starts with `return None, <message>` plus `raise ...(<message>)` because those are the current validation-error shapes. If later code introduces `return False, <message>` as an agent-facing validation surface, the source set must be extended and existing rows migrated in the same PR.

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

In `fleet_orchestrator/cli_taey_plan.py`, `fleet_orchestrator/cli_taey_task.py`, and future fleet orchestrator CLI modules whose names start with `cli_taey_`, include failure sinks:

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

- the `setup.py` console scripts.
- executable scripts under `scripts/`.
- installed repo CLI entrypoints if the verifier runs after editable install.

This catches the exact class of bug where a message teaches a non-existent command. A literal command-shaped token is not enough unless that command exists in one of the repo command sources.

The implementation should seed tests with known commands including `taey-plan`, `taey-task`, `taey-receipts`, and `taey-notify`, but it should not hard-code only those four if the repo already exposes additional valid `taey-*` entrypoints.

### Endpoint detection

Detect endpoint tokens matching:

```text
\b(?:GET|POST|PATCH)\s+/api/[A-Za-z0-9_./{}<>:-]+
```

Then strictly validate the method/path against the FastAPI route table. Path parameters may use `{id}`, `{task_id}`, or `<task-id>` placeholders; the validator should normalize placeholder names before comparing to route templates.

If the route table cannot be imported, the gate must fail closed rather than regex-pass a fake endpoint. If a row relies on `DELETE /api/...` or another method, it should fail until the design is explicitly extended. The current requested assertion is GET/POST/PATCH. A token such as `POST /api/does-not-exist` must fail.

### Structured field detection

For dict/list AST literals and JSON-like string payloads, recursively search for any non-empty key:

- `next_step`
- `next_action`
- `enable_with`

If a structured field exists but its value is empty, null, or a generic phrase like `see docs`, the static assertion fails. If the field value contains a command token or endpoint token, the same strict CLI/endpoint existence validators apply to that token. A `next_step` value containing an invalid command-shaped token must fail; the acceptance fixture should include this literal in a non-invocation context:

```text
# taey-fake structured next_step must fail
```

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
- Dynamic rows must include the current `Fingerprint`. A changed fingerprint fails even when the structural id still exists, forcing re-review of helper-composed or runtime-only messages after reorder or rewrite.

## Machine Registry

Use a parseable companion registry rather than overloading the current prose ledger:

The proposed companion is a future registry Markdown file under the docs directory.

`docs/ai_native_surface_audit.md` should link to the registry and remain the human audit narrative. The registry should have one table between marker comments:

```markdown
<!-- ai-native-surfaces:start -->
| File | Function | Kind | Ordinal | Line Hint | Fingerprint | Classification | Teaching Evidence | Rationale | Review |
| --- | --- | --- | ---: | ---: | --- | --- | --- | --- | --- |
| fleet_orchestrator/tasks_api.py | session_current | http_exception_detail | 1 | 890 | abc123 | teaches | GET /api/sessions/{session}/current; taey-plan current | Empty-current API response teaches the current and next-ready probes. | PR review |
<!-- ai-native-surfaces:end -->
```

Allowed classifications:

- `teaches`
- `needs-fix`
- `exempt`

Registry rules:

- `actual - registered` fails as missing registry row.
- `registered - actual` fails as stale registry row.
- duplicate `(File, Function, Kind, Ordinal)` fails.
- matching id with mismatched `Fingerprint` fails.
- unknown classification fails.
- empty `Rationale` fails for every row.
- `exempt` requires rationale text that states why teaching would be wrong or impossible.
- `exempt` requires non-empty `Review` naming the PR, audit, or reviewer that accepted the exemption; generated exemptions may not auto-green the initial registry.
- `needs-fix` requires rationale text, non-empty `Review`, and a baseline-debt marker accepted during the gate introduction. It is not a teaching pass; the verifier should print the count of `needs-fix` rows. A future PR may not add a new `needs-fix` row to make a new non-teaching surface green.
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

An exempt row must say what makes the surface non-teachable. "Not needed" is not a rationale. The initial migration must not auto-exempt rows to reach green; every initial exemption needs audit review recorded in the `Review` column. Existing non-teaching debt should be marked `needs-fix`, not `exempt`, unless the surface truly should never teach.

## Edge Cases

### Multiple messages in one function

Use ordinals per `(file, function, kind)`. If a function has three `HTTPException` calls, they are `#1`, `#2`, and `#3`. If one is removed, the registry must be reviewed for the whole group.

### Dynamic ordinal reorder

Ordinals alone can silently swap rationale between two dynamic rows if the same number of surfaces remains in the same function. The `Fingerprint` column closes that gap. It is not part of the structural id, so line shifts still pass, but a source-expression rewrite or same-group reorder changes the fingerprint and fails until the registry row is re-derived.

### F-strings

The extractor should preserve constant fragments from `ast.JoinedStr`. If a fragment contains a real command, endpoint, or structured key, it can pass. If all actionable content is in runtime variables, the surface is dynamic and must be registry-classified.

### Helper-composed messages

The verifier can resolve simple local helpers later, but the safety rule does not depend on that. A helper-composed message is dynamic unless statically resolved; dynamic rows require teaching evidence.

### Non-Exception surfaces

This gate is not an exception-handler gate. It covers return payloads, rendered wake text, stderr prints, parser errors, and structured HTTP details. The `kind` field distinguishes these surfaces so they do not collide.

### Module-level qualname

When a surface is emitted at module scope, use `<module>` as its function name. Current module-level next-step constants in `fleet_orchestrator/orch_schema.py` are not standalone surfaces; the raise or return site that emits the constant is the surface. If a future module-level raise or error response is added, its qualname is `<module>`.

### Same text emitted through multiple endpoints

Each emission site gets its own surface id. Shared helper text does not collapse rows, because a helper can be safe in one route and misleading in another route. A later implementation may add helper provenance, but completeness is keyed by emitted surface.

### Blank strings and generic raw passthrough

An empty string, raw `str(exc)`, or raw response body is a surface if it reaches an agent. It must either be fixed to teach or explicitly exempted with rationale. Raw passthrough is not exempt merely because the upstream might include a next step.

### Registry row labels from the old ledger

Human labels such as `OS-03`, `API-14`, or `CLI-06` may remain as optional aliases, but they must not be the registry key. The machine key is structural.

## Implementation Shape

Add future artifacts:

- a verifier script named verify-ai-native-surface-coherence.py under the repo's `scripts/` directory.
- an acceptance test named ai_native_surface_coherence_acceptance.py under the repo's `tests/` directory.
- the companion registry file described in the Machine Registry section.
- a ship-gate step after the existing doc/flag and exception-classification gates.

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
- A fake endpoint such as `POST /api/does-not-exist` fails even though it matches the endpoint regex.
- A structured `next_step` value containing the deliberately invalid command token shown in the structured-field section fails.
- A newly added non-teaching surface cannot be made green by adding a new `needs-fix` row outside the reviewed baseline debt set.
- A real endpoint with placeholder spelling differences passes after route normalization.
- A new `orch_schema` raise with no next-step evidence fails.
- A new API router module with an error sink but no module classification fails.
- A CLI empty-state message like `next: none` is included and cannot disappear from the registry.
- Reverting to line-key matching fails the line-shift fixture.

## Migration Plan

1. Implement the AST enumerator and registry parser with the same line-hint-only matching discipline as `scripts/verify-exception-classification.py`.
2. Generate an initial registry from `origin/main` and map each current `docs/ai_native_surface_audit.md` row to the structural surface id where possible.
3. For any old ledger row that has no AST surface, either add an explicit enumeration rule or move it to a non-gated historical section. Do not let it remain in the machine registry.
4. Run the verifier. Mark static non-teaching surfaces as `teaches` only when the static assertion passes; otherwise classify them honestly as baseline `needs-fix` rows with rationale and review. The verifier may pass reviewed baseline debt so the gate can merge before the remediation pass, but it must report the `needs-fix` count and reject any newly added `needs-fix` row outside the reviewed baseline debt set. True exemptions require rationale plus audit review.
5. Wire the new verifier script and acceptance test into ship-gate.
6. Keep `docs/ai_native_surface_audit.md` as the narrative ledger, but treat the new companion registry as the source of truth for completeness/no-stale enforcement.

## Non-Goals

- This gate does not prove runtime behavior for every branch. It proves the source-level contract that every scoped surface is known and either teaches or is intentionally exempt.
- This gate does not replace acceptance tests that trigger representative errors.
- This gate does not classify broad exception handlers; F9 remains the source for that.
- This gate does not require every success message to teach. It focuses on rejections, empty states, stop/wake affordances, and failure outputs that affect autonomous operation.
