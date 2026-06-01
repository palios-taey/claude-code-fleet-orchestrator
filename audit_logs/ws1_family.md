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

---

## CORRECTION 2026-06-01 (Jesse): multi-tenant framing STRUCK, not reclassified.
There is NO multi-tenant. The product is a LOCAL single-user single-machine tool. The :5002 API is local glue between the user's own CLIs (taey-task/taey-plan) + own web dashboard and the user's own Neo4j (verified: ORCH_DASHBOARD_URL=localhost:5002, app.js fetch('/api/...')). One tenant = the user. No external callers. Adopters each run their own copy on their own hardware.

Therefore Cosmos/Horizon/Clarity BLOCKERs (tenant isolation, task theft, unauth mutation) are NOT real findings — they audited a public-multi-tenant-service that this is not. The cause was my packet omitting the product shape; reviewers pattern-matched "HTTP API" → "hosted service." Struck.

REAL residue, total:
- config FAIL-LOUD on malformed config (genuine even for one local user — a local misconfig shouldn't silently mis-gate). → small fix.
- ws3-localbind: default 127.0.0.1 so it isn't needlessly on the LAN. Network HYGIENE, not a threat model. No auth.
- SECURITY.md: state plainly "local single-user tool; runs on your machine; internal services are local + unauthenticated by design; not a hosted/multi-user service."

ws1 effectively PASSES on the 2 genuine ENDORSEs (Gaia+Logos) once those 2 small items land. NO multi-tenant work. Future packets carry the product-shape header (memory: product_is_local_single_user_not_a_service) so this phantom never recurs.

---
## config-fail-loud FIXED @ 0ea4f5d (Gaia's lone ENDORSE gate) — 2026-06-01
_parse_product_owner_map (config.py:210): blank→{} (intentional); valid json/kv→dict; MALFORMED nonempty→raise OrchConfigError. Behaviorally verified (UNSET={}, KV={'a':'b'}, JSON={'a':'b'}, MALFORMED→RAISED). codex also handles json-parses-but-not-dict (edge beyond Gaia's repro). Gate CLEAN, origin==local.
ws1 correctness COMPLETE. Remaining ws1 = ws3-localbind (default 127.0.0.1) + SECURITY.md (local-single-user threat model) — then ws1 closes. NO multi-tenant.
