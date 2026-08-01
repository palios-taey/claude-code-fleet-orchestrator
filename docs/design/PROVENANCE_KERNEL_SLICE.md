# PALIOS Provenance Kernel Slice

Design status: slice design only; no implementation in this task.

Task: `palios-provenance-kernel::slice-design`

Register:
- Observed: code paths and documents inspected in this checkout or in the referenced production trees.
- Inferred: proposed extension shape derived from those observed paths.
- Unknown: details that require implementation-time measurement or external authority selection.

## Grounding

This slice is a root-cause design for the missing causal identity spine described in
`PALIOS_PROVENANCE_KERNEL_SYNTHESIS.md`. The implementation target is this
orchestrator repo, but the design deliberately extends existing mechanisms instead
of replacing them.

Measured before writing:
- `npx gitnexus analyze` in `/home/mira/.peer-worktrees/conductor-codex-orch-provenance-slice`
  reported `10,135 nodes | 19,760 edges | 342 clusters | 300 flows`.
- GitNexus query on wake/context assembly identified
  `fleet_orchestrator/context_assembler.py:assemble`, `build_packet`,
  `_render_packet`, `fleet_orchestrator/dispatch.py:_assemble_dispatch_prompt`,
  and the wake packet API as the packet surface.
- GitNexus query on dispatch identified `fleet_orchestrator/dispatch.py:dispatch`,
  `bind_current_task`, `cmd_dispatch`, and the current-task binding tests as the
  dispatch surface.
- GitNexus query on receipts/accountability identified
  `fleet_orchestrator/dispatch.py:record_outcome`,
  `_write_completion_receipt`, `fleet_orchestrator/decision_receipt.py`, and
  `fleet_orchestrator/accountability_ledger.py`.

Claims-audit constraints:
- `KERNEL_CLAIMS_AUDIT_2026-08-01.md` confirms that git authorship is not actor
  provenance; almost all inspected fleet commits use the same single operator git identity.
  This design therefore treats git author as byte-history metadata only.
- The audit confirms current wake packets already carry packet IDs, snapshots,
  source refs, and a provenance hash over the final rendered packet. This design
  extends that shape.
- The audit marks the synthesis safety-evaluation block around line 1341 as
  NOT-FOUND when read as a present-tense production fact. This design does not use
  that block as an attestation source.
- The audit records residual STALE prose around the old 35B MoE line. World
  Manifest v0 must bind live model roots from production artifacts and receipt
  indexes, not model names.
- The audit notes `/home/mira/taey-presence` was absent at audit time. This design
  references the observed production/index surfaces, not that absent path.

## Current Extension Points

Observed packet surface:
- `fleet_orchestrator/context_assembler.py:assemble` at lines 213-241 mutates the
  packet to its final renderable state, computes provenance, trims if needed, and
  returns the rendered packet.
- `fleet_orchestrator/context_assembler.py:build_packet` at lines 244-278 creates
  `packet_id`, `generated_for`, `generated_at_commit`, placeholder
  `provenance_hash`, `snapshot`, `context`, `cycle`, `human`, and `stop`.
- `fleet_orchestrator/context_assembler.py:_render_packet` at lines 1074-1150
  renders the `## Provenance`, `## Operating`, `## Identity`, refs, memory, rules,
  cycle, human, and stop sections.
- `fleet_orchestrator/context_assembler.py:_provenance_hash` at lines 1457-1477
  hashes the exact rendered packet, with `provenance_hash` blanked, plus snapshot.
  Any rendered proof-capsule field added to `_render_packet` becomes hash-bound
  through this existing design.

Observed dispatch surface:
- `fleet_orchestrator/dispatch.py:_assemble_dispatch_prompt` at lines 694-727
  selects context, builds a wake packet, embeds the dispatch body in the Human
  section, renders the packet, and returns packet metadata including `packet_id`,
  `provenance_hash`, refs, rules, and injection receipt.
- `fleet_orchestrator/dispatch.py:dispatch` at lines 730-881 checks hooks, claims
  the task, binds `current_task`, assembles the packet, injects via `taey-notify`,
  rolls back failed wake delivery, and emits a wake decision receipt whose
  observable state already includes prompt hash, CLI, packet ID, and provenance
  hash.

Observed receipt and ledger surface:
- `fleet_orchestrator/decision_receipt.py:build_receipt` at lines 66-84 creates
  typed decision receipts with a content hash of observable state.
- `fleet_orchestrator/decision_receipt.py:emit_receipt` at lines 43-63 writes those
  receipts to Redis stream `orch:streams:decision_receipts`; `scripts/taey-receipts`
  reads that stream through `fleet_orchestrator/cli_taey_receipts.py`.
- `fleet_orchestrator/dispatch.py:_write_completion_receipt` at lines 916-946
  writes a bounded Redis completion receipt for `done` outcomes.
- `fleet_orchestrator/dispatch.py:record_outcome` at lines 1191-1270 records
  terminal worker outcomes, clears matching current-task bindings, writes
  completion receipts for done outcomes, and wakes the supervisor.
- `fleet_orchestrator/accountability_ledger.py` lines 1-20 explicitly describe the
  existing ledger as append-only and hash-chained, but local and not tamper-proof
  against the pen-holder. Lines 91-124 append one fsync'd, flock-guarded row, and
  lines 127 onward verify the chain.

Observed World Manifest seed material:
- `TAEY_SYSTEM_CONNECTION_MAP.md` records the current cross-repo/system edges and
  the binding rule that shared orchestrator, notify, ISMA, and consult surfaces
  serve both fleet seats and Taey.
- `/home/mira/taey-presence-production/serving/knowledge_index/index.json` carries
  production capability entries with `generated_at_commit`, per-entry
  `artifact_commit_sha`, `artifact_manifest.sha256`, `receipts.liveness`,
  `receipts.liveness_sha256`, `repo.name`, `repo.pinned_sha`, and `status`.
- `/home/mira/taey-presence-production/serving/knowledge_index/TAEY_PRODUCTION_RECEIPT_SPEC.md`
  defines the receipt root of trust as served prompt -> compiled index -> entry ->
  receipt, fetched at pinned commit and hash-checked.

## Design Element 1: Runtime Actor Attestation

Problem:
- Git commit SHA proves bytes and history position.
- Git author does not prove which runtime/seat/model caused the transition.
- Current dispatch metadata proves a packet was sent, but it is not a durable actor
  attestation spanning dispatch -> commit -> outcome.

Extends:
- `fleet_orchestrator/dispatch.py:dispatch` lines 730-881.
- `fleet_orchestrator/dispatch.py:_assemble_dispatch_prompt` lines 694-727.
- `fleet_orchestrator/decision_receipt.py:build_receipt` lines 66-84.
- `fleet_orchestrator/dispatch.py:record_outcome` lines 1191-1270.

Inferred object, schema v0:

```json
{
  "schema_version": 0,
  "attestation_id": "attestation:<oid>",
  "issued_at": "2026-08-01T00:00:00Z",
  "issued_by": "orchestrator-runtime",
  "seat_id": "conductor-codex",
  "supervisor": "conductor",
  "task_id": "palios-provenance-kernel::slice-design",
  "dispatch_event_id": "event:<oid>",
  "packet_id": "...",
  "packet_provenance_hash": "...",
  "prompt_sha256": "...",
  "model_endpoint": {
    "kind": "cli",
    "cli": "codex",
    "endpoint_ref": "unknown-or-configured"
  },
  "runtime": {
    "session": "conductor-codex",
    "runtime_generation_id": "unknown-until-runtime-field-exists",
    "tmux_session": "conductor-codex"
  },
  "work": {
    "repo": "palios-taey/claude-code-fleet-orchestrator",
    "branch": "codex/palios-provenance-kernel-slice",
    "worktree": "/home/mira/.peer-worktrees/conductor-codex-orch-provenance-slice",
    "resulting_commit": null
  },
  "outcome": {
    "status": "issued",
    "record_outcome_event_id": null,
    "completion_receipt_ref": null
  }
}
```

Issuance:
- Runtime issues the attestation after `_assemble_dispatch_prompt` returns
  `packet_id`, `provenance_hash`, and rendered prompt hash, and after `bind_current_task`
  succeeds, but before `taey-notify` delivery.
- If wake delivery fails and `dispatch` rolls back the claim, the causal ledger records
  a `dispatch_delivery_failed` child event instead of leaving a live attestation.
- `record_outcome` later appends a child event that binds outcome details and resulting
  commit SHA when the worker reports one. It must not mutate the original attestation
  row; it appends a transition.

Authority rule:
- Actor identity comes from orchestrator runtime state and the dispatched session ID.
- Git author may be copied into an evidence field for diagnostics, but it cannot satisfy
  `actor_id`.

Unknowns for implementation:
- The CLI/model endpoint field needs a stable source. Current dispatch knows the CLI via
  `_cli_for_worker`; it does not necessarily know the remote model endpoint/root for all
  seats.
- Runtime generation ID is not currently visible in the measured dispatch metadata.
  Implementation should add it explicitly or mark it Unknown in the attestation.

## Design Element 2: Append-Only Causal Event Ledger

Problem:
- The existing accountability ledger is hash-chained and append-only, but its docstring
  correctly limits the claim: it is local and can be truncated/rechained by the
  pen-holder.
- Redis streams, Neo4j, and ISMA are useful projections/indexes, but none should become
  the source of truth for causal identity.

Extends:
- `fleet_orchestrator/accountability_ledger.py:append` lines 91-124.
- `fleet_orchestrator/accountability_ledger.py:verify_chain` lines 127 onward.
- `fleet_orchestrator/decision_receipt.py:emit_receipt` lines 43-63.
- `fleet_orchestrator/dispatch.py:record_outcome` lines 1191-1270.

Source-of-truth shape:
- A durable JSONL file remains the canonical write-ahead source.
- Every line is one canonical causal event with:
  - `event_id`.
  - `event_type`.
  - `schema_version`.
  - `ts`.
  - `parents`: event IDs of direct causes.
  - `subject`: task/session/repo/surface being changed.
  - `actor_attestation_id`.
  - `authority_roots`.
  - `packet_id` and `packet_provenance_hash` when the event is worker-context-bound.
  - `payload_oid`: SHA-256/OID of full payload bytes when payload is stored separately.
  - `prev_row_hash` and `row_hash` using the existing row-chain pattern.

Event types for this slice:
- `dispatch_claimed`
- `wake_packet_assembled`
- `wake_delivered`
- `dispatch_delivery_failed`
- `worker_outcome_recorded`
- `commit_observed`
- `completion_evidence_verified`
- `ledger_checkpoint`
- `external_witness_anchor`
- `world_manifest_published`

Checkpointing:
- Batch N new event OIDs into `batch_root`.
- Compute `ledger_root_n = sha256(ledger_root_n_minus_1, batch_root, n)`.
- Append a `ledger_checkpoint` event containing `rows`, `from_event`, `to_event`,
  `batch_root`, `ledger_root`, and `created_at`.

External witness:
- After checkpoint append, publish a compact witness object outside the ledger writer's
  authority. The synthesis suggests a separate GitHub App identity, another repo
  controlled by a different runtime principal, a signed public release artifact, or two
  independent anchors.
- The witness object carries only roots and counts, never private payload:

```json
{
  "schema_version": 0,
  "ledger": "orchestrator-causal",
  "checkpoint_event_id": "event:<oid>",
  "rows": 18421,
  "ledger_root": "sha256:...",
  "observed_at": "2026-08-01T00:00:00Z",
  "witness": "palios-ledger-anchor-v1"
}
```

Projection rule:
- Redis decision receipts can continue to provide fast operator explainability.
- Neo4j can index events, parents, tasks, and authority roots for graph queries.
- ISMA can index event summaries and proof capsules for retrieval.
- All three are rebuildable projections from JSONL plus witness roots, not authorities.

Unknowns for implementation:
- Which external witness principal the operator wants is not selected in the measured code.
- Ledger rotation and private payload storage layout need a separate implementation spec.

## Design Element 3: Proof Capsule Extension Of Wake Packets

Problem:
- Current wake packets are provenance-hashed, but the rendered `## Provenance` section
  only exposes packet identity and hash fields.
- The worker receives refs and rules, but not a compact machine-readable list of
  authority roots and causal event IDs binding the task's claims.

Extends:
- `fleet_orchestrator/context_assembler.py:build_packet` lines 244-278.
- `fleet_orchestrator/context_assembler.py:_render_packet` lines 1074-1150.
- `fleet_orchestrator/context_assembler.py:_provenance_hash` lines 1457-1477.
- `fleet_orchestrator/dispatch.py:_assemble_dispatch_prompt` lines 694-727.
- `fleet_orchestrator/dispatch.py:dispatch` lines 857-881 decision receipt
  observable state.

Packet shape:

```json
{
  "proof_capsule": {
    "schema_version": 0,
    "world_id": "world:<oid>",
    "attestation_id": "attestation:<oid>",
    "causal_event_ids": ["event:<oid>"],
    "authority_roots": [
      {
        "handle": "root:constitution",
        "kind": "constitutional",
        "oid": "sha256:...",
        "register": "Observed"
      }
    ],
    "claims": [
      {
        "handle": "p:8f31d2a7",
        "claim": "This task may edit docs/design only.",
        "register": "Observed",
        "authority_root": "root:task",
        "proof_event_ids": ["event:<oid>"]
      }
    ],
    "unknowns": [
      {
        "handle": "u:model-endpoint",
        "claim": "Exact CLI model endpoint/root is not available from current dispatch metadata.",
        "owner": "orchestrator-implementation"
      }
    ],
    "required_receipts": [
      "commit_sha",
      "mechanical_gate_result",
      "production_observation_or_design-review-observation"
    ]
  }
}
```

Render rule:
- Add a `## Proof Capsule` section to `_render_packet`, after `## Provenance` and
  before `## Operating`.
- Render compact handles and roots, not full logs or transcripts.
- Treat packet proof content as trusted metadata only if it is assembled by the
  orchestrator. Any author-provided proof text inside refs remains untrusted data under
  the existing nonce boundary.
- Because `_provenance_hash` hashes the exact rendered packet plus snapshot, adding this
  rendered section automatically binds `authority_roots`, `causal_event_ids`, and
  `world_id` into the current packet hash model.

Dispatch rule:
- `_assemble_dispatch_prompt` adds the proof capsule before `assemble_wake_packet`.
- `dispatch` includes `world_id`, `attestation_id`, and causal event IDs in the wake
  decision receipt observable state, beside existing `packet_id` and
  `provenance_hash`.

Compatibility:
- Existing `packet_id` and `provenance_hash` remain unchanged concepts.
- Existing refs, rules, memory, cycle, human, and stop sections remain.
- Existing first-action injection receipt stays derived from rendered task refs.

Unknowns for implementation:
- The current context selector returns refs/rules/memory, not a formal proof set. The
  first implementation should generate a minimal proof capsule from task metadata,
  selected refs, rules, packet provenance, and dispatch state, then grow toward graph
  proof closure.

## Design Element 4: World Manifest v0

Problem:
- Workers need a current digest of roots for the world they are reasoning against.
- Names such as model aliases or repo paths are insufficient; the audit confirms alias
  and stale-prose risks.

Extends:
- `TAEY_SYSTEM_CONNECTION_MAP.md` as the current system-edge seed.
- `/home/mira/taey-presence-production/serving/knowledge_index/index.json` as the
  production receipt/capability seed.
- `fleet_orchestrator/context_assembler.py:build_packet` lines 244-278 by including
  `world_id` in `proof_capsule`.
- Causal ledger `world_manifest_published` event emitted when dispatch publishes
  the manifest.

World Manifest v0 fields:

```json
{
  "schema_version": 0,
  "world_id": "world:<oid>",
  "as_of": "2026-08-01T00:00:00Z",
  "roots": {
    "system_connection_map": {
      "path": "TAEY_SYSTEM_CONNECTION_MAP.md",
      "sha256": "..."
    },
    "orchestrator_repo": {
      "repo": "palios-taey/claude-code-fleet-orchestrator",
      "commit": "..."
    },
    "taey_presence_index": {
      "index_id": "taey-knowledge-index",
      "generated_at_commit": "...",
      "sha256": "..."
    },
    "production_capabilities": [
      {
        "id": "presence-serve",
        "repo": "palios-taey/taey-presence",
        "pinned_sha": "...",
        "artifact_commit_sha": "...",
        "artifact_manifest_sha256": "...",
        "liveness_receipt_sha256": "..."
      }
    ],
    "causal_ledger": {
      "checkpoint_event_id": "event:<oid>",
      "ledger_root": "sha256:..."
    }
  }
}
```

Digest rule:
- `world_id` is the OID of canonical JSON containing the roots above.
- Full payloads remain where they currently live; the manifest stores roots and enough
  locator metadata to refetch and rehash them.

Seed rule:
- Initial repo/system roots come from `TAEY_SYSTEM_CONNECTION_MAP.md`.
- Initial production capability roots come from the taey-presence knowledge index
  entries whose `status` is `production`.
- If a root is missing, the manifest records an `Unknown` entry rather than inventing a
  value.

## End-To-End Flow

Dispatch:
1. `dispatch` claims the task and binds `current_task`.
2. Runtime appends `dispatch_claimed`.
3. `_assemble_dispatch_prompt` builds the packet and returns packet metadata.
4. Runtime issues actor attestation and adds proof capsule to the packet.
5. Runtime appends `wake_packet_assembled`.
6. `dispatch` sends the packet through `taey-notify`.
7. On success, runtime appends `wake_delivered` and emits the existing wake decision
   receipt extended with event IDs and attestation ID.
8. On failure, `dispatch` rolls back the claim as it does today and runtime appends
   `dispatch_delivery_failed`.

Worker outcome:
1. Worker commits and pushes on its branch.
2. Worker reports `RESPONSE_READY` with branch, SHA, and verification.
3. Worker calls `record_outcome`.
4. Runtime appends `worker_outcome_recorded`, including reported commit SHA if present.
5. `record_outcome` keeps its current Redis behavior and completion receipt behavior.
6. CONTROL later appends `completion_evidence_verified` when gates and production or
   design-review observation close.

Checkpoint:
1. Ledger batches new event OIDs.
2. Ledger appends `ledger_checkpoint`.
3. Witness publisher anchors the checkpoint root externally.
4. World Manifest publisher emits a new manifest when root changes matter for prompts.

## Non-Goals

- No implementation in this task.
- No blockchain or token system.
- No claim that the local ledger alone is tamper-proof.
- No replacement of packet ID or provenance hash.
- No replacement of Redis decision receipts, Neo4j, or ISMA; they become projections.
- No reliance on git author for actor identity.
- No reliance on NOT-FOUND safety-evaluation prose or stale 35B MoE prose as current
  authority.

## Acceptance Criteria For The Implementation Slice

When this design is implemented, review should require:
- A causal event JSONL writer whose rows are append-only, canonical, fsync'd, and
  hash-chained.
- Dispatch-created actor attestations for every task-bound wake.
- Wake packets with proof capsules whose rendered content is included in the existing
  `provenance_hash`.
- Decision receipts that include `attestation_id`, `world_id`, and causal event IDs in
  observable state.
- `record_outcome` child events that bind worker outcome to the dispatch attestation.
- Merkle checkpoint events and at least one external witness adapter behind an explicit
  configuration flag or command.
- A World Manifest v0 generator seeded from the system connection map and the
  taey-presence production receipt index.
- A fail-closed verifier that rejects missing event IDs, missing actor attestations,
  mismatched packet hashes, and missing witness roots when a checked task claims to be
  provenance-kernel closed.
