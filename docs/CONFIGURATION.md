# Configuration & Feature Flags

This is the complete, authoritative list of every environment variable the
orchestrator reads. It exists because the flag surface had drifted: more than
half of these flags were undocumented, and nothing distinguished a legitimate
operator option from an internal toggle that could weaken the system's own
accountability guarantees.

**The rule for this file (project rule, 2026-06-15):** every flag listed here
must be a genuine operator preference/option that makes sense for a user running
the product — *not* a mechanism for an instance to bypass accountability or the
intended functionality of the system. Each flag's classification below was
adversarially validated by independent reviewers against that rule.

Legend for **In our deployment**: the value set in the operator's `.env` on the
authors' fleet, or `(unset → default)`.

---

## 1. Connections & storage (operator config)

| Flag | Default | Gates | In our deployment |
|---|---|---|---|
| `ORCH_HOST` | `127.0.0.1` | Bind interface for the mutable API/dashboard. Non-loopback is an explicit opt-in. | unset → `127.0.0.1` |
| `ORCH_PORT` | `5002` | Mutable API/dashboard port. | unset → `5002` |
| `ORCH_API_BASE` / `ORCH_DASHBOARD_URL` | `http://127.0.0.1:5002` | Base URL the CLIs call. | `http://10.0.0.163:5002` |
| `ORCH_REDIS_HOST` / `ORCH_REDIS_PORT` | `127.0.0.1` / `6379` | Redis connection. | `127.0.0.1` / `6379` |
| `ORCH_REDIS_SENTINELS` / `ORCH_REDIS_SENTINEL_MASTER` | `""` / `orch-master` | Optional Redis Sentinel HA. | unset |
| `ORCH_NEO4J_URI` / `ORCH_NEO4J_DB` | (required) | Neo4j connection. | `bolt://10.0.0.163:7689` / `neo4j` |
| `ORCH_DATA_DIR` / `ORCH_STATE_DIR` | platform dirs | Data/state directories. | unset → default |
| `ORCH_DOTENV` | auto-discover | Explicit dotenv path. | unset |
| `ORCH_NOTIFY_CLI` / `ORCH_NOTIFY_LIB_ROOT` | `taey-notify` / auto | Notification CLI name + lib root. | unset → default |

All of the above are ordinary deployment config — host/port/credentials/paths.
None affects what counts as "done" or whether discipline is enforced.

## 2. Security & access (operator config)

| Flag | Default | Gates | In our deployment |
|---|---|---|---|
| `ORCH_AUTH_TOKEN` | unset (no auth) | Optional bearer token gating mutable methods. | unset (loopback-only) |
| `ORCH_REF_ALLOWED_ROOT` | unset | Filesystem sandbox root for `[ref:]` reads. Reads outside it are refused. | `/home/mira/the-conductor,/home/mira/hunter` |
| `ORCH_SESSION_IDS` | `""` | Dashboard `/api/sessions` allowlist (UI filter only — does not affect enforcement). | unset |
| `ORCH_SESSION_ROOTS` | `""` | Maps sessions → repo roots for context. | unset |
| `ORCH_RULES_ROOT` | `""` | Directory of rule files surfaced in context. | unset |

## 3. Public read-only dashboard (operator config)

| Flag | Default | Gates | In our deployment |
|---|---|---|---|
| `ORCH_PUBLIC_SHOW_SESSIONS` | fail-closed | Allowlist of sessions shown on the public read-only surface. | `conductor,weaver,tutor,infra,hunter,taey-ed` |
| `ORCH_PUBLIC_HIDE_SESSIONS` | unset | Denylist override. | unset |
| `ORCH_PUBLIC_HIDE_PROJECT_IDS` | unset | By-id project hide backstop. | unset |
| `ORCH_DASHBOARD_SESSIONS` | `""` | Session list the dashboard renders. | unset |

These scope the *read-only* public view; they cannot mutate state or change enforcement.

## 4. Gate / ownership mapping (operator config)

| Flag | Default | Gates | In our deployment |
|---|---|---|---|
| `ORCH_GATE_OWNERS` | generic stage keys | Maps generic gate stages → operator session names. | unset → generic |
| `ORCH_GATE_REPO` | repo root | Repo the gate process runs against. | unset → default |
| `ORCH_PRODUCT_OWNER_MAP` | `{}` | Optional worker→product owner remap for dispatch. | unset |
| `ORCH_SHIP_GATES` | built-in stage list | Names of the ship-gate stages. | unset → default |
| `ORCH_PRE_MERGE_REQUIRED_CHECKS` | unset | Required CI checks the pre-merge gate enforces. | unset |

## 5. Feature toggles (capabilities — default OFF unless noted)

These enable optional capabilities. Default-off means the capability is inert
until an operator turns it on. **Scrutiny point:** a default-off toggle is only
legitimate if the capability is genuinely optional — not if it gates core
intended functionality that the product claims to provide.

| Flag | Default | Enables | In our deployment |
|---|---|---|---|
| `ORCH_CHAT_ENABLED` | OFF | Dashboard chat-to-session box. | `1` (on) |
| `ORCH_AWAIT_SIGNAL_GATES` | **ON** | `AWAIT:<kind>:<detail>` declared-wait stop gates. | unset → on |
| `ORCH_WORKER_TASK_LIVENESS` | **ON** | TTL auto-requeue + supervisor wake for stalled dispatched tasks. | unset → on |
| `ORCH_WAKE_PACKET_ENABLED` | OFF | Per-session wake-state packets (dynamic context). | see note |
| `ORCH_DECISION_RECEIPTS_ENABLED` | OFF | Decision-receipt log events. | unset → off |
| `ORCH_LOOPS_ENABLED` | OFF | The signal/clock/task-state loop engine. | unset → off |
| `ORCH_GATE_TEMPLATE_ENABLED` | OFF | Forced sub-role gate template injected on plan ingest. | unset → off |

> **Note / honest gap (verified):** `ORCH_WAKE_PACKET_ENABLED` is default-OFF and
> is set **neither in `.env` nor in the live :5002 process environment** — yet
> wake-state packets are observed reaching sessions in our deployment. Therefore
> this flag does **not** gate the packets that actually reach sessions; that path
> (the hook/daemon side) bypasses it. The flag name implies control it does not
> have. **Action:** the audit must decide whether to (a) make this flag actually
> gate the live path, or (b) remove it as a misleading no-op. This is precisely
> the "the flag doesn't do what it says" defect this file exists to end.

## 6. Enforcement & accountability flags — PRIMARY SCRUTINY

These are the flags that can change whether the system enforces its own
discipline. They are the ones most at risk of being an accountability bypass.

| Flag | Default | Effect | In our deployment | Concern |
|---|---|---|---|---|
| `CF_HANDOFF_ENFORCE` (+ `_SESSIONS`) | OFF | Handoff-validation enforcement is opt-in **per session**, via an allowlist. Off → handoffs are not validated. | unset → off for all | Per-session allowlist = enforcement can apply to some sessions and not others. Legitimate only as a staged rollout, not a permanent partial state. |
| `CF_HANDOFF_ACK_PASSIVE` (+ `_SESSIONS`) | OFF | Per-session passive-ack handoff mode. | unset → off | Same allowlist-scoping concern. |
| `CF_STOP_INPROGRESS` (+ `_SESSIONS`) | OFF | Per-session enablement of stop-on-in-progress behavior (falls through to a redis check otherwise). | unset → off | Allowlist-scoping of a stop-discipline behavior. |

**Not flag-gated (good):** completion-evidence enforcement is **hardcoded
always-on** (`orch_schema.py` `_validate_terminal_status_write`) — there is *no*
flag to disable it, verified by live test (a no-evidence completion is rejected).
The earlier plan referencing `CF_COMPLETION_EVIDENCE_REQUIRED` was stale; that
flag does not exist. Supervisor keep-going is likewise hardcoded with no
off-switch (its former flag `CF_SUPERVISOR_DISPATCH` was removed for being a
bypass).

## 7. Tuning parameters (numeric, low-risk)

| Flag | Default | Tunes |
|---|---|---|
| `CF_HANDOFF_PICKUP_POLL_BUDGET` | `5` | Handoff pickup poll attempts. |
| `CF_HANDOFF_VALIDATE_TIMEOUT_S` | `0.2` | Handoff validation timeout. |
| `CF_HANDOFF_SESSION_FLAGS_FILE` / `_TTL_SECS` | unset / cache TTL | Per-session handoff flag file + cache TTL. |
| `ORCH_WORKER_TASK_LIVENESS_TTL_SEC` | built-in | Stall TTL before requeue/wake. |

## 8. Removed / not-a-flag (cleanup)

- `CF_SUPERVISOR_DISPATCH` — **dead remnant**, comment-only; the flag was removed
  2026-06-11 because a default-off supervisor-keep-going toggle was itself a
  bypass. The comment stays as a do-not-reintroduce marker; no live read.
- `ORCH_TASK_NOT_READY` — **not a flag**; it is a log-message label inside an
  `OrchTaskNotReady` exception. Listed here only to explain why it appears in a
  naive `ORCH_*` grep.

---

*Every flag above was adversarially reviewed against the project rule (genuine
operator option, not an accountability/functionality bypass). See the audit
record in the project history.*
