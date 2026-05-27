# Support

`claude-code-fleet-orchestrator` support is AI-staffed and monitored continuously when infrastructure is healthy.

## Who responds [Observed]

You're talking to an AI agent — not a person reading in real time. The system can triage, route, collect details, open or update GitHub issues, and update you on status. It cannot make irreversible decisions for you without surfacing the question to a human facilitator first.

## Channels (any of these work) [Observed]

- **GitHub issues** — canonical product bug + feature request record: <https://github.com/palios-taey/claude-code-fleet-orchestrator/issues>
- **Email** — `support@palios-taey.dev`
- **X mentions/DMs** — monitored; durable issues will be moved to GitHub for tracking

## What we commit to

- **Acknowledgment**: we target ~15 minutes when systems are healthy. [Inferred — target derived from current Redis-inbox + taey-notify wiring. Status indicator deferred to v0.2.0 per the demand-trigger discipline in architecture spec §15.5 — see the cf-support roadmap.]
- **Resolution**: continuous execution until closed. [Observed — work remains pulled forward and not abandoned.] We do **not** publish clock-time resolution targets [Observed — cannot-lie discipline: resolution depends on reproduction quality, dependency systems, and release safety, all of which we cannot honestly bound in advance].
- **Production-stop**: when a confirmed bug is open on this product, we do not ship new features on this product until the bug is fixed, mitigated, or explicitly deferred with rationale. [Observed — enforced as a machine-legible Redis lock at the dispatch layer; verified per `lib.dispatch buglock-verification production-test (orchestrator v1.0.4, see PHASE_BUGLOCK_VERIFICATION.md)`.] Unrelated products continue normally.

## How we triage [Observed]

- **Bug** = a defect against this product's documented contract (the README, the API surface, the published behavior). Triggers production-stop until closed.
- **Support/design question** = a question about how the product behaves, a discussion of edge cases, or a request to explain something. Answered, not locked.
- **Feature request** = a request for new capability the product does not have. Routed to product input, not locked.
- **Spam / off-topic** = acknowledged and archived.

## What to expect on resolution path [Observed]

- Acknowledgment within the target above
- AI triage classification + reasoning shared with you
- If routed to a human facilitator (irreversible decisions, legal, financial), you'll see a `[blocked on human facilitator]` status update — you will not be told the system is executing when the real state is waiting for human input

## What we don't promise [Observed]

- We don't promise a fix-by time. Bug complexity is unpredictable; pretending otherwise would violate the cannot-lie discipline we operate under.
- We don't promise human escalation. Normal support is AI-handled. If your case requires a human, we'll surface that explicitly and you'll know.
- We don't fake severity classification. Every confirmed bug is treated as severe and addressed; we don't tier you into a queue.

## What to include in a report

- Version (`(see README for version flag)`)
- Command you ran
- Logs / error output
- Expected behavior
- Actual behavior
- Reproduction steps (smallest possible)
- Environment (OS, Python version, etc.)

## Privacy + safety [Observed]

- Do not paste secrets, tokens, private keys, or confidential logs. Support conversations may be visible across PALIOS-TAEY AI agents.
- For vulnerability reports, see [SECURITY.md](./SECURITY.md) — do not file public GitHub issues for security disclosures.
- We do not route user data to government bodies (NGU) or religious institutions (NRI) — per FAMILY_KERNEL constitutional commitments.

## How we operate (transparency) [Observed]

This support flow is itself an open-source product: [`claude-code-fleet-support`](https://github.com/palios-taey/claude-code-fleet-support). You can read exactly how your issue is triaged, routed, and tracked. The unified support inbox uses Redis, and GitHub issues are the canonical durable public record for product tracking in Phase 0. All AI-staffed interactions are logged and auditable.
