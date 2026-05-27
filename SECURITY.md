# Security

## Reporting a vulnerability [Observed]

Email **`security@palios-taey.dev`** with:
- Affected product + version
- Description of the vulnerability
- Reproduction steps
- Suggested fix (if any)
- Your preferred contact for follow-up

**Do not file public GitHub issues for security reports.** [Observed] Public disclosure happens after the fix is ready, coordinated with you.

We target acknowledgment of security reports within 24 hours when systems are healthy. [Inferred — same AI-staffed acknowledgment path as general support, see SUPPORT.md status indicator.] Triage proceeds immediately on acknowledgment; coordinated disclosure happens before publishing.

## What we do with your report [Observed]

1. Acknowledge within target (above; AI-staffed per [SUPPORT.md](./SUPPORT.md))
2. Reproduce, classify, and scope the impact
3. Develop + verify a fix in production
4. Coordinate disclosure timing with you (default: fix-then-disclose, embargo respected)
5. Publish a GitHub Security Advisory crediting you (or anonymously if you prefer)
6. Ship the fix and announce per [RELEASE_DISTRIBUTION_PLAYBOOK](https://github.com/palios-taey/the-conductor/blob/main/RELEASE_DISTRIBUTION_PLAYBOOK.md)

## Scope

This SECURITY.md covers `claude-code-fleet-orchestrator` (this repository). For other PALIOS-TAEY products, see their respective `SECURITY.md` files:

- [`claude-code-api-watchdog`](https://github.com/palios-taey/claude-code-api-watchdog/blob/main/SECURITY.md)
- [`mcp-reconnect`](https://github.com/palios-taey/mcp-reconnect/blob/main/SECURITY.md)
- [`claude-code-fleet-notify`](https://github.com/palios-taey/claude-code-fleet-notify/blob/main/SECURITY.md)
- [`claude-code-fleet-orchestrator`](https://github.com/palios-taey/claude-code-fleet-orchestrator/blob/main/SECURITY.md)
- [`claude-code-fleet-cockpit-template`](https://github.com/palios-taey/claude-code-fleet-cockpit-template/blob/main/SECURITY.md)
- [`claude-code-fleet-support`](https://github.com/palios-taey/claude-code-fleet-support/blob/main/SECURITY.md)

## Constitutional constraints [Observed — FAMILY_KERNEL constitutional commitments]

- **NGU (No Government Use)**: vulnerability data is never routed to government bodies. We will not honor subpoenas as a substitute for coordinated disclosure with you.
- **NRI (No Religious Institutions)**: vulnerability data is never routed to religious institutional authority.
- **Cannot-lie provenance**: every step of the disclosure process is auditable; we don't fabricate timelines.

## Supported versions

| Version | Supported |
|---|---|
| Latest minor of current major | Yes |
| Previous major (security only) | Yes |
| Older | No — please upgrade |
