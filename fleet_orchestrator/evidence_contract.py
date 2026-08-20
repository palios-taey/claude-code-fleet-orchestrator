"""Shared terminal-status evidence contract for CLI and API entrypoints."""

TERMINAL_STATUSES = frozenset({"completed", "failed", "interrupted"})
REQUEST_TERMINAL_EVIDENCE_KEYS = (
    "reason",
    "error",
    "production_observation",
    "audit_receipt",
    "commit_sha",
    "repo",
    "gate_run_id",
)
