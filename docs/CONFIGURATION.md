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
- The completion-evidence check first enforces shape/plausibility, then records a separate truth marker. `completion_evidence_verification.status=VERIFIED` only when either: `gh api` confirms the GitHub commit exists in the explicit `evidence.repo`, that repo is present in `ORCH_COMPLETION_ALLOWED_REPOS`, and the repo's verification profile passed; or true no-runtime/non-gated non-production research/prototype evidence includes `supervisor_verification` with `mode` of `research` or `prototype`, a distinct `verifier`, and an `observation` of what that supervisor checked. Gated repos require every required independent gate context for that exact `commit_sha` from trusted GitHub actors/apps. Gateless repos require the commit to be reachable from the repo default branch. Commit evidence without `repo` is rejected instead of guessing a fallback repo, and `supervisor_verification` never rescues commit-backed evidence from the GitHub gate path. Before `supervisor_verification` can verify no-`commit_sha` evidence, the configured runtime repo classifies the task first, and `evidence.repo` is only context when no runtime repo is set; if the selected repo is allowlisted as `:gated`, completed writes without `commit_sha` are rejected even when `supervisor_verification` is present so caller-supplied verifier names cannot bypass open-PR and required-gate checks. Gateless/local/non-repo completions without `commit_sha` stay completed but explicitly unverified. Off-allowlist commit repos stay `UNVERIFIED`, not rejected. The token is still the control for *who can reach the port*.
- The **public read-only** surface (`scripts/orch-public`, `:5005`) is separate,
  GET-only, fail-closed (shows nothing unless a session is explicitly allowlisted),
  and scrubs secrets/operator paths. It is the only surface intended for exposure.

## 1. Connections, credentials, storage

| Flag | Default | Purpose |
|---|---|---|
| `ORCH_HOST` | `127.0.0.1` | Bind interface for the mutable API/dashboard (see posture above). |
| `ORCH_PORT` | `5002` | Mutable API/dashboard port. |
| `ORCH_API_BASE` / `ORCH_DASHBOARD_URL` | `http://127.0.0.1:5002` | Base URL the CLIs call. |
| `ORCH_COMPLETION_GITHUB_REPO` / `GITHUB_REPOSITORY` | inferred from `gh api repos/:owner/:repo` | Runtime GitHub repo used by PR-reference reconciliation when a task names a bare PR number, and used to classify no-`commit_sha` completed writes before any caller-supplied `evidence.repo`. If the runtime repo is allowlisted as `:gated`, no-`commit_sha` completed writes are rejected even when `supervisor_verification` is present; commit evidence must include explicit `completion_evidence.repo` and is never inferred. |
| `ORCH_COMPLETION_ALLOWED_REPOS` | unset | Comma-separated GitHub repo verification profiles for completed-task verification. Entries are `OWNER/REPO` or `OWNER/REPO:gated` for the default required-check profile, and `OWNER/REPO:gateless` for repos that lack PR/check machinery and verify by commit reachability from the default branch. When unset or empty, startup logs a warning and all commit-based completions stay `UNVERIFIED` until configured. A caller-supplied repo outside this list is `UNVERIFIED`, even if the commit and statuses exist. |
| `ORCH_COMPLETION_REQUIRED_CHECKS` / `ORCH_PRE_MERGE_REQUIRED_CHECKS` | `r5-audit-gate,ship-gate-acceptance` | Comma-separated GitHub check/status contexts required before a completed task's commit evidence can be marked `VERIFIED` for gated repos without a repo-specific override. |
| `ORCH_COMPLETION_REPO_CHECKS` | unset | Semicolon-separated per-repo required-check overrides for gated completion evidence. Each entry is `OWNER/REPO=check-a,check-b`; for example `palios-taey/taeys-hands=r5-audit-gate,consultation-v2-integrity`. Repos not listed here use `ORCH_COMPLETION_REQUIRED_CHECKS`. |
| `ORCH_COMPLETION_TRUSTED_CHECK_RUN_APPS` | `github-actions` | Comma-separated GitHub check-run app slugs trusted for required check-run contexts. Matching name + success is insufficient if the app is not trusted. |
| `ORCH_COMPLETION_TRUSTED_STATUS_CREATORS` | `github-actions[bot]` | Comma-separated GitHub commit-status creator logins trusted for required status contexts. Matching context + success is insufficient if the creator is not trusted. |
| `ORCH_NEO4J_URI` / `ORCH_NEO4J_DB` | (required) | Neo4j connection. **No auth** — the orchestrator connects with no credentials and does not support internal-service auth (run Neo4j with `NEO4J_AUTH=none`). Internal-service credentials are intentionally unsupported: the network is the boundary, and a credential dimension in the driver config caused a recurring outage. |
| `ORCH_KB_NEO4J_URI` | unset | Optional, separate no-auth Neo4j URI for wake-packet Knowledge Base injection. Must be set together with `ORCH_KB_MAP_PATH`; if one is set without the other, packet assembly fails loud. |
| `ORCH_KB_MAP_PATH` | unset | Optional JSON selector map for Knowledge Base injection. Must be set together with `ORCH_KB_NEO4J_URI`. Selector-matched tasks fail loud when the configured KB is unreachable or a mapped `stable_key` has no `CURRENT_REVISION`; tasks that match no selector receive no KB section. |
| `ORCH_REDIS_HOST` / `ORCH_REDIS_PORT` | (required) | Orchestrator Redis connection for API/dashboard state, locks, receipts, and orchestrator-owned runtime data. The core `OrchConfig` has no built-in default; set these explicitly, as in `.env.example`. |
| `REDIS_HOST` / `REDIS_PORT` | `127.0.0.1` / `6379` | Fleet-notify/session-state Redis used by dispatch `current_task`, Stop-hook `idle`/outcome state, session pause, and worker liveness. Set this to the same instance as `ORCH_REDIS_*` for normal local installs; split only for deliberate divergence testing. |
| `ORCH_REDIS_SENTINELS` / `ORCH_REDIS_SENTINEL_MASTER` | `""` / `orch-master` | Optional Redis Sentinel HA. |
| `ORCH_DATA_DIR` / `ORCH_STATE_DIR` | platform dirs | Data / state directories. |
| `ORCH_DOTENV` | auto-discover | Explicit dotenv path. Set to `empty` to suppress cwd/repo `.env` auto-loading for defaults-contract tests; normal operator runs can leave auto-discovery enabled. |
| `ORCH_AGENT_TEST_INFRA` | unset | Test-only isolation marker. Local agent mutation tests must run through `scripts/orch-acceptance-isolated`, which sets this to `throwaway` after assigning non-live loopback Redis/Neo4j ports and rejects non-loopback store hosts. GitHub Actions service-container runs set `ephemeral-ci`. It is not a server auth or runtime security control. |
| `ORCH_LIVE_REDIS_GUARD_HOST` / `ORCH_LIVE_REDIS_GUARD_PORT` | `127.0.0.1` / `6379` | Test-only live Redis snapshot target used by `scripts/orch-acceptance-isolated` to fail if acceptance-attributable keys change on the operator/live Redis while the isolated throwaway stores run. Override only when the live Redis guard itself is intentionally pointed somewhere else. |
| `ORCH_NOTIFY_CLI` / `ORCH_NOTIFY_LIB_ROOT` | `taey-notify` / auto | Notification CLI name + lib root. |
| `ORCH_HUMAN_REVIEW_ALERT_TARGET` | unset | Optional session target for human-review dashboard delivery-failure alerts. When unset, delivery failure still fails closed with API/UI recovery instructions but no session notify is attempted. |
| `ORCH_PEER_RESPAWN_SCRIPT` | required | Absolute peer-respawn executable used by `orch-cron` project-trigger delivery prechecks when a target tmux session is absent. Set this to the operator-owned peer-respawn script path for the local install; this is not an auth boundary. |
| `ACCOUNTABILITY_LEDGER_PATH` | platform state dir | Location of the hash-chained accountability ledger. The ledger module is explicit that it is **tamper-evident, not tamper-proof**; an ephemeral path silently loses the record — point it at durable, operator-owned storage. |
| `ACCOUNTABILITY_CI_AUDIT_PATH` | platform state dir | Location of the separate hash-chained CI merge audit ledger (`ci-audit.jsonl`). Use durable, operator-owned storage; this chain records completed CONTROL merges only when real gate results and durations are supplied. |
| `ORCH_CAUSAL_LEDGER_PATH` | `ORCH_DATA_DIR/provenance/causal-events.jsonl` | Optional override for the append-only causal dispatch/outcome ledger. Use durable, operator-owned storage; a temporary path loses the provenance row-chain across restarts. |
| `ORCH_WORLD_MANIFEST_PATH` | ORCH_DATA_DIR provenance data file | Optional output path for the published World Manifest v0 JSON. Dispatch publishes the manifest before rendering the wake-packet proof capsule. |
| `ORCH_WORLD_SYSTEM_MAP_PATH` | repo-local system connection map | Optional World Manifest seed override for the system connection map. A missing or unreadable file becomes an explicit `Unknown` root. |
| `ORCH_WORLD_KNOWLEDGE_INDEX_PATH` | sibling/home Taey Presence production index when present | Optional World Manifest seed override for the Taey Presence knowledge index. Only `status=production` capabilities are included; a missing index becomes explicit `Unknown`, not invented data. |
| `ORCH_PROVENANCE_WITNESS_ENABLED` | `off` | Explicit gate for external checkpoint anchoring. Invoking anchoring while unset/off fails loud. |
| `ORCH_PROVENANCE_WITNESS_PRINCIPAL` | unset | External witness principal selected by the operator. The jsonl adapter refuses to anchor without it. |
| `ORCH_PROVENANCE_WITNESS_ADAPTER` | `jsonl` | Current external witness adapter name. It writes roots and counts only after the explicit gate is enabled. |
| `ORCH_PROVENANCE_WITNESS_PATH` | unset | Required path for the jsonl witness object stream. Missing path fails loud when anchoring is invoked. |

## 2. Security & access

| Flag | Default | Purpose |
|---|---|---|
| `ORCH_AUTH_TOKEN` | unset (tokenless mutable API) | Bearer token gating mutable methods — see posture above. |
| `ORCH_ALLOW_UNAUTH_NON_LOOPBACK` | unset (fail closed) | Explicit acknowledgement that a non-loopback mutable API bind without `ORCH_AUTH_TOKEN` is intentional. This is not auth; it only permits startup in the trusted single-user LAN posture. |
| `ORCH_REF_ALLOWED_ROOT` | unset | Explicit filesystem sandbox root(s) for `[ref:]` reads; reads outside allowed roots are refused. |
| `ORCH_SESSION_IDS` | `""` | Comma-separated registered control-principal session names. Registration is exact for supervisor plan/root access: if `family-codex` is listed, that Codex session is the supervisor and the bare `family` Claude plus `family-gemini`, `family-grok`, and `family-claude` are its workers. If bare `family` is listed, legacy topology remains unchanged and the suffixed sessions are its workers. Configured topology wins over stale Redis parent state, mixed registration of both `family` and `family-codex` is invalid deployment input, and worker spellings never gain supervisor plan access. The dashboard shows only registered control principals; notify/wake endpoints also admit their derived workers. When **empty**, the target filter is OFF (the API's real boundary is `ORCH_AUTH_TOKEN`/loopback). Plan ingest fails loud when registration is absent. |
| `ORCH_BADGE_FALLBACK_SUPERVISOR` | unset | Optional explicit dashboard supervisor bucket for `/api/supervisors/badges` work whose effective supervisor is not itself visible in `/api/sessions`. Honored only when it resolves to a configured dashboard supervisor; unset drops non-dashboard work from the badge view. |
| `ORCH_OUTWARD_SESSION` | unset | Optional claimed session id. The GitHub broker authorizes the live peer session from SO_PEERCRED pid → TTY, not this string. |
| `TAEY_SESSION` | unset | Optional worker session id alias consulted after `ORCH_OUTWARD_SESSION` for outward GitHub authorization. |
| `ORCH_GITHUB_BROKER_SOCKET` | unset | Unix socket path for the GitHub outward **exec** channel. Worker `gh` clients send argv here (`op=exec` only); they never receive `GH_TOKEN`. Mint/revoke on this socket is denied. Required for `scripts/gh-outward`. |
| `ORCH_GITHUB_BROKER_CONTROL_SOCKET` | unset | Unix socket path for the authenticated **control** channel, mode `0660` (not `0600`: owner-only cannot be opened by uid-mira orch even with a supplementary group). `bind_current_task` mints and `session_unbind_current_task` revokes here. Not exported to workers. Unset: mint/revoke no-op and GitHub writes fail-closed. |
| `ORCH_GITHUB_BROKER_CONTROL_CGROUP` | `/system.slice/fleet-orchestrator-api.service` | Absolute kernel cgroup path of the system API unit allowed to mint/revoke. Compared by equality only (no suffix/subtree match). Same-UID workers in `user.slice`/`tmux-spawn` cannot join this cgroup. |
| `ORCH_GITHUB_BROKER_EXEC_GROUP` | `github-workers` | Unix group assigned to the exec socket and its parent (`chown` `st_gid`). Missing group or chown failure fail-closes the broker. SupplementaryGroups does not set `st_gid`. |
| `ORCH_GITHUB_BROKER_CONTROL_GROUP` | `github-control` | Unix group assigned to the control socket and its parent. Missing group or chown failure fail-closes the broker. |
| `ORCH_GITHUB_BROKER_CONTROL_SESSIONS` | `ORCH_SESSION_IDS` | Optional supervisor TTY sessions that may mint if they can open the control socket. The production caller is the system API cgroup. |
| `ORCH_GITHUB_BROKER_CONTROL_UIDS` | broker uid | Optional extra uid map. Control mint is authorized by supervisor TTY session, not by sharing the login uid. |
| `ORCH_GITHUB_BROKER_WORKER_UIDS` | unset (any uid may exec) | Comma/space-separated uids allowed to `op=exec` on the worker socket. Empty means the group-writable exec socket does not uid-filter (CONTROL deploy sets distinct worker uids). |
| `ORCH_GITHUB_BROKER_INNER` | unset | Absolute path of the inner `gh` binary, visible only to the broker principal (`scripts/github-brokerd`). Workers must not have this path. |
| `ORCH_GITHUB_BROKER_ALLOW_LIVE` | unset | If `1`, `scripts/install-github-broker` may write a live system prefix. Unset refuses `/usr`, `/usr/local`, and `~/.local`. This PR does not set it; CONTROL deploy only. |
| `ORCH_SESSION_ROOTS` | `""` | Maps sessions → repo roots for context; these roots are also auto-derived as allowed `[ref:]` roots. |
| `ORCH_PEER_WORKTREE_ROOTS` | `~/.peer-worktrees` | Path list, separated by `:` or commas, whose clean branch checkouts may be detached by the CONTROL pre-merge gate and stale-branch cleanup before deleting merged PR branches. Dirty peer worktrees are preserved; non-peer checkouts fail closed. |
| `ORCH_RULES_ROOT` | `""` | Directory of rule files surfaced in context. |
| `ORCH_MEMORY_ROOT` | `""` | Directory of local memory files surfaced in wake-packet Memory. |
| `ORCH_IDENTITY_ROOT` | `""` | Optional trusted identity directory for wake packets. Companion sessions load a bounded companion runtime core plus hash-bound `${ORCH_IDENTITY_ROOT}/...` source pointers from this root; engineering sessions use the built-in lean role core. Supported layouts: Markdown files under companion or taey subdirectories, root-level companion/taey/IDENTITY/PERSONALITY Markdown files, or the corpus identity plus layer_1 layout. |
| `ORCH_COMPANION_SESSIONS` | `taey,companion` | Comma-separated session ids that should receive companion identity instead of engineering identity. CLI peer suffixes (`-codex`, `-gemini`, `-grok`) remain engineering. |
| `ORCH_ENABLEMENT_SESSIONS` | `""` | Comma-separated non-peer session ids that should receive the enablement role core instead of the engineering fallback. Companion identity still wins first, and CLI peer suffixes remain engineering. |

For an existing deployment moving from bare supervisors to `*-codex` controls, stop API writers and run `python -m fleet_orchestrator.control_principal_migration` with the new `ORCH_SESSION_IDS` to inspect the exact project/task/ref counts. Re-run with `--apply` to commit the migration in one Neo4j transaction. The command rewrites `OrchProject.supervisor`, exact bare-supervisor `OrchTask.owner`, and `OrchSupervisor` refs only; it does not change `dispatched_to`, Redis `current_task`, or historical audit fields. Restart services only after the apply result reports zero remaining old records.

## 3. Public read-only dashboard (display only — cannot mutate or change enforcement)

`ORCH_PUBLIC_SHOW_SESSIONS` (fail-closed allowlist), `ORCH_PUBLIC_HIDE_SESSIONS`,
`ORCH_PUBLIC_HIDE_PROJECT_IDS`, `ORCH_DASHBOARD_SESSIONS`.

## 4. Gate / ownership

`ORCH_GATE_OWNERS` (generic stage keys → operator sessions), `ORCH_GATE_REPO`,
`ORCH_PRODUCT_OWNER_MAP` (validated JSON; rejects empty keys/values),
`ORCH_SHIP_GATES` (fail-closed — no gates ⇒ not shippable; cannot be emptied to
force a pass), `ORCH_PRE_MERGE_REQUIRED_CHECKS` (consumed by the pre-merge gate
and as the fallback completed-task verification context list),
`ORCH_COMPLETION_GITHUB_REPO`, `ORCH_COMPLETION_ALLOWED_REPOS`,
`ORCH_COMPLETION_REQUIRED_CHECKS`, `ORCH_COMPLETION_REPO_CHECKS`,
`ORCH_COMPLETION_TRUSTED_CHECK_RUN_APPS`,
`ORCH_COMPLETION_TRUSTED_STATUS_CREATORS`, `ORCH_CAREERS_CANONICAL_STATUS_PATHS`
(comma-separated relative canonical careers-state paths checked for untracked
drift by loop-proof completion verification; default `foundations/careers`).

## 5. Feature toggles

| Flag | Default | Enables |
|---|---|---|
| `ORCH_AWAIT_SIGNAL_GATES` | **ON** | Stop only on an exact `AWAIT:<kind>:<detail>` marker (prose waits rejected). OFF is *stricter*. The supported kinds are `human-review`, `family-consent`, and `external-signal`; use `AWAIT:external-signal:<id>` for cross-session or external executor waits. |
| `ORCH_WORKER_TASK_LIVENESS` / `ORCH_WORKER_TASK_LIVENESS_TTL_SEC` | **ON** / unset | Advisory worker stall-detection / heartbeat. Structured `AWAIT:<kind>:<detail>` waits exempt liveness expiry; free-text `blocked_on` is informational only and can be returned to pending with a teaching wake. Matching-current-task `last_tool_activity` and age-bounded `tool_running_at` producer stamps refresh liveness; stale `tool_running` alone does not. |
| `ORCH_CHAT_ENABLED` | **ON** | Dashboard chat-to-session box. Chat is an injection vector; keep the mutable API loopback-only or protect non-loopback trusted-LAN deployments with `ORCH_AUTH_TOKEN`. Set `0`/`false` only to intentionally hide the chat route. |
| `ORCH_WAKE_PACKET_ENDPOINT_ENABLED` (`ORCH_WAKE_PACKET_ENABLED` deprecated alias) | **ON** | Gates **only** the `/api/sessions/{id}/wake-packet` context endpoint. Session *waking* (`send_wake`) runs regardless. The old `ORCH_WAKE_PACKET_ENABLED` name is still read as a non-breaking alias but should not be used in new configs. |
| `ORCH_DECISION_RECEIPTS_ENABLED` | **ON** | Best-effort decision-receipt explainability records. They are written to Redis stream `orch:streams:decision_receipts` and surfaced by `taey-receipts list`; nothing blocks on them. |
| `ORCH_LOOPS_ENABLED` | **ON** | The additive signal/clock/task-state loop API routes. When disabled, loop operations return `ok:false`, `enabled:false`, and `reason:"loops disabled"`; core stop/dispatch integration is deliberately not wired in this phase. |
| `ORCH_GATE_TEMPLATE_ENABLED` | **ON** | Applies the forced sub-role gate template when a plan explicitly requests that template. |
| `ORCH_NOTIFY_DAEMON_WATCHDOG` | **ON** | Enables the `orch-watch` notify-daemon watchdog: delegated service liveness and heartbeat freshness with direct out-of-band alerts, plus stuck-inbox delivery SLO checks that first reconcile usage-limit idle state and then alert the conductor inbox once per stuck message if still unresolved. |

## 6. Handoff / stop discipline

| Flag | Default | Effect |
|---|---|---|
| `CF_HANDOFF_PICKUP_POLL_BUDGET` | `5` | Handoff helper pickup polling. The current stop-decision path does not call handoff validation. |
| `ORCH_NOTIFY_DAEMON_WATCH_INTERVAL_SEC` | `30` | `orch-watch` notify-daemon watchdog cadence, in seconds. |
| `ORCH_NOTIFY_DAEMON_HEARTBEAT_MAX_AGE_SEC` | `15` | Maximum accepted age for `taey:_notify_daemon:heartbeat` before `orch-watch` treats the notify daemon as wedged. |
| `ORCH_NOTIFY_DAEMON_STUCK_INBOX_MAX_AGE_SEC` | `600` | Maximum accepted age for a queued `${NOTIFY_KEY_PREFIX}:*:inbox` message before `orch-watch` raises the stuck-delivery SLO alert. |
| `ORCH_COMPOSER_OCCUPANCY_MAX_AGE_SEC` | `300` | Maximum accepted age, in seconds, for a worker's `${NOTIFY_KEY_PREFIX}:<session>:composer_occupancy` stamp before `orch-watch` ignores it for wedged-composer activation-failure alerts. |
| `ORCH_WEDGED_COMPOSER_STABILITY_WINDOW_SEC` | `120` | Minimum TTL for the wedged-composer candidate fingerprint, clamped to at least `120` seconds so the two-sweep stability check is independent of `ORCH_COMPOSER_OCCUPANCY_MAX_AGE_SEC`. |
| `ORCH_WEDGED_COMPOSER_REARM_SEC` | `1800` | Minimum re-arm window, in seconds, before `orch-watch` can re-alert on the same wedged-composer activation-failure transition while the failed-activation record remains. Transient empty composer reads do not clear this throttle. |
| `ORCH_NOTIFY_ROUTER_SERVICE` | `conductor-notify-router` | `systemctl --user is-active` service name checked by the notify-daemon watchdog. |
| `ORCH_NOTIFY_DAEMON_ALERT_TARGET` | `conductor` | tmux session that receives direct out-of-band service and heartbeat liveness watchdog alerts. |

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
`CLAUDE_SETTINGS_PATH`, `CODEX_HOOKS_PATH`, `GEMINI_SETTINGS_PATH`,
`GROK_HOOKS_PATH`, `TAEYS_HANDS_ROOT`, `TAEY_NODE_ID`,
`NOTIFY_DAEMON_PIDFILE`, `NOTIFY_KEY_PREFIX`.

## 8. Test-only (never read by the running server)

`ORCH_AGENT_TEST_INFRA` (`throwaway` for `scripts/orch-acceptance-isolated`,
`ephemeral-ci` for GitHub Actions service containers), `ORCH_TEST_NAMESPACE`
(safety guard — acceptance tests refuse to run against a production Neo4j
namespace), `EASY_SETUP_ACCEPTANCE_INJECT_FAIL` /
`REF_ACCEPTANCE_INJECT_FAIL` (negative-control fault injectors — they make tests
*fail*, cannot fake a green), `PROBE_CHECK_MODE` / `PROBE_HEAD_SHA` (pre-merge gate
test scaffolding), `ORCH_NOTIFY_CLI` (also overridable in tests), `PATH`.

---

**Not hardcoded-toggleable (no off-switch — verified):** the completion-evidence
gate and supervisor keep-going are enforced unconditionally in code; no env flag
disables them, and no DB write path bypasses the evidence gate. Completed-task
truth is surfaced separately as VERIFIED/UNVERIFIED; the verifier config changes
which GitHub repo/check contexts can prove a commit, not whether evidence is
required.
