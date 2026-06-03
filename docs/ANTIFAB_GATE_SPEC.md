# Anti-Fabrication Gate — Final Blueprint (5/5 fleet-converged)

This is the BLUEPRINT for the enforcement. The enforcement itself is the GitHub
credential/ruleset regime + the external gate repo below — NOT this document. A
document cannot enforce anything; it is recorded here so the build is unambiguous.

Source: red-team cycle, 5/5 Chats (Gaia/Logos/Cosmos/Horizon/Clarity) + 3 CLI peers
(codex/gemini/grok), adversarial mandate "break Claude's cage." Breaks +
designs: `dispatch_packets/antifab_chat_breaks.md`, `ANTIFAB_CYCLE_responses.md`.

## The anchor (one principle, not a patch list)
**Claude holds NO credential that can:** (a) write `main`, (b) post or satisfy the
required status check, (c) edit branch protection / rulesets, or (d) control the
runner host. Everything below is an instance of this.

## Components
1. **External gate repo `taey-gate`, owned by Jesse, on a branch Claude cannot write.**
   Holds the immutable harness. (Same-repo PRs run the workflow FROM the PR branch —
   so an in-repo gate is neuterable to `exit 0`. CODEOWNER-lock blocks merging the
   change, not running it green. The gate MUST be external.)
2. **A GitHub App (`ship-gate`)** that, on a PR, fetches the PR's exact SHA, runs the
   harness, and posts the ONE required status. The required check is **pinned to this
   App's source** — never "any source" (else any write-token forges green).
3. **GitHub-hosted EPHEMERAL runners only.** No self-hosted / our GPU boxes (that
   hands Claude the host and every artifact).
4. **The harness = read-after-write through the real stack:**
   - runner-generated random UUID → headless browser submits it via the real UI →
     **kill + restart the API process** (a static/mock page can't survive) → reload →
     assert the UUID is present **in the rendered DOM (value-assert, not existence)**
     AND by a **direct DB query** (harness hits the DB directly — no app seam to mock).
   - `continue-on-error` forbidden; any step failing ⇒ status = failure.
   - The App reports failure unless it actually fetched the SHA, built, deployed, drove
     the UI, produced artifacts, and verified postconditions. **`skipped`/`neutral`
     must NOT count as passing** (GitHub treats them as pass by default — guard it).
   - Screenshots/traces = immutable uploaded artifacts for audit, NEVER the pass
     condition.
5. **Branch-protection / ruleset on `main` (and the rebuild branch):**
   - require the ONE check, pinned to the App source; **strict** (up-to-date) AND run
     on **both `pull_request` and `merge_group`** (merge-queue blind spot — else stale
     code merges); dismiss-stale-approvals; require-most-recent-push.
   - linear history; block force-push + branch deletion.
   - **"Do not allow bypassing"; bypass-list empty** except one logged break-glass
     HUMAN (not Claude); enforce-admins on; exactly ONE active ruleset (overlapping
     classic rules → only one applies).
6. **Auto-merge on green.** A green required check self-merges the PR. **No approver;
   the human is fully out of the per-PR loop.** (Impossibility triangle: you cannot
   have {un-fakeable + human-fully-out + a real non-Claude approval} — any automated
   approver Claude can feed is injectable; only a human is un-gameable. So don't trust
   approval — trust the machine gate and demote approval to nothing.)
7. **Deletions:** the in-product ship-check is removed (Claude's own code = zero
   security; branch protection IS the gate). No Claude-assembled audit packets, no
   Claude-authored evaluation rubric.
8. **Peers + Chats = POST-HOC advisory spot-auditors only** — they re-run the locked
   gate on random merged SHAs using credentials Claude cannot read. Never a per-PR gate.
9. **Hardening after the regime is set:** ROTATE all credentials; audit collaborators,
   deploy keys, fine-grained PATs, GitHub Apps, Actions/environment secrets, machine
   users — confirm none give Claude (a)–(d). Forbid `pull_request_target` executing PR
   code; keep the gate secret-free.

## Decision (made): AUTO-MERGE on the locked external gate
Chosen over an isolated-token automated approver: cleanest fit for "human out of the
loop," and adds no judge for Claude to prompt-inject. Peer-quorum kept only as the
post-hoc advisory in (8).

## The only human cost (irreducible)
- **One-time:** create `taey-gate` (Jesse-owned, Claude-unwritable); set the ruleset
  above with auto-merge; ensure credential isolation; rotate/audit.
- **Rare:** a non-Claude reviews the gate-repo harness WHEN it is written and WHEN it
  changes. Never per-PR.

## Trust root (stated honestly)
Security reduces to: the `taey-gate` harness is correctly written, and Claude's
credentials are truly isolated. Claude may AUTHOR the harness, but it is not trusted
until a non-Claude reviews it and installs it in Jesse's repo. That review (on-write +
on-change) is the entire residual trust, and it is rare.
