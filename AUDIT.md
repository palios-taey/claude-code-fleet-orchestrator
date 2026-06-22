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
- **Every env flag, defaults, posture:** `docs/CONFIGURATION.md`.
- **Operational issue discipline:** `OPERATIONAL_DISCIPLINE.md` (public-repo issues are treated as blocking incidents until closed, not ordinary backlog grooming).

## The invariant claims (verify each against source)

**Accountability (claimed unconditional — no flag, no bypass):**
- C1. **No task reaches `completed` without passing an authorization gate — and there are exactly TWO gated completion paths, no others.**
  1. **Ordinary completion** — `fleet_orchestrator/orch_schema.py:update_task_status` calls `fleet_orchestrator/orch_schema.py:_validate_terminal_status_write` after local/driver setup and **before any Cypher write**; its only `SET t.status='completed'` writes follow that gate. `fleet_orchestrator/orch_schema.py:create_task` likewise gates a terminal `initial_status`.
  2. **Human-review-gate completion** — `fleet_orchestrator/orch_schema.py:complete_human_review_gate` writes `t.status='completed'` via its OWN Cypher (it does NOT call `_validate_terminal_status_write`). This is the dashboard-UI path, gated differently: it matches only `task_type='human-review'`, requires a non-empty human `answer`, and sets `q.verified=true`. By design, ordinary agent/CLI completion **cannot** complete a human-review task — so this is a *more-restricted* path with a human-answer authorization gate, **not** an evidence-less bypass.

  Verify there is no *third* `SET t.status='completed'` writer, and that path 2 genuinely requires the human answer. (A 2026-06-15 DR reviewer correctly refuted the earlier wording "no DB path outside `update_task_status`" — path 2 exists; the security property "no evidence-less/agent-reachable bypass" still holds.)
- C2. **Supervisor keep-going has no off-switch.** Hardcoded; the former `CF_SUPERVISOR_DISPATCH` flag was removed (comment remains). Verify no flag re-introduces a stop-while-ready-work bypass.
- C3. **The evidence gate checks shape, not truth.** It validates evidence *format* (e.g. `commit_sha` is 4–64 hex), and explicitly cannot verify a SHA exists (no git at runtime). Claim is bounded honestly — verify the boundary is as stated, not stronger.

**Network / exposure posture (claimed, with its real limits stated):**
- C4. **`ORCH_HOST` defaults to loopback (`127.0.0.1`).** Verify the default bind is private.
- C5. **The mutable API is tokenless by default on loopback; auth is enforced only when `ORCH_AUTH_TOKEN` is set.** Startup refuses to serve the mutable API on a non-loopback bind without either `ORCH_AUTH_TOKEN` or `ORCH_ALLOW_UNAUTH_NON_LOOPBACK=1`. Verify `fleet_orchestrator/tasks_api.py:_enforce_mutable_api_exposure`: loopback starts, non-loopback with a token starts, non-loopback with `ORCH_ALLOW_UNAUTH_NON_LOOPBACK=1` starts with an explicit exposure acknowledgement log, and non-loopback without either raises `SystemExit` before serving. The override is an exposure-intent acknowledgement for a trusted single-user LAN, not authentication.
- C6. **The public read-only surface (`:5005`, `scripts/orch-public`, `public_readonly.py`) is GET-only, fail-closed (shows nothing unless a session is explicitly allowlisted), and scrubs secrets/operator paths.** Verify there is no mutate/notify route and no leak path.

**Integrity / honesty:**
- C7. **Ship gates are fail-closed** — no declared gate tasks ⇒ not shippable; the config cannot be emptied/gamed to force a pass (`shippability.py`).
- C8. **No operator-specific identity or paths are baked in as defaults** — names/paths/IPs come from config/env; missing config fails loud, not a silent operator default. (Enforced by `tests/standalone_sessions_acceptance.py`, `tests/lane_state_acceptance.py`.)
- C9. **`version.py` is the single source of truth for the version**, and a release tag that disagrees with it fails CI (`.github/workflows/version-tag-consistency.yml`).
- C10. **`orch-watch` independently monitors notification-delivery liveness by default.** Verify `fleet_orchestrator/cli_orch_watch.py`: `ORCH_NOTIFY_DAEMON_WATCHDOG` defaults on, `check_notify_daemon_liveness` checks the delegated router service and `taey:_notify_daemon:heartbeat` freshness, `check_stuck_inbox_delivery` checks old `${NOTIFY_KEY_PREFIX:-taey}:*:inbox` messages, and watchdog failures alert out-of-band through direct tmux submission rather than depending on `taey-notify`.

## Known gaps the code does NOT hide (verify these are still only-this-bad)
- G1. **Wake-packet gating is endpoint-scoped.** The canonical flag is now `ORCH_WAKE_PACKET_ENDPOINT_ENABLED`; the deprecated `ORCH_WAKE_PACKET_ENABLED` alias remains for old `.env` files. Either flag gates only the `/wake-packet` context endpoint, not session waking (`send_wake`).

## Recently closed gaps reviewers should regression-check
- R1. Stop-on-in-progress is no longer per-session opt-in. Handoff validation helpers remain present, but pending/unacked handoff records are **not** a stop-decision blocker on current main. There should be no live `CF_HANDOFF_ENFORCE`, `CF_HANDOFF_SESSION_FLAGS_FILE`, `CF_STOP_INPROGRESS`, `flags_for_session`, or Redis `{prefix}:stop_inprogress_enabled` bypass in runtime code; pending handoff records should not produce a stop block. (`tests/no_flag_bypass_acceptance.py`.)

## Deliverable
Per claim (C1–C10), live gap (G1), and regression target (R1): CONFIRMED / GAP (with `file:line`). Plus: any unclaimed security/accountability-relevant behavior you found by reading the source. Then a top-level verdict: ENDORSE (code matches its stated claims; gaps are only as bad as stated) or BLOCK (named claim is false, or an unclaimed bypass exists).
