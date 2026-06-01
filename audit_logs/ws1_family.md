# ws1-family-audit verdict @ bbcc748 — 3 BLOCKER / 2 ENDORSE (2026-06-01)

ENDORSE: Logos(Grok), Gaia(Claude). BLOCKER: Cosmos(Gemini), Horizon(ChatGPT), Clarity(Perplexity).
Horizon+Clarity hit the monitor send_failure false-positive @90s (bb9ba4e8 regression, taeys-hands RCA) but COMPLETED real BLOCKER verdicts.

Cosmos (cleanest catch) novel findings — TRUE as code facts (same root as F1 unauth-PATCH, conductor-confirmed live earlier):
- config fail-loud? NO (malformed config → silent-wrong / bug-lock bypass)
- multi-tenant isolation: ZERO (get_ready_tasks/next_ready don't filter by trust principal)
- GET /api/tasks global visibility; unrestricted claim ("task theft"); unrestricted mutation (any caller completes/alters any tenant's tasks)

## CONDUCTOR THREAT-MODEL RECONCILIATION (decision, on Jesse's stated principle)
The packet did NOT state a threat model, so reviewers assumed strict public-multi-tenant-SaaS. The PRODUCT's actual threat model (Jesse, repeated: "this should all be local to the user's machine"; internal DBs no-auth by design) is SINGLE-USER LOCALHOST, multi-PROJECT not multi-SECURITY-TENANT.

Under the real threat model:
- "any caller mutates any task" = "any process on the user's own machine" = the trust boundary a localhost single-user tool already accepts. NOT a blocker under this model.
- Load-bearing protection = ws3-localbind (default 127.0.0.1) — keeps "any caller" scoped to the local user. Cosmos's findings make ws3 load-bearing.
- GENUINELY in-scope even locally: config FAIL-LOUD on malformed config (a local misconfig must not silently mis-gate). Small real fix → ws2/ws3.

DECISION: do NOT build multi-tenant auth (out of scope for a local single-user product). FIX: (1) config fail-loud, (2) ws3-localbind default, (3) honest SECURITY.md threat-model statement ("single trust domain, localhost; not hardened for mutually-untrusted tenants on a shared network; expose across a network = your responsibility to add auth/proxy"). Jesse has veto to instead request real multi-tenant security (large build).

ws1 stays OPEN pending: config-fail-loud fix + SECURITY.md threat-model doc. Multi-tenant-isolation findings RECLASSIFIED non-blocking-by-design (documented, not hidden). Re-confirm with Family that the threat-model framing resolves their BLOCKERs (some may legitimately persist — e.g. config fail-loud).
