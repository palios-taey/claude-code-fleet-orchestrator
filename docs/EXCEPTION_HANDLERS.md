# Critical Exception Handler Classification

This registry covers every broad `except Exception` handler in the critical orchestrator paths named by audit F9 / issue #114.

Categories:

- `intentional-fail-open`: the handler deliberately continues an action when the guard/sidecar fails, usually to avoid missing a wake.
- `intentional-fail-closed`: the handler deliberately refuses to prove progress, stop, liveness, or success when the read/check fails.
- `harmless-best-effort`: the handler protects parsing, rendering, optional telemetry, or cleanup side effects whose failure must not crash the live loop.
- `defect`: the handler previously masked a production-diagnostic failure. Remediation is conservative logging only unless stated otherwise.

The gate is `scripts/verify-exception-classification.py`, wired through `tests/exception_classification_acceptance.py` and ship-gate. It fails if a critical-path broad handler is missing here, if a row points at a non-current line/function, or if a `defect` row lacks observable logging in the handler body.

## Registry

| File | Line | Function | Category | Rationale | Remediation |
| --- | ---: | --- | --- | --- | --- |
| fleet_orchestrator/cli_orch_watch.py | 159 | resolve_supervisor | harmless-best-effort | Parent Redis read can fail; suffix fallback keeps watch classification available. | Annotated in registry; no behavior change. |
| fleet_orchestrator/cli_orch_watch.py | 183 | get_current_task | harmless-best-effort | Malformed current_task is returned as raw data for supervisor diagnosis. | Annotated in registry; no behavior change. |
| fleet_orchestrator/cli_orch_watch.py | 194 | get_last_outcome | harmless-best-effort | Malformed last_outcome is surfaced as unknown with raw details. | Annotated in registry; no behavior change. |
| fleet_orchestrator/cli_orch_watch.py | 248 | _target_stop_decision_allows_stop | intentional-fail-closed | A failed stop-decision consult must not suppress a wake as allow-stop. | Existing error log; no behavior change. |
| fleet_orchestrator/cli_orch_watch.py | 293 | _process_handoff_timeouts | harmless-best-effort | One dispatcher's handoff processing failure must not kill the watcher loop. | Existing error log; no behavior change. |
| fleet_orchestrator/cli_orch_watch.py | 306 | _stop_gate_dedup | intentional-fail-open | If dedup Redis fails, the safer direction is to send the stop-gate wake. | Existing error log; no behavior change. |
| fleet_orchestrator/cli_orch_watch.py | 386 | _handle_user_stop_gate | intentional-fail-closed | A blocked_on validation error must not suppress the stop-gate wake. | Existing warning log; no behavior change. |
| fleet_orchestrator/cli_orch_watch.py | 507 | notify_supervisor_of_stuck | intentional-fail-closed | A blocked_on validation error must not suppress PEER_IDLE supervision. | Existing warning log; no behavior change. |
| fleet_orchestrator/cli_orch_watch.py | 513 | notify_supervisor_of_stuck | harmless-best-effort | Bad started_at only affects diagnostic duration text. | Annotated in registry; no behavior change. |
| fleet_orchestrator/cli_orch_watch.py | 653 | handle_done_del | harmless-best-effort | Readiness checker is a wake heuristic; failure is logged and watcher continues. | Existing error log; no behavior change. |
| fleet_orchestrator/cli_orch_watch.py | 682 | _redis_now | harmless-best-effort | Redis TIME fallback to local clock keeps duration math available. | Annotated in registry; no behavior change. |
| fleet_orchestrator/cli_orch_watch.py | 747 | investigate | harmless-best-effort | Bad last_activity in grace-window drift check falls back to zero. | Annotated in registry; no behavior change. |
| fleet_orchestrator/cli_orch_watch.py | 778 | investigate | harmless-best-effort | Bad last_activity in stuck math only skips false-positive timing. | Annotated in registry; no behavior change. |
| fleet_orchestrator/cli_orch_watch.py | 782 | investigate | harmless-best-effort | Bad started_at in stuck math only skips false-positive timing. | Annotated in registry; no behavior change. |
| fleet_orchestrator/cli_orch_watch.py | 903 | main | harmless-best-effort | Pubsub read failure is logged and retried so the daemon stays alive. | Existing error log; no behavior change. |
| fleet_orchestrator/cli_orch_watch.py | 924 | main | harmless-best-effort | Sweep failure is logged and the watcher loop continues. | Existing error log; no behavior change. |
| fleet_orchestrator/cli_orch_watch.py | 931 | main | harmless-best-effort | Background liveness failure is logged and retried on next loop. | Existing error log; no behavior change. |
| fleet_orchestrator/cli_orch_watch.py | 948 | main | harmless-best-effort | A single investigate failure is logged without killing all watches. | Existing error log; no behavior change. |
| fleet_orchestrator/context_assembler.py | 313 | _safe_context_record | harmless-best-effort | Optional ref-context retrieval emits an unavailable marker instead of failing packet assembly. | Annotated in registry; no behavior change. |
| fleet_orchestrator/context_assembler.py | 554 | _ledger_tail | harmless-best-effort | Ledger tail is diagnostic context; failure is returned as unavailable metadata. | Annotated in registry; no behavior change. |
| fleet_orchestrator/context_assembler.py | 773 | _git_head | harmless-best-effort | Git hash is packet metadata; failure falls back to unknown. | Annotated in registry; no behavior change. |
| fleet_orchestrator/decision_receipt.py | 38 | maybe_emit_receipt | harmless-best-effort | Decision receipts are optional telemetry and must not block the decision path. | Existing exception log; no behavior change. |
| fleet_orchestrator/decision_receipt.py | 60 | emit_receipt | harmless-best-effort | Redis receipt sink is optional telemetry and failure returns None. | Existing exception log; no behavior change. |
| fleet_orchestrator/dispatch.py | 345 | _rollback_claim_only | intentional-fail-closed | Rollback failure is visible, but the caller must keep surfacing the original dispatch failure. | Existing warning log; no behavior change. |
| fleet_orchestrator/dispatch.py | 379 | _rollback_claim | intentional-fail-closed | If current_task ownership cannot be read, reverting blindly could clobber newer work. | Existing warning log; no behavior change. |
| fleet_orchestrator/dispatch.py | 423 | _rollback_claim | intentional-fail-closed | Neo4j rollback failure is visible while preserving the original dispatch failure. | Existing warning log; no behavior change. |
| fleet_orchestrator/dispatch.py | 450 | _rollback_claim | intentional-fail-closed | Binding cleanup failure is visible and must not mask the dispatch failure. | Existing warning log; no behavior change. |
| fleet_orchestrator/dispatch.py | 762 | check_previous_task | harmless-best-effort | Malformed current_task is preserved as raw data for supervisor handling. | Annotated in registry; no behavior change. |
| fleet_orchestrator/dispatch.py | 768 | check_previous_task | harmless-best-effort | Malformed last_outcome becomes unknown with raw details. | Annotated in registry; no behavior change. |
| fleet_orchestrator/handoff_validation.py | 36 | _default_pickup_poll_budget | harmless-best-effort | Bad environment config falls back to the documented default budget. | Annotated in registry; no behavior change. |
| fleet_orchestrator/handoff_validation.py | 45 | _json_dict | harmless-best-effort | Malformed handoff JSON is treated as absent. | Annotated in registry; no behavior change. |
| fleet_orchestrator/handoff_validation.py | 80 | _index_record | harmless-best-effort | Index TTL alignment is a sidecar optimization after the record is already written. | Annotated in registry; no behavior change. |
| fleet_orchestrator/handoff_validation.py | 85 | _index_record | harmless-best-effort | Index expiry adjustment failure must not make the handoff write fail. | Annotated in registry; no behavior change. |
| fleet_orchestrator/handoff_validation.py | 370 | process_expired_handoffs | intentional-fail-closed | Failed wake delivery is recorded on the handoff instead of crashing timeout processing. | Existing error metadata is persisted; no behavior change. |
| fleet_orchestrator/inflight.py | 28 | _json_dict | harmless-best-effort | Malformed notify-state JSON is treated as absent. | Annotated in registry; no behavior change. |
| fleet_orchestrator/inflight.py | 81 | active_inflight_signal | intentional-fail-closed | If current_task cannot be read, the code will not prove the task active from that key. | Annotated in registry; no behavior change. |
| fleet_orchestrator/inflight.py | 94 | active_inflight_signal | intentional-fail-open | If terminal outcome cannot be read, a fresh heartbeat may still prove live work. | Annotated in registry; no behavior change. |
| fleet_orchestrator/inflight.py | 98 | active_inflight_signal | intentional-fail-closed | If heartbeat cannot be read, this worker cannot prove live work. | Annotated in registry; no behavior change. |
| fleet_orchestrator/orch_schema.py | 340 | _normalize_refs | harmless-best-effort | Bad ref section bounds drop only that section. | Annotated in registry; no behavior change. |
| fleet_orchestrator/orch_schema.py | 406 | _allowed_ref_roots | harmless-best-effort | Bad JSON env format falls back to comma/path parsing. | Annotated in registry; no behavior change. |
| fleet_orchestrator/orch_schema.py | 442 | _ref_allowed_root | harmless-best-effort | Invalid source path cannot contribute an allowed root. | Annotated in registry; no behavior change. |
| fleet_orchestrator/orch_schema.py | 453 | validate_source_path_for_refs | intentional-fail-closed | Invalid source_path is rejected for ref validation. | Annotated in registry; no behavior change. |
| fleet_orchestrator/orch_schema.py | 462 | validate_source_path_for_refs | intentional-fail-closed | Invalid source_path with refs is rejected. | Annotated in registry; no behavior change. |
| fleet_orchestrator/orch_schema.py | 497 | resolve_ref_path | harmless-best-effort | Bad source-relative base is skipped; explicit allowed roots are still checked. | Annotated in registry; no behavior change. |
| fleet_orchestrator/orch_schema.py | 513 | resolve_ref_path | intentional-fail-closed | Unreadable ref path is rejected with an explicit warning string. | Annotated in registry; no behavior change. |
| fleet_orchestrator/orch_schema.py | 527 | _git_head_for_path | harmless-best-effort | Ref provenance falls back to nogit when Git metadata is unavailable. | Annotated in registry; no behavior change. |
| fleet_orchestrator/orch_schema.py | 612 | _read_ref_context | harmless-best-effort | Ref read failure becomes per-section unreadable warnings. | Annotated in registry; no behavior change. |
| fleet_orchestrator/orch_schema.py | 862 | _resolve_supervisor_session | harmless-best-effort | Parent Redis read failure falls back to suffix/session resolution. | Annotated in registry; no behavior change. |
| fleet_orchestrator/orch_schema.py | 883 | _configured_dashboard_supervisors | harmless-best-effort | Supervisor resolution failure falls back to normalized session id. | Annotated in registry; no behavior change. |
| fleet_orchestrator/orch_schema.py | 970 | _safe_observed_stop_task_id | harmless-best-effort | Stop fail-closed fallback only keeps current task id if it can be read. | Annotated in registry; no behavior change. |
| fleet_orchestrator/orch_schema.py | 1374 | _dispatch_age_seconds | intentional-fail-closed | If task age cannot be read, freshness cannot be proven. | Annotated in registry; no behavior change. |
| fleet_orchestrator/orch_schema.py | 1394 | _dispatch_age_seconds | intentional-fail-closed | If updated_at cannot be parsed, freshness cannot be proven. | Annotated in registry; no behavior change. |
| fleet_orchestrator/orch_schema.py | 1439 | get_session_liveness | intentional-fail-closed | If heartbeat cannot be read, session liveness falls back to idle/not active. | Annotated in registry; no behavior change. |
| fleet_orchestrator/orch_schema.py | 1899 | get_session_stop_decision | intentional-fail-closed | Stop-engine errors must block stopping and label the keystone failure. | Existing fail-closed decision payload; no behavior change. |
| fleet_orchestrator/orch_schema.py | 1940 | get_session_stop_decision | harmless-best-effort | Stale convergence counter cleanup must not alter a non-convergable block. | Annotated in registry; no behavior change. |
| fleet_orchestrator/orch_schema.py | 1979 | get_session_stop_decision | intentional-fail-open | Convergence marker Redis failure cannot enforce the release valve; decision is returned with marker metadata. | Existing decision metadata; no behavior change. |
| fleet_orchestrator/orch_schema.py | 1999 | init_schema | intentional-fail-closed | Constraint creation errors are collected so startup can refuse with schema errors. | Existing error aggregation; no behavior change. |
| fleet_orchestrator/orch_schema.py | 2006 | init_schema | intentional-fail-closed | Index creation errors are collected so startup can refuse with schema errors. | Existing error aggregation; no behavior change. |
| fleet_orchestrator/orch_schema.py | 2410 | _clear_matching_current_task | intentional-fail-closed | Malformed current_task is not deleted because ownership cannot be proven. | Annotated in registry; no behavior change. |
| fleet_orchestrator/orch_schema.py | 3597 | _resolve_chat_question | harmless-best-effort | Malformed open-question rows are preserved while resolving valid rows. | Annotated in registry; no behavior change. |
| fleet_orchestrator/orch_schema.py | 3611 | _resolve_chat_question | harmless-best-effort | Malformed needs_you payload is ignored instead of blocking answer resolution. | Annotated in registry; no behavior change. |
| fleet_orchestrator/out_of_band.py | 89 | heartbeat_out_of_band_task | intentional-fail-closed | Malformed registration aborts heartbeat with explicit RuntimeError. | Annotated in registry; no behavior change. |
| fleet_orchestrator/out_of_band.py | 112 | out_of_band_task_active | intentional-fail-closed | Malformed registration cannot prove out-of-band work active. | Annotated in registry; no behavior change. |
| fleet_orchestrator/plan_readiness.py | 138 | _dedup_wake | intentional-fail-open | Redis dedup failure allows the wake to avoid missing newly ready work. | Existing debug log; no behavior change. |
| fleet_orchestrator/plan_readiness.py | 190 | check_readiness | harmless-best-effort | Readiness wake is advisory and documented as best-effort. | Existing debug log; no behavior change. |
| fleet_orchestrator/plan_readiness.py | 214 | check_readiness | intentional-fail-open | Notify Redis import/connect failure disables dedup but still allows wake evaluation. | Annotated in registry; no behavior change. |
| fleet_orchestrator/tasks_api.py | 219 | _init_schema_on_startup | harmless-best-effort | Handoff index backfill is startup repair, not a startup prerequisite. | Existing warning log; no behavior change. |
| fleet_orchestrator/tasks_api.py | 501 | update | defect | Mutating task update 500s were returned to callers without server-side context. | Added LOGGER.exception; response behavior unchanged. |
| fleet_orchestrator/tasks_api.py | 547 | create_human_review_gate_endpoint | defect | Mutating gate creation 500s were returned without phase/task/question context in logs. | Added LOGGER.exception; response behavior unchanged. |
| fleet_orchestrator/tasks_api.py | 573 | answer_question_endpoint | defect | Mutating question answer 500s were returned without question id in logs. | Added LOGGER.exception; response behavior unchanged. |
| fleet_orchestrator/tasks_api.py | 596 | ui_answer_human_review_gate_endpoint | defect | UI human-review answer 500s were returned without question id in logs. | Added LOGGER.exception; response behavior unchanged. |
| fleet_orchestrator/tasks_api.py | 1157 | session_wake_packet | intentional-fail-closed | Wake-packet assembly errors return ok:false to the caller. | Annotated in registry; no behavior change. |
| fleet_orchestrator/tasks_api.py | 1178 | health | intentional-fail-closed | Health probe reports 503 when ready-task lookup fails. | Annotated in registry; no behavior change. |
| fleet_orchestrator/worker_liveness.py | 97 | register_worker_task_liveness | intentional-fail-closed | Neo4j registration failure means worker liveness was not registered. | Existing warning log; no behavior change. |
| fleet_orchestrator/worker_liveness.py | 108 | register_worker_task_liveness | harmless-best-effort | Redis sidecar failure is logged after Neo4j liveness registration succeeds. | Existing warning log; no behavior change. |
| fleet_orchestrator/worker_liveness.py | 119 | clear_worker_task_liveness | defect | Redis cleanup failure could leave stale liveness sidecar state with no production signal. | Added LOG.warning; return behavior unchanged. |
| fleet_orchestrator/worker_liveness.py | 129 | _json_dict | harmless-best-effort | Malformed worker-liveness JSON is treated as absent. | Annotated in registry; no behavior change. |
