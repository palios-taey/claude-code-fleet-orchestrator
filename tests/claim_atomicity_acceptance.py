"""Ship-gate e2e — the task CLAIM path is atomic under real concurrency (grok ws2-state F11).

grok's F11 worst-case was "two workers both read pending, both SET owner -> clobber". The claim
is a SINGLE Cypher statement (MATCH (t) WHERE status='pending' SET status='in_progress',owner)
-- Neo4j evaluates WHERE+SET atomically, so of N concurrent claimers EXACTLY ONE wins and the
rest see status!='pending' and raise OrchTaskNotReady. This proves there is no read-then-write
clobber window in the claim path (the path that actually races in normal operation).

Env: ORCH_NEO4J_URI, ORCH_NEO4J_DB, ORCH_REDIS_HOST/PORT, ORCH_DASHBOARD_URL, ORCH_NOTIFY_LIB_ROOT.
"""
from __future__ import annotations

import concurrent.futures
import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.orch_schema import create_project, create_phase, create_task, get_neo4j_driver  # noqa: E402
from lib.dispatch import _claim_ready_orch_task, OrchTaskNotReady  # noqa: E402
from lib.config import OrchConfig  # noqa: E402

CFG = OrchConfig()
_PFX = f"claimatomic-{uuid.uuid4().hex[:8]}"
_FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: str = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        _FAILURES.append(label)


def main() -> int:
    drv = get_neo4j_driver(CFG)

    def status_owner(tid):
        with drv.session(database=CFG.neo4j_db) as s:
            r = s.run(
                """
                MATCH (t:OrchTask {id:$i})
                RETURN t.status AS st,
                       t.owner AS o,
                       t.dispatched_to AS d,
                       t._claim_lock AS claim_lock
                """,
                i=tid,
            ).single()
            return (r["st"], r["o"], r["d"], r["claim_lock"]) if r else (None, None, None, None)

    with drv.session(database=CFG.neo4j_db) as s:
        s.run("MATCH (n) WHERE n.id STARTS WITH $p DETACH DELETE n", p=_PFX)
    try:
        create_project(project_id=_PFX, name=_PFX, config=CFG)
        create_phase(project_id=_PFX, phase_id=f"{_PFX}::ph", name="ph", config=CFG)
        N = 8
        for trial in range(5):
            T = f"{_PFX}::t{trial}"
            create_task(phase_id=f"{_PFX}::ph", task_id=T, description="x",
                        owner="", wake_owner_if_ready=False, config=CFG)
            workers = [f"{_PFX}-w{i}-codex" for i in range(N)]

            def claim(w):
                try:
                    _claim_ready_orch_task(T, w)
                    return ("won", w)
                except OrchTaskNotReady:
                    return ("lost", w)
                except Exception as e:  # noqa: BLE001
                    return ("error", repr(e))

            with concurrent.futures.ThreadPoolExecutor(max_workers=N) as ex:
                results = list(ex.map(claim, workers))
            winners = [w for (r, w) in results if r == "won"]
            errors = [w for (r, w) in results if r == "error"]
            st, owner, disp, claim_lock = status_owner(T)
            _check(f"trial {trial}: EXACTLY ONE concurrent claimer wins ({len(winners)})", len(winners) == 1, str(results))
            _check(f"trial {trial}: no unexpected errors", not errors, str(errors))
            _check(f"trial {trial}: task ends in_progress owned by the single winner",
                   st == "in_progress" and disp == winners[0] if winners else False, f"st={st} disp={disp} winners={winners}")
            _check(f"trial {trial}: claim lock is non-growing boolean true",
                   claim_lock is True, f"_claim_lock={claim_lock!r}")
    finally:
        with drv.session(database=CFG.neo4j_db) as s:
            s.run("MATCH (n) WHERE n.id STARTS WITH $p DETACH DELETE n", p=_PFX)
    if _FAILURES:
        print(f"\nFAIL — {len(_FAILURES)}: {_FAILURES}")
        return 1
    print("\nPASS — claim is atomic under concurrency and _claim_lock does not grow (F11 claim-path).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
