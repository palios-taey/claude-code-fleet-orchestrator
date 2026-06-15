# AUDIT.md — reviewer entry point: audit the code against its claims

**You are auditing this repository. Read this, then verify it against the source — do not trust it.**

This file is the single entry point for any code review of `claude-code-fleet-orchestrator`. It states, in falsifiable terms, what the system **claims** to do and is **intended** to do, and points to where each claim lives. Your job is to **clone the repo at the reviewed commit and check the code against every claim below** — confirm it, or flag the gap.

## How to audit (non-negotiable)
1. **The source at the reviewed SHA is the only oracle.** Not this document, not the maintainer's summary, not a dispatch packet. Clone/fetch `https://github.com/palios-taey/claude-code-fleet-orchestrator` at the exact SHA you were given and read the code.
2. **Enumerate yourself.** This doc may be incomplete or wrong. Don't rely on its lists — grep the source. Flag anything security- or accountability-relevant that the code does and this doc does **not** claim (unclaimed surface) as well as anything this doc claims that the code does **not** deliver (overclaim).
3. **This document is itself under audit.** Every claim here is a claim *about the code*. If a claim is false against the source, that is a finding — report it. Do not endorse a claim you did not verify.
4. **Three-register every finding** (Observed / Inferred / Unknown) with `file:line`. Default to BLOCK when you cannot verify.

## The map (where things live)
- **Code map + invariants:** `CLAUDE.md` (Code Map, Working Rules, Verification).
- **Feature claims + how to observe each:** `docs/CAPABILITIES.md` (live capability ledger — "if a row can't be observed by its named command/file, it's a bug").
- **Every env flag (66), defaults, posture:** `docs/CONFIGURATION.md`.

## The invariant claims (verify each against source)

**Accountability (claimed unconditional — no flag, no bypass):**
- C1. **No task reaches `completed` without passing the evidence gate.** `_validate_terminal_status_write` is called in `update_task_status` (`orch_schema.py:2521`) — after local/driver setup but **before any Cypher write**; the only `SET t.status='completed'` writes (`:2539`, `:2582`) are both inside that function, after the gate. `create_task` (`:2294`) likewise gates a terminal `initial_status`. No env guard wraps either call. Verify there is no Cypher/DB path that sets a task `completed` outside this gate. (Wording note: the gate is *not* literally the first statement — local-var/driver setup precedes it — but it precedes all DB writes; a 2026-06-15 reviewer correctly flagged the looser "runs first" phrasing.)
- C2. **Supervisor keep-going has no off-switch.** Hardcoded; the former `CF_SUPERVISOR_DISPATCH` flag was removed (comment remains). Verify no flag re-introduces a stop-while-ready-work bypass.
- C3. **The evidence gate checks shape, not truth.** It validates evidence *format* (e.g. `commit_sha` is 4–64 hex), and explicitly cannot verify a SHA exists (no git at runtime). Claim is bounded honestly — verify the boundary is as stated, not stronger.

**Network / exposure posture (claimed, with its real limits stated):**
- C4. **`ORCH_HOST` defaults to loopback (`127.0.0.1`).** Verify the default bind is private.
- C5. **The mutable API is tokenless by default; auth is enforced only when `ORCH_AUTH_TOKEN` is set.** A non-loopback bind without a token is reachable unauthenticated and the server only **logs a warning** (does not refuse to start). This is intended for a single trusted machine/LAN. Verify this is exactly the behavior (no stronger, no weaker) — and flag if you think warn-only is wrong for the product's claims.
- C6. **The public read-only surface (`:5005`, `scripts/orch-public`, `public_readonly.py`) is GET-only, fail-closed (shows nothing unless a session is explicitly allowlisted), and scrubs secrets/operator paths.** Verify there is no mutate/notify route and no leak path.

**Integrity / honesty:**
- C7. **Ship gates are fail-closed** — no declared gate tasks ⇒ not shippable; the config cannot be emptied/gamed to force a pass (`shippability.py`).
- C8. **No operator-specific identity or paths are baked in as defaults** — names/paths/IPs come from config/env; missing config fails loud, not a silent operator default. (Enforced by `tests/standalone_sessions_acceptance.py`, `tests/lane_state_acceptance.py`.)
- C9. **`version.py` is the single source of truth for the version**, and a release tag that disagrees with it fails CI (`.github/workflows/version-tag-consistency.yml`).

## Known gaps the code does NOT hide (verify these are still only-this-bad)
- G1. **Handoff/stop enforcement (`CF_HANDOFF_ENFORCE`, `CF_STOP_INPROGRESS`, …) is per-session opt-in via allowlists, with no enforce-all mode** — dormant for any session not enumerated. A global/default-on mode is planned, not built. (`handoff_validation.py:flags_for_session`, `orch_schema.py:_stop_inprogress_enabled`.)
- G2. **`CF_STOP_INPROGRESS` has a non-env enablement path:** a Redis set `{NOTIFY_KEY_PREFIX}:stop_inprogress_enabled`. Toggleable at runtime, invisible to env-flag audits.
- G3. **`ORCH_WAKE_PACKET_ENABLED` only gates the `/wake-packet` context endpoint** — it does NOT gate session waking (`send_wake`). The name overclaims.

## Deliverable
Per claim (C1–C9) and gap (G1–G3): CONFIRMED / GAP (with `file:line`). Plus: any unclaimed security/accountability-relevant behavior you found by reading the source. Then a top-level verdict: ENDORSE (code matches its stated claims; gaps are only as bad as stated) or BLOCK (named claim is false, or an unclaimed bypass exists).
