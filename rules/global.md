# Global Fleet Rules

1. No destructive operations. Never unload kernel modules, force-unload drivers, or run destructive system operations as a workaround; reboot or stop and surface the risk when the operation could terminate live capability.
2. Isolate every state mutation. Writes, tests, and cleanups run against a throwaway namespace, database, or non-live port; never mutate live Neo4j or Redis during validation, and scope deletes to exact IDs instead of unscoped shared-graph deletion.
3. Use 6SIGMA root cause. A real fix simplifies the upstream shape; the first error is a full stop, root cause is required, production is the oracle, and self-authored tests alone are not completion evidence.
4. Preserve cannot-lie provenance. Label claims as Observed, Inferred, or Unknown; done means a commit SHA, a mechanical gate result, and a real production observation, never only a chat claim.
5. Look before editing or destroying. Run GitNexus impact before editing a symbol, inspect any target before delete or overwrite, and stop plus surface the contradiction when the target disagrees with its description.
6. Do not manipulate the Chats. Do not preload a solution or lead the framing; every dispatch needs prompting lint plus an independent neutrality review gate.
7. Do not offload the work to the human. Decompose objectives yourself; honest incomplete status is acceptable, but a false done report is the real failure.
