# Known Findings — Audit Status Ledger

**Purpose:** This file is handed to every reviewer (Family Chats, Grok CLI,
Codex) **before** they audit. It lists what we already know — so audits spend
their effort finding *novel* problems, not re-reporting these. If you are a
reviewer: assume everything below is already tracked. **Report only what is
NOT here.** If you believe a "fixed" item is not actually fixed, say so with
the file:line evidence — that is a novel finding.

Three-register truth required on every new finding: Observed / Inferred / Unknown.

Canonical source of the F-series: `the-conductor/plans/v1_4_0_findings.md`
(4-reviewer audit 2026-05-31: Claude / Grok / Perplexity / Gemini).

---

## Status legend
- `OPEN` — known, not yet fixed
- `FIXED@<sha>` — fixed, verified, commit referenced
- `GATED` — now mechanically blocked by the integrity gate (cannot regress silently)

## Release blockers
| id | severity | status | summary |
|---|---|---|---|
| F1 | CRITICAL | OPEN · GATED(0.0.0.0) | No API auth; `--host 0.0.0.0` binds all interfaces; POST notify shells to tmux. Anyone on :5002 owns the fleet. |
| F2 | CRITICAL | PARTIAL@25243c1 | No packaging/tests/CI. pyproject + entry points landed; CI integrity gate landed @c86a41a; install-check + release-eng remain. |
| F3 | CRITICAL | OPEN | Migration writes negative-epoch priority; API rejects negative priority. Self-contradiction. |
| F4 | CRITICAL | OPEN · GATED | Hardcoded `/path/to/repo` (6 sites) + `ORCH_NEO4J_URI` silent-default to `bolt://127.0.0.1:7689`. Worst: dispatch injects path into worker prompts. |

## No-fallback / fail-loud (Jesse-explicit)
| id | severity | status | summary |
|---|---|---|---|
| F5 | HIGH | OPEN | Silent fallback to Redis direct-push when CLI missing. |
| F6 | HIGH | OPEN · GATED(check=False) | `check=False` on taey-notify exec (2 sites); failure swallowed, task claimed but prompt vanishes. |
| F7 | HIGH | OPEN | Bare except wraps HTTPException(404)→re-emits 500. |
| F8 | HIGH | OPEN · GATED(finally:pass) | `finally: pass` dead blocks — gate finds **9** genuine ones in orch_schema.py. |
| F9 | HIGH | OPEN | create_task/create_question: `result.single()["id"]` TypeError→500 on no rows. |
| F10 | HIGH | OPEN | Repo-wide bare-except / silent-swallow triage (Grok grep). |

## Correctness
| id | severity | status | summary |
|---|---|---|---|
| F11 | HIGH | OPEN | Parent project status clobbered in_progress→active when one child completes (multi-task projects break in flight). |
| F12 | HIGH | OPEN | record_outcome touches only Redis on error; Neo4j task orphans in_progress forever. |
| F13 | HIGH | OPEN | Dispatch writes suffixed owner; ready-queries filter base names → orphaned/invisible tasks. |
| F14 | HIGH | OPEN | PATCH /api/task blindly accepts any status → can revive completed, fracture DAG. |
| F15 | HIGH | OPEN | init_schema() never called at startup; no unique constraint → duplicate nodes. |
| F16 | HIGH | OPEN | Singleton driver ignores config after first call. |
| F17 | HIGH | OPEN | orch-watch clobbers redis notify-keyspace-events instead of unioning. |
| F18 | HIGH | OPEN | Two "ready" definitions disagree on stopped-with-orphaned-stop-reason projects. |
| F19 | HIGH | OPEN | create_question f-string Cypher fragment (future-fragile, breaks plan caching). |

## Engine bugs (found post-audit — NEW vs the F-table)
| id | severity | status | summary |
|---|---|---|---|
| ENG-DEPENDS | CRITICAL | FIXED@96bebfe(undeployed) · LIVE-MITIGATED 2026-05-31 | **next-ready / surfacing query ignores `depends:` entirely.** FIX: 96bebfe unifies ready-defs (grok r2 CLEAR) but is on release/v1.4.0 branch, NOT yet merged+deployed to the live engine. INTERIM MITIGATION applied to the LIVE Neo4j graph: 145 pending tasks with an unmet DEPENDS_ON edge had `blocked_on` set to 'AUTO-GATED(depends-unmet): interim until v1.4.0 depends-enforcement deploys' — the live next-ready excludes blocked_on (orch_schema.py:848,1023), so premature peer-wakes (weaver/taeys-hands/hunter) stop. Greppable marker; v1.4.0 deploy must clear these (the deployed engine enforces depends natively). **original:** ingested 32-task plan surfaced grok-audit tasks despite unmet depends; grok refused to fabricate. Observed 2026-05-31 by taeys-hands (production): ingested a 32-task plan; within seconds the engine surfaced `p0-grok-audit` + `p2-grok-audit` to taeys-hands-grok despite explicit `depends:` on tasks that don't exist yet. Grok honestly refused to fabricate. This breaks the core promise (dependency-ordered surfacing) AND defeats audit-gate rails that rely on depends. Same query path as F33. Fix in ws0-engine-owner. |
| GH#12-notify | HIGH | OPEN | claude-code-fleet-notify#12 (distinct from orchestrator GH#12 auth bug): hunter session (CLAUDE_CONFIG_DIR=~/.claude-hunter, project hunter-sprint-08) stopped at a turn boundary ~2026-05-31 20:15Z with registered user_stop_conditions UNMET; engine did NOT fire WAKE_WITH_QUEUE, session got no wake + idled. Stop-discipline engine let a stop happen that the conditions should have blocked (or failed to re-wake with the ready queue). Needs triage in the stop-engine wake path — likely related to F34/wake-gating or the depends-not-enforced surfacing. https://github.com/palios-taey/claude-code-fleet-notify/issues/12 |
| GH#11 | HIGH | OPEN | No wake-on-PID-exit: task blocked_on an external PID never wakes when the PID exits. Hunter sat stuck after codex finished + committed. https://github.com/palios-taey/claude-code-fleet-notify/issues/11 |
| F33 | HIGH | OPEN | Engine surfaces tasks to wrong-owner sessions (auto-claim via unowned/team-matched fallback). |
| F34 | HIGH | OPEN | WAKE_REASON_REQUIRED fires on non-supervisor sessions. |
| ENG-PROBE-POLLUTION | LOW | OPEN | Observed 2026-05-31: production-validation probe projects (ws0-prod-*, ws0-reblock-*) created with `owner=conductor` + zero-dep surface in the supervisor's real next-ready queue and fire WAKEs at conductor. Validation-by-production is correct, but probes must be isolated: dedicated owner (e.g. `__probe__`) or a probe project flag excluded from next-ready, and cleaned up (DETACH DELETE) when the validating task completes. Currently they accumulate + wake the supervisor. |
| PROC-GROK-VERDICT-LOSS | MEDIUM | OPEN | Observed 2026-05-31: grok-cli ran the ws0 audit (2m7s turn) but persisted NOTHING — no audit_logs file, no commit, no taey-notify back to conductor. grok-cli is an alt-screen TUI with no scrollback retention, so a verdict produced only in-pane evaporates. **Process fix:** grok audit packets must require the verdict be WRITTEN TO FILE + git-committed + notified as the FIRST actions (the artifact IS the deliverable), and the audit task cannot close without the committed file existing. A pane-only verdict = audit not done. |
| ENG-STALE-SUPERSEDED | MEDIUM | OPEN | Observed 2026-05-31 (taeys-hands): engine re-surfaces tasks from a superseded plan (g2-production-grep-doc from taeys-hands-consolidation) after the plan was replaced. Stale tasks from prior ingest are not retired when a new plan supersedes; need archive-on-supersede or stale-task suppression. |
| UX-STATUS-NOSYNC | MEDIUM | OPEN | hunter 2026-05-31 production pressure-test: completed work does NOT auto-sync to orchestrator task status — had to manually `taey-task update <id> completed` for 8 already-finished tasks, else `taey-plan next` kept returning a done task as ready. The API has the manual path but autonomous sessions need auto-completion (or a completion signal from the worker dispatch path). UX gap for unattended operation. Related to ws0-done-evidence (completion contract) — fold the auto-sync there. |
| UX-CLI-FLAG-DRIFT | LOW | OPEN | hunter 2026-05-31: `taey-task update <id> --status completed` fails (CLI expects POSITIONAL: `taey-task update <id> {completed|failed|in_progress|interrupted}`). Either accept `--status` as an alias or document the positional form in --help + README. Minor but trips first-time/automation use. |
| ENG-NEO4J-AUTH-SCHEME | HIGH | FIXED@6c0bf3b(live lib) + 1ef0460(notify sanitizer) + f9665d3(src) — 2026-05-31, taeys-hands-verified in hook env | **TRUE ROOT CAUSE (proven by clean before/after repro, NOT the original diagnosis):** the live DBs are intentionally NO-AUTH; the bug was config.py's `break # first found wins` .env loader. A subprocess whose cwd has a PARTIAL .env (e.g. /path/to/repo, which lacks ORCH_NEO4J_URI) read that file, break'd, never reached the package-root .env with ORCH_NEO4J_URI=...7689, defaulted to localhost:7687, where auth=None → neo4j AuthError "missing key scheme". FIX: remove the break so candidates MERGE (6c0bf3b live lib + f9665d3 src). NO fail-loud-on-creds (would break the no-auth DBs — corrected mid-investigation). Symptom (scary text → Anthropic API classifier break) fixed by hooks/_shared.py `_sanitize_engine_error` (notify 1ef0460). VERIFIED: taeys-hands hook env now HOOK_URI=...7689, HOOK_CONNECT=1, can operate. Repro: UNFIXED+partial-cwd-.env→localhost:7687→AuthError; FIXED→...7689→OK. NOTE earlier rows in this file mis-diagnosed as empty-creds/fail-loud — superseded by this. |
| ENG-NEO4J-AUTH-SCHEME-old | HIGH | OPEN | Stop-hook outcome-recording crashes with neo4j AuthError "Unsupported authentication token, missing key `scheme`" on task completion (taeys-hands, p0-systemd-launch). **Observed:** live config.py:185-191 branches `if neo4j_user and neo4j_pass: auth=(user,pass) else: auth=None`; that path RETURNs 1 fine from conductor's interactive env; neo4j driver **6.0.2** uniform across system + taeys-env-sys (CORRECTED: earlier draft of this row said 5.28.2 — that was wrong, direct check shows 6.0.2) — so NOT a tuple-vs-dict bug and NOT a version split (both taeys-hands hypotheses refuted by direct check). Task DID complete; crash is post-completion in the outcome-recording hook only. **CONFIRMED root cause (taeys-hands in-hook probe 2026-05-31):** in the Stop-hook subprocess env, `USER_LEN 0 PASS_LEN 0` + URI defaulted to `bolt://localhost:7687` (wrong port; Neo4j here is 7689). Creds resolve EMPTY because the hook subprocess inherits no shell-sourced env. config.py SILENTLY takes the `auth=None` branch (the exact silent-fallback pattern Jesse banned) instead of raising — then the unauthenticated/misconfigured connection fails. **Fix (ws1-config):** config must FAIL LOUD when creds are unset (raise naming the env vars), use explicit basic_auth(), and the URI default to localhost:7687 must also fail-loud or source machine env rather than silently pointing at the wrong port. Plus: the hook invocation must source the orchestrator env. taeys-hands holding bug-lock until this lands. |

## Docs / cleanup
| id | severity | status | summary |
|---|---|---|---|
| F20-F22 | HIGH | OPEN | README says v1.2.1; CHANGELOG missing v1.3.x; docstring refs private shim; SCHEMA.md describes fields that don't exist + wrong priority-direction statement. |
| F23-F31 | MED/LOW | OPEN | forced_continuation_count dead field; Cypher dup; PRODUCT_OWNER_MAP Mira-only; sessions list hardcoded in 2 places; `lib`→fleet_orchestrator (DONE@25243c1); config.py docstring untruths; internal notes at repo root; unverifiable SLAs; no CONTRIBUTING. |
| F32 | LOW | OPEN | load-md re-ingest re-fires zero-dep wakes outside 600s dedup window. |

## Integrity-gate baseline (mechanical, 2026-05-31 @c86a41a)
`tools/lint_no_silent_fallbacks.py --all` → **19 findings**: 9 finally:pass · 6 hardcoded /path/to/repo · 2 check=False · 1 internal-IP default · 1 0.0.0.0 bind. These are now un-mergeable until fixed (red CI blocks merge).

---

*Reviewers: if it's on this list, we know. Find what isn't.*
| Q4-PRESENCE | LOW | OPEN→ws2 | Gaia v5: evidence gate validates PRESENCE not CONTENT — placeholder 'x'/'y' pass. Optional hardening: format-assertion at orch_schema.py:138 (sha-shape + min obs length). |
| Q4-NULLOMIT | LOW | OPEN→ws2 | Cosmos v5: JSON null-vs-omit edge in PATCH → 500 fail-closed (no threat, just ugly). Distinguish null from omitted. |
| N2-NOTNULL | MEDIUM | OPEN→ws2 | get_agent_tasks orch_schema.py:1502 un-coalesced status filter (last coalesce site). Structural fix: NOT-NULL constraint on OrchTask.status at init_schema → removes need for scattered coalesce. |
| V1-SEND-BUG | HIGH (fleet-infra) | OPEN (taeys-hands GH issue filed) | taeys-hands V1 consultation dispatcher: ChatGPT/Horizon send fails to fire after composer-prep (mode_select/send path) — blocked 3+ ws0 dispatch rounds + multiple Sprint-7/8 dispatches. Also Perplexity 3x send_failure, Gemini pre-register crash. NOT an orchestrator bug — it's in taeys-hands consultation_v2 V1 codepath. taeys-hands owns RCA (Jesse-directed). Cross-referenced here because it blocks Family-audit dispatch reliability. |
