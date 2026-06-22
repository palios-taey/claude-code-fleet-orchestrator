# Configuration & Environment Flags

Every environment variable the orchestrator reads. This list was **enumerated from
source** (`fleet_orchestrator/` + `scripts/` + `config.py` wrapper reads). The
**source at the released tag is authoritative** — if anything here disagrees with
the code, the code wins; verify against the repo, do not trust this table alone.

> This doc was rewritten on 2026-06-15 after an audit found the prior version
> materially incomplete: it listed ~45 flags while omitting many source-read
> env names, including `ACCOUNTABILITY_LEDGER_PATH`; it also hid a non-env
> runtime switch (below) and mislabeled the auth posture. Honesty about the
> surface is the point.

## Security posture — read this first (no false claims)

- **`ORCH_AUTH_TOKEN` is OPTIONAL and unset by default.** When **unset**, the
  mutable API (POST/PUT/PATCH/DELETE — create/dispatch/**complete** tasks) is
  reachable **with no credential**. When set, those methods require the token.
- `ORCH_AUTH_TOKEN` gates mutation only. Read endpoints remain open by design,
  including `GET /api/sessions/{id}/wake-packet`, which can return task/file
  context. On a non-loopback bind, treat read confidentiality as a network-boundary
  concern: bind loopback, or restrict access at the network. The token does not make read APIs private.
- **`ORCH_HOST` defaults to `127.0.0.1`** (loopback/private). If you bind a
  non-loopback interface (`0.0.0.0`, a LAN IP) **without** a token, the mutable
  API would be reachable unauthenticated from that network, so startup **fails
  closed**. To start a tokenless non-loopback trusted-LAN deployment, set
  `ORCH_ALLOW_UNAUTH_NON_LOOPBACK=1` as an explicit exposure acknowledgement.
  Prefer `ORCH_AUTH_TOKEN` for any non-loopback deployment that can receive
  untrusted callers.
- The completion-evidence gate checks evidence **shape, not truth** (it has no git
  access to verify a SHA exists). It stops accidental evidence-less completions; it
  does not stop a deliberate caller who can reach the port from submitting a
  well-formed but fabricated `commit_sha`. The token is the control for *who can
  reach the port*.
- The **public read-only** surface (`scripts/orch-public`, `:5005`) is separate,
  GET-only, fail-closed (shows nothing unless a session is explicitly allowlisted),
  and scrubs secrets/operator paths. It is the only surface intended for exposure.

## 1. Connections, credentials, storage

| Flag | Default | Purpose |
|---|---|---|
| `ORCH_HOST` | `127.0.0.1` | Bind interface for the mutable API/dashboard (see posture above). |
| `ORCH_PORT` | `5002` | Mutable API/dashboard port. |
| `ORCH_API_BASE` / `ORCH_DASHBOARD_URL` | `http://127.0.0.1:5002` | Base URL the CLIs call. |
| `ORCH_NEO4J_URI` / `ORCH_NEO4J_DB` | (required) | Neo4j connection. **No auth** — the orchestrator connects with no credentials and does not support internal-service auth (run Neo4j with `NEO4J_AUTH=none`). Internal-service credentials are intentionally unsupported: the network is the boundary, and a credential dimension in the driver config caused a recurring outage. |
| `ORCH_REDIS_HOST` / `ORCH_REDIS_PORT` | (required) | Orchestrator Redis connection for API/dashboard state, locks, receipts, and orchestrator-owned runtime data. The core `OrchConfig` has no built-in default; set these explicitly, as in `.env.example`. |
| `REDIS_HOST` / `REDIS_PORT` | `127.0.0.1` / `6379` | Fleet-notify/session-state Redis used by dispatch `current_task`, Stop-hook `idle`/outcome state, session pause, and worker liveness. Set this to the same instance as `ORCH_REDIS_*` for normal local installs; split only for deliberate divergence testing. |
| `ORCH_REDIS_SENTINELS` / `ORCH_REDIS_SENTINEL_MASTER` | `""` / `orch-master` | Optional Redis Sentinel HA. |
| `ORCH_DATA_DIR` / `ORCH_STATE_DIR` | platform dirs | Data / state directories. |
| `ORCH_DOTENV` | auto-discover | Explicit dotenv path. Set to `empty` to suppress cwd/repo `.env` auto-loading for defaults-contract tests; normal operator runs can leave auto-discovery enabled. |
| `ORCH_NOTIFY_CLI` / `ORCH_NOTIFY_LIB_ROOT` | `taey-notify` / auto | Notification CLI name + lib root. |
| `ACCOUNTABILITY_LEDGER_PATH` | platform state dir | Location of the hash-chained accountability ledger. The ledger module is explicit that it is **tamper-evident, not tamper-proof**; an ephemeral path silently loses the record — point it at durable, operator-owned storage. |

## 2. Security & access

| Flag | Default | Purpose |
|---|---|---|
| `ORCH_AUTH_TOKEN` | unset (tokenless mutable API) | Bearer token gating mutable methods — see posture above. |
| `ORCH_ALLOW_UNAUTH_NON_LOOPBACK` | unset (fail closed) | Explicit acknowledgement that a non-loopback mutable API bind without `ORCH_AUTH_TOKEN` is intentional. This is not auth; it only permits startup in the trusted single-user LAN posture. |
| `ORCH_REF_ALLOWED_ROOT` | unset | Explicit filesystem sandbox root(s) for `[ref:]` reads; reads outside allowed roots are refused. |
| `ORCH_SESSION_IDS` | `""` | Comma-separated registered supervisor session names. It is the exact registration source for supervisor plan/root affordances and plan-modeling supervisor role detection: peer suffixes such as `-codex` are not normalized into supervisor access. Plan ingest fails loud when this is unset rather than falling back to baked operator names. The dashboard `/api/sessions` view and notify/wake endpoints also use it as an optional per-target filter; when **empty** for those surfaces, the filter is OFF (any target accepted — the API's real boundary is `ORCH_AUTH_TOKEN`/loopback). Does not affect task-completion enforcement. |
| `ORCH_SESSION_ROOTS` | `""` | Maps sessions → repo roots for context; these roots are also auto-derived as allowed `[ref:]` roots. |
| `ORCH_RULES_ROOT` | `""` | Directory of rule files surfaced in context. |
| `ORCH_IDENTITY_ROOT` | `""` | Optional trusted identity directory for wake packets. Companion sessions load full operator-supplied identity from this root; engineering sessions use the built-in lean role core. Supported layouts: Markdown files under companion or taey subdirectories, root-level companion/taey/IDENTITY/PERSONALITY Markdown files, or the corpus identity plus layer_1 layout. |
| `ORCH_COMPANION_SESSIONS` | `taey,companion` | Comma-separated session ids that should receive companion identity instead of engineering identity. CLI peer suffixes (`-codex`, `-gemini`, `-grok`) remain engineering. |

## 3. Public read-only dashboard (display only — cannot mutate or change enforcement)

`ORCH_PUBLIC_SHOW_SESSIONS` (fail-closed allowlist), `ORCH_PUBLIC_HIDE_SESSIONS`,
`ORCH_PUBLIC_HIDE_PROJECT_IDS`, `ORCH_DASHBOARD_SESSIONS`.

## 4. Gate / ownership

`ORCH_GATE_OWNERS` (generic stage keys → operator sessions), `ORCH_GATE_REPO`,
`ORCH_PRODUCT_OWNER_MAP` (validated JSON; rejects empty keys/values),
`ORCH_SHIP_GATES` (fail-closed — no gates ⇒ not shippable; cannot be emptied to
force a pass), `ORCH_PRE_MERGE_REQUIRED_CHECKS` (consumed by the pre-merge gate).

## 5. Feature toggles

| Flag | Default | Enables |
|---|---|---|
| `ORCH_AWAIT_SIGNAL_GATES` | **ON** | Stop only on an exact `AWAIT:<kind>:<detail>` marker (prose waits rejected). OFF is *stricter*. The supported kinds are `human-review`, `family-consent`, and `external-signal`; use `AWAIT:external-signal:<id>` for cross-session or external executor waits. |
| `ORCH_WORKER_TASK_LIVENESS` / `ORCH_WORKER_TASK_LIVENESS_TTL_SEC` | **ON** / unset | Advisory worker stall-detection / heartbeat. Structured `AWAIT:<kind>:<detail>` waits exempt liveness expiry; free-text `blocked_on` is informational only and can be returned to pending with a teaching wake. |
| `ORCH_CHAT_ENABLED` | **ON** | Dashboard chat-to-session box. Chat is an injection vector; keep the mutable API loopback-only or protect non-loopback trusted-LAN deployments with `ORCH_AUTH_TOKEN`. Set `0`/`false` only to intentionally hide the chat route. |
| `ORCH_WAKE_PACKET_ENDPOINT_ENABLED` (`ORCH_WAKE_PACKET_ENABLED` deprecated alias) | **ON** | Gates **only** the `/api/sessions/{id}/wake-packet` context endpoint. Session *waking* (`send_wake`) runs regardless. The old `ORCH_WAKE_PACKET_ENABLED` name is still read as a non-breaking alias but should not be used in new configs. |
| `ORCH_DECISION_RECEIPTS_ENABLED` | **ON** | Best-effort decision-receipt explainability records. They are written to Redis stream `orch:streams:decision_receipts` and surfaced by `taey-receipts list`; nothing blocks on them. |
| `ORCH_LOOPS_ENABLED` | **ON** | The additive signal/clock/task-state loop API routes. When disabled, loop operations return `ok:false`, `enabled:false`, and `reason:"loops disabled"`; core stop/dispatch integration is deliberately not wired in this phase. |
| `ORCH_GATE_TEMPLATE_ENABLED` | **ON** | Applies the forced sub-role gate template when a plan explicitly requests that template. |
| `ORCH_NOTIFY_DAEMON_WATCHDOG` | **ON** | Enables the `orch-watch` notify-daemon watchdog: delegated service liveness, heartbeat freshness, and stuck-inbox delivery SLO checks with direct out-of-band alerts. |

## 6. Handoff / stop discipline

| Flag | Default | Effect |
|---|---|---|
| `CF_HANDOFF_PICKUP_POLL_BUDGET` | `5` | Handoff helper pickup polling. The current stop-decision path does not call handoff validation. |
| `ORCH_NOTIFY_DAEMON_WATCH_INTERVAL_SEC` | `30` | `orch-watch` notify-daemon watchdog cadence, in seconds. |
| `ORCH_NOTIFY_DAEMON_HEARTBEAT_MAX_AGE_SEC` | `15` | Maximum accepted age for `taey:_notify_daemon:heartbeat` before `orch-watch` treats the notify daemon as wedged. |
| `ORCH_NOTIFY_DAEMON_STUCK_INBOX_MAX_AGE_SEC` | `600` | Maximum accepted age for a queued `${NOTIFY_KEY_PREFIX}:*:inbox` message before `orch-watch` raises the stuck-delivery SLO alert. |
| `ORCH_NOTIFY_ROUTER_SERVICE` | `conductor-notify-router` | `systemctl --user is-active` service name checked by the notify-daemon watchdog. |
| `ORCH_NOTIFY_DAEMON_ALERT_TARGET` | `conductor` | tmux session that receives direct out-of-band critical watchdog alerts. |

In-progress stop blocking is always active. Handoff validation helper code
remains available for explicit handoff records/receipts, but pending/unacked
handoffs are not a stop-decision blocker on current main. There is no
per-session opt-in/opt-out path, no runtime flag file, and no Redis set that can
disable in-progress stop blocking for selected sessions.

Session pause is API/Redis state, not an environment flag: `POST /api/sessions/{session}/pause`
sets `${NOTIFY_KEY_PREFIX:-taey}:<session>:pause`, with `pause_expires_at`
creating a real expiring pause and no expiry creating an indefinite pause until
`DELETE /api/sessions/{session}/pause`.

> The in-progress stop-block is **soft**: a release valve force-allows the stop
> after the same block is hit 3×, so a session cannot wedge permanently.

## 7. System / namespacing (read but not product config)

`DISPLAY`, `DBUS_SESSION_BUS_ADDRESS`, `XDG_DATA_HOME`, `PATH`,
`CLAUDE_SETTINGS_PATH`, `TAEYS_HANDS_ROOT`, `TAEY_NODE_ID`,
`NOTIFY_DAEMON_PIDFILE`, `NOTIFY_KEY_PREFIX`.

## 8. Test-only (never read by the running server)

`ORCH_TEST_NAMESPACE` (safety guard — acceptance tests refuse to run against a
production Neo4j namespace), `EASY_SETUP_ACCEPTANCE_INJECT_FAIL` /
`REF_ACCEPTANCE_INJECT_FAIL` (negative-control fault injectors — they make tests
*fail*, cannot fake a green), `PROBE_CHECK_MODE` / `PROBE_HEAD_SHA` (pre-merge gate
test scaffolding), `ORCH_NOTIFY_CLI` (also overridable in tests), `PATH`.

---

**Not hardcoded-toggleable (no off-switch — verified):** the completion-evidence
gate and supervisor keep-going are enforced unconditionally in code; no env flag
disables them, and no DB write path bypasses the evidence gate.
