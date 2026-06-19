# Exception Handler Registry Identity Design

## Problem

`scripts/verify-exception-classification.py` currently matches each critical broad
`except Exception` handler to `docs/EXCEPTION_HANDLERS.md` by
`(file, line, function)`. That preserves completeness, but it makes the gate
position-sensitive: unrelated edits that shift line numbers can turn an unchanged
handler into both a stale registry row and an unclassified code handler.

## Proposed Key

Use a structural handler id:

```text
<file>::<enclosing_qualname>::<exception_type>::<ordinal>
```

- `file`: repo-relative critical file path.
- `enclosing_qualname`: nearest lexical class/function path already produced by
  `_function_path`, for example `main`, `init_schema`, or
  `SomeClass.method`.
- `exception_type`: normalized caught exception expression. The current F9 gate
  only admits broad `Exception`; the field keeps the format explicit and leaves
  room for future broad-handler classes without rekeying the registry again.
- `ordinal`: 1-based AST order among handlers with the same
  `(file, enclosing_qualname, exception_type)`. Compute it after sorting handlers
  by `(lineno, col_offset)` within that group. Line numbers may remain in the
  registry as `Line Hint`, but they must not participate in matching.

The registry format should add identity columns and demote line number to a hint:

```text
| File | Function | Exception | Ordinal | Line Hint | Category | Rationale | Remediation |
```

## Gate Semantics

The verifier should build the set of actual handler ids from the AST and the set
of registered handler ids from the registry.

- `actual - registered` fails as unclassified.
- `registered - actual` fails as stale.
- duplicate registry ids fail.
- invalid categories still fail.
- `defect` handlers still require observable logging in the matched handler body.
- empty rationale/remediation still fail.
- a pure line shift passes because `Line Hint` is informational only.

This keeps the real safety property: every current critical-path broad handler
must have an intentional classification, and dead registry entries must be
removed.

## Required Ordinal Disambiguation

`(file, function, Exception)` alone is not unique today. Current main has 77
critical handlers but only 56 unique keys without ordinal. These groups require
the ordinal tie-breaker:

- `fleet_orchestrator/cli_orch_watch.py::investigate::Exception` lines 747, 778, 782
- `fleet_orchestrator/cli_orch_watch.py::main::Exception` lines 903, 924, 931, 948
- `fleet_orchestrator/cli_orch_watch.py::notify_supervisor_of_stuck::Exception` lines 507, 513
- `fleet_orchestrator/dispatch.py::_rollback_claim::Exception` lines 379, 423, 450
- `fleet_orchestrator/dispatch.py::check_previous_task::Exception` lines 834, 840
- `fleet_orchestrator/handoff_validation.py::_index_record::Exception` lines 80, 85
- `fleet_orchestrator/inflight.py::active_inflight_signal::Exception` lines 81, 94, 98
- `fleet_orchestrator/orch_schema.py::_dispatch_age_seconds::Exception` lines 1469, 1489
- `fleet_orchestrator/orch_schema.py::_resolve_chat_question::Exception` lines 3723, 3737
- `fleet_orchestrator/orch_schema.py::get_session_stop_decision::Exception` lines 1994, 2035, 2089
- `fleet_orchestrator/orch_schema.py::init_schema::Exception` lines 2109, 2116
- `fleet_orchestrator/orch_schema.py::resolve_ref_path::Exception` lines 592, 608
- `fleet_orchestrator/orch_schema.py::validate_source_path_for_refs::Exception` lines 548, 557
- `fleet_orchestrator/plan_readiness.py::check_readiness::Exception` lines 190, 214
- `fleet_orchestrator/worker_liveness.py::register_worker_task_liveness::Exception` lines 97, 108

## Acceptance Shape

Update `tests/exception_classification_acceptance.py` to prove the new invariant:

- current registry still passes.
- inserting a blank/comment before a critical handler does not fail the gate.
- adding an unclassified `except Exception` fails.
- leaving a registry row for a removed handler fails as stale.
- two `except Exception` handlers in one function are independently classified by
  ordinal.
- reverting to line-key matching fails the line-shift case.

## Migration Notes

The implementation should migrate existing registry rows mechanically from the
current `(file, line, function)` matches: discover all current handlers, assign
ordinals, match each old row to its current handler by the old key once, then emit
the identity-keyed row with the old line copied into `Line Hint`.
