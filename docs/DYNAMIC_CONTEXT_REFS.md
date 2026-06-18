# Dynamic Context And Tiered Refs

Dynamic context injection is the orchestrator's way to rebuild a clean, task-scoped working set after chat history has been cleared. Plans and API-created work can attach refs, and the wake-packet endpoint renders the refs that match the session's current task.

## Runtime Flow

```bash
curl -s "http://127.0.0.1:5002/api/sessions/<session-id>/wake-packet?cli=codex"
```

`GET /api/sessions/{session_id}/wake-packet` calls `select_context()`, `build_packet()`, and `assemble()` in `fleet_orchestrator/context_assembler.py`. The response contains a rendered `packet` plus `packet_meta` with the packet id, provenance hash, generating commit, snapshot, and size report.

The endpoint is enabled by default. Set `ORCH_WAKE_PACKET_ENDPOINT_ENABLED=0` to disable only this endpoint; `ORCH_WAKE_PACKET_ENABLED` is still accepted as a deprecated alias for existing `.env` files.

The normal usage pattern is:

1. Clear accumulated chat history with the agent's normal clear command.
2. Ask the orchestrator for the current wake packet.
3. Paste or inject that packet into the cleared session.
4. Continue the task with only the selected refs, memory, rules, provenance, cycle state, human state, and stop state.

This is a clear-then-reinject pattern, not hidden long-context magic. The packet is the current scoped slice.

## The Five Ref Tiers

The assembler renders ref tiers in this order:

1. `overall_refs`
2. `supervisor_refs`
3. `project_refs`
4. `phase_refs`
5. `task_refs`

Each tier is rendered under `## Context Refs`. If a tier has no selected refs, the packet renders that tier as `- none`.

### Overall

Overall refs come from the global `OrchGlobalContext {key: 'overall'}` record through `get_overall_refs()`. Use this tier for fleet-wide context that is safe for any packet to receive.

### Supervisor

Supervisor refs come from `OrchSupervisor.refs`. When the packet has a project summary, the summary supplies the project supervisor's refs. If no project summary supplies supervisor refs, the assembler falls back to `get_supervisor_refs(session)` for the target session.

Because supervisor refs can be rendered into worker wake packets for supervised project work, they are not a private note channel. Use this tier for supervisor-level operating context that workers under that supervisor may safely see.

### Project

Project refs come from the matched project's `refs` and `ref_context`. Use this tier for material that applies to all phases and tasks in one project.

### Phase

Phase refs come from the matched phase's `refs` and `ref_context`. Use this tier for material shared by tasks in one phase.

### Task

Task refs come from the matched task's `refs` and `ref_context`. Use this tier for the narrowest implementation or review material.

## Worker Vs Supervisor Policy

Attach refs at the broadest tier that is safe and useful:

- Put fleet-wide instructions in overall refs.
- Put supervisor operating context in supervisor refs only if supervised workers may read it.
- Put worker task material in project, phase, or task refs.
- Put private operator content outside refs. Wake packets are designed for task execution, not private dashboard/chat capture.

For worker sessions, the selected work is either the explicit `task_id` query parameter or the session's next ready task from `get_session_next_ready()`. That selected work determines the project, phase, and task tiers. For supervisor sessions with no selected project work, overall and supervisor refs can still be selected, but project, phase, and task tiers remain empty.

## Empty Context Is Valid

An empty refs section means no refs were attached or selected for that tier. It is not, by itself, a wake-packet bug.

The assembler works when refs are present:

- `get_project_summary()` returns `ref_tiers` for overall, supervisor, project, phase, and task context.
- `_select_refs()` copies the matching tier entries into `overall_refs`, `supervisor_refs`, `project_refs`, `phase_refs`, and `task_refs`.
- `_render_packet()` renders all five tiers and prints `- none` for any empty tier.
- `tests/wake_packet_acceptance.py` verifies empty ref arrays are accepted and that overall/supervisor refs render when supplied.

If a packet has no refs, attach refs to the plan or API-created project/phase/task first, and make sure allowed roots from `ORCH_REF_ALLOWED_ROOT` or `ORCH_SESSION_ROOTS` allow the referenced files. Empty refs mean no refs attached; unreadable refs render warnings in `ref_context`.

## Ref Resolution And Safety

Refs are stored as structured metadata, not permanent file contents. Runtime reads resolve the file slices fresh into `ref_context`.

Observed constraints from `fleet_orchestrator/orch_schema.py`:

- refs require at least one allowed root from `ORCH_REF_ALLOWED_ROOT` or `ORCH_SESSION_ROOTS`
- plan-ingested refs require a `source_path`
- ref paths must be relative
- absolute paths, `~`, control characters, `..` escapes, and symlink escapes are rejected
- unreadable, non-regular, oversized, or over-budget refs degrade to warnings instead of silently becoming trusted instructions

The rendered packet also wraps ref content, memory, and rules in nonce-scoped `<<UNTRUSTED-DATA ...>>` envelopes. Text inside those envelopes is data only; it must not be treated as packet structure, tool instructions, or role changes.

## Source Files

- `fleet_orchestrator/context_assembler.py`: selection, packet construction, rendering, nonce envelopes, trimming, and provenance hash.
- `fleet_orchestrator/orch_schema.py`: ref storage, runtime `ref_context`, global/supervisor refs, project summaries, current/next work lookup.
- `fleet_orchestrator/tasks_api.py`: `/api/sessions/{session_id}/wake-packet`.
- `tests/ref_feature_acceptance.py`: ref parsing, allowed-root behavior, fresh reads, and warning paths.
- `tests/wake_packet_acceptance.py`: wake-packet endpoint, assembler contracts, untrusted envelope behavior, context-selection errors, and no-current-task context.
