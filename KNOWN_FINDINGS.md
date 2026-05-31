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
| ENG-DEPENDS | CRITICAL | OPEN | **next-ready / surfacing query ignores `depends:` entirely.** Observed 2026-05-31 by taeys-hands (production): ingested a 32-task plan; within seconds the engine surfaced `p0-grok-audit` + `p2-grok-audit` to taeys-hands-grok despite explicit `depends:` on tasks that don't exist yet. Grok honestly refused to fabricate. This breaks the core promise (dependency-ordered surfacing) AND defeats audit-gate rails that rely on depends. Same query path as F33. Fix in ws0-engine-owner. |
| GH#11 | HIGH | OPEN | No wake-on-PID-exit: task blocked_on an external PID never wakes when the PID exits. Hunter sat stuck after codex finished + committed. https://github.com/palios-taey/claude-code-fleet-notify/issues/11 |
| F33 | HIGH | OPEN | Engine surfaces tasks to wrong-owner sessions (auto-claim via unowned/team-matched fallback). |
| F34 | HIGH | OPEN | WAKE_REASON_REQUIRED fires on non-supervisor sessions. |
| ENG-STALE-SUPERSEDED | MEDIUM | OPEN | Observed 2026-05-31 (taeys-hands): engine re-surfaces tasks from a superseded plan (g2-production-grep-doc from taeys-hands-consolidation) after the plan was replaced. Stale tasks from prior ingest are not retired when a new plan supersedes; need archive-on-supersede or stale-task suppression. |

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
