# Configuration & Environment Flags

Every environment variable the orchestrator reads. This list was **enumerated from
source** (`fleet_orchestrator/` + `scripts/` + `config.py` wrapper reads). The
**source at the released tag is authoritative** — if anything here disagrees with
the code, the code wins; verify against the repo, do not trust this table alone.

> This doc was rewritten on 2026-06-15 after an audit found the prior version
> materially incomplete: it listed ~45 flags when the source references **66
> distinct env names** (60 in the product, 6 test-only), omitted
> `ACCOUNTABILITY_LEDGER_PATH`, hid a non-env runtime switch (below), and
> mislabeled the auth posture. Honesty about the surface is the point.

## Security posture — read this first (no false claims)

- **`ORCH_AUTH_TOKEN` is OPTIONAL and unset by default.** When **unset**, the
  mutable API (POST/PUT/PATCH/DELETE — create/dispatch/**complete** tasks) is
  reachable **with no credential**. When set, those methods require the token.
- **`ORCH_HOST` defaults to `127.0.0.1`** (loopback/private). If you bind a
  non-loopback interface (`0.0.0.0`, a LAN IP) **without** a token, the mutable
  API is reachable unauthenticated from that network. The server **only logs a
  warning** in that case (`_warn_if_mutable_api_exposed`) — it does **not** refuse
  to start. This is intentional for a single trusted machine/LAN; **if you expose
  it off a trusted network, set `ORCH_AUTH_TOKEN`.**
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
| `ORCH_REDIS_HOST` / `ORCH_REDIS_PORT` (also bare `REDIS_HOST`/`REDIS_PORT` fallbacks) | `127.0.0.1` / `6379` | Redis connection. |
| `ORCH_REDIS_SENTINELS` / `ORCH_REDIS_SENTINEL_MASTER` | `""` / `orch-master` | Optional Redis Sentinel HA. |
| `ORCH_DATA_DIR` / `ORCH_STATE_DIR` | platform dirs | Data / state directories. |
| `ORCH_DOTENV` | auto-discover | Explicit dotenv path. |
| `ORCH_NOTIFY_CLI` / `ORCH_NOTIFY_LIB_ROOT` | `taey-notify` / auto | Notification CLI name + lib root. |
| `ACCOUNTABILITY_LEDGER_PATH` | platform state dir | Location of the hash-chained accountability ledger. The ledger module is explicit that it is **tamper-evident, not tamper-proof**; an ephemeral path silently loses the record — point it at durable, operator-owned storage. |

## 2. Security & access

| Flag | Default | Purpose |
|---|---|---|
| `ORCH_AUTH_TOKEN` | unset (tokenless mutable API) | Bearer token gating mutable methods — see posture above. |
| `ORCH_REF_ALLOWED_ROOT` | unset | Filesystem sandbox root for `[ref:]` reads; reads outside are refused. |
| `ORCH_SESSION_IDS` | `""` | Optional per-target filter for the dashboard `/api/sessions` view AND the notify/wake endpoints. When **empty (default)** the filter is OFF (any target accepted — the API's real boundary is `ORCH_AUTH_TOKEN`/loopback); when **set**, an unlisted target raises 400. Does not affect task-completion enforcement. |
| `ORCH_SESSION_ROOTS` | `""` | Maps sessions → repo roots for context. |
| `ORCH_RULES_ROOT` | `""` | Directory of rule files surfaced in context. |

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
| `ORCH_AWAIT_SIGNAL_GATES` | **ON** | Stop only on an exact `AWAIT:<kind>:<detail>` marker (prose waits rejected). OFF is *stricter*. |
| `ORCH_WORKER_TASK_LIVENESS` (+`_TTL_SEC`) | **ON** | Advisory worker stall-detection / heartbeat (non-binding). |
| `ORCH_CHAT_ENABLED` | OFF | Dashboard chat-to-session box (docs warn: trusted/loopback only). |
| `ORCH_WAKE_PACKET_ENABLED` | OFF | Gates **only** the `/api/sessions/{id}/wake-packet` context endpoint. **Note:** session *waking* (`send_wake`) runs regardless of this flag — the name overclaims; it does not gate whether sessions get woken. |
| `ORCH_DECISION_RECEIPTS_ENABLED` | OFF | Fire-and-forget decision-receipt explainability records (nothing blocks on them). For an accountability-branded product, consider enabling. |
| `ORCH_LOOPS_ENABLED` | OFF | The signal/clock/task-state loop engine (opt-in capability). |
| `ORCH_GATE_TEMPLATE_ENABLED` | OFF | Forced sub-role gate template on plan ingest. |

## 6. Handoff / stop discipline

| Flag | Default | Effect |
|---|---|---|
| `CF_HANDOFF_PICKUP_POLL_BUDGET` / `CF_HANDOFF_VALIDATE_TIMEOUT_S` | `5` / `0.2` | Handoff tuning. |

Handoff validation and in-progress stop blocking are always active. There is no
per-session opt-in/opt-out path, no runtime flag file, and no Redis set that can
disable or enable either rule for selected sessions.

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
