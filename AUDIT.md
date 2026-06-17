# AUDIT.md — reviewer entry point: audit the code against its claims

**You are auditing this repository. Read this, then verify it against the source — do not trust it.**

This file is the single entry point for any code review of `claude-code-fleet-orchestrator`. It states, in falsifiable terms, what the system **claims** to do and is **intended** to do, and points to where each claim lives. Your job is to **read the published repo and check the code against every claim below** — confirm it, or flag the gap.

## How to audit (non-negotiable)
1. **The published source is the only oracle.** Not this document, not the maintainer's summary, not a dispatch packet. Read `https://github.com/palios-taey/claude-code-fleet-orchestrator` (the repo as published — review what's there; you are not expected to navigate to a specific commit).
2. **Enumerate yourself.** This doc may be incomplete or wrong. Don't rely on its lists — grep the source. Flag anything security- or accountability-relevant that the code does and this doc does **not** claim (unclaimed surface) as well as anything this doc claims that the code does **not** deliver (overclaim).
3. **This document is itself under audit.** Every claim here is a claim *about the code*. If a claim is false against the source, that is a finding — report it. Do not endorse a claim you did not verify.
4. **Three-register every finding** (Observed / Inferred / Unknown) with `file:line`. Default to BLOCK when you cannot verify.

## The map (where things live)
- **Code map + invariants:** `CLAUDE.md` (Code Map, Working Rules, Verification).
- **Feature claims + how to observe each:** `docs/CAPABILITIES.md` (live capability ledger — "if a row can't be observed by its named command/file, it's a bug").
- **Every env flag (66), defaults, posture:** `docs/CONFIGURATION.md`.

## The invariant claims (verify each against source)

**Accountability (claimed unconditional — no flag, no bypass):**
- C1. **No task reaches `completed` without passing an authorization gate — and there are exactly TWO gated completion paths, no others.**
  1. **Ordinary completion** — `update_task_status` (`orch_schema.py`) calls `_validate_terminal_status_write` at `:2523` (after local/driver setup, **before any Cypher write**); its only `SET t.status='completed'` writes (`:2541`, `:2584`) follow that gate. `create_task` (`:2296`) likewise gates a terminal `initial_status`.
  2. **Human-review-gate completion** — `complete_human_review_gate` (`:3818`) writes `t.status='completed'` at `:3845` via its OWN Cypher (it does NOT call `_validate_terminal_status_write`). This is the dashboard-UI path, gated differently: it matches only `task_type='human-review'`, requires a non-empty human `answer`, and sets `q.verified=true`. By design, ordinary agent/CLI completion **cannot** complete a human-review task — so this is a *more-restricted* path with a human-answer authorization gate, **not** an evidence-less bypass.

  Verify there is no *third* `SET t.status='completed'` writer, and that path 2 genuinely requires the human answer. (A 2026-06-15 DR reviewer correctly refuted the earlier wording "no DB path outside `update_task_status`" — path 2 exists; the security property "no evidence-less/agent-reachable bypass" still holds.)
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
- G1. **`ORCH_WAKE_PACKET_ENABLED` only gates the `/wake-packet` context endpoint** — it does NOT gate session waking (`send_wake`). The name overclaims.

## Recently closed gaps reviewers should regression-check
- R1. Handoff validation and stop-on-in-progress are no longer per-session opt-in. There should be no live `CF_HANDOFF_ENFORCE`, `CF_HANDOFF_SESSION_FLAGS_FILE`, `CF_STOP_INPROGRESS`, `flags_for_session`, or Redis `{prefix}:stop_inprogress_enabled` bypass in runtime code. (`tests/no_flag_bypass_acceptance.py`.)

## Deliverable
Per claim (C1–C9), live gap (G1), and regression target (R1): CONFIRMED / GAP (with `file:line`). Plus: any unclaimed security/accountability-relevant behavior you found by reading the source. Then a top-level verdict: ENDORSE (code matches its stated claims; gaps are only as bad as stated) or BLOCK (named claim is false, or an unclaimed bypass exists).
