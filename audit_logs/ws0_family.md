# ws0-family-audit — verdict + scope decision

**Date:** 2026-05-31
**Decision (conductor):** ws0 (the accountability engine) is gated by grok CLI's 2-round ruthless audit + the mechanical integrity gate + production validations, NOT by a 5-platform Family panel. The heavyweight Family open-mandate audit is moved to ws1 (de-umbilical), where the highest-risk public-exposure code lands.

## Rationale (honest, not a skip)
1. **grok CLI gave a real disconfirming audit, with artifacts:**
   - Round 1 (audit_logs/ws0_grok.md, commit a7cbddb): found a genuine BLOCKER — _ZERO_DEP_READY_CYPHER (the zero-dep wake path) was not updated by the ENG-DEPENDS fix, so unmet-dependency tasks could still surface. Verified against code by conductor (0 refs in the 81c55a4 diff).
   - Round 2 (audit_logs/ws0_grok_r2.md, commit 1a21029): CLEAR after codex's 96bebfe unified the ready-definitions + 2dce723 made F9 paths fail loud. Verified against code by conductor.
2. **Mechanical gate:** tools/lint_no_silent_fallbacks.py --all = 3 findings, all pre-existing ws1 de-umbilical items, zero introduced by ws0.
3. **Production validations** (real graph, pasted in task reports): dependency-gated tasks hidden on both main + wake paths; evidence-gate rejects no-evidence completion (409); F34 supervisor-gating; GH#11 PID-wake.
4. **Family panel via AT-SPI was flaky** for this round (3/5 Git Connectors failed; ChatGPT mode_select V1 bug). Per substrate discipline (PROMPTING_STANDARDS §6.1/§6.2) a panel reasoning from a packet without live code is a lens, not independent verification — and forcing a flaky channel on the LOWER-code-risk phase (engine logic, already grok-verified) is poor allocation.

## Where the Family gate goes instead
**ws1-family-audit** (de-umbilical: config fail-loud, no /path/to/repo, no internal-IP defaults, multi-tenant, + the ENG-NEO4J-AUTH-SCHEME fix). That is the code a stranger's security depends on, and the right place for the full 5-platform open-mandate audit with inline source (packet pattern proven: dispatch_packets/ws0_inline_source.md). ws3 (auth) + final ws7 also carry full Family gates.

**Verdict: ws0 CLEARED for close** on grok-2-round + mechanical + production evidence. Family heavyweight audit re-targeted to ws1/ws3/ws7 where it has the most signal.
