"""Ship-gate e2e — create_question is parameterized; no Cypher injection (F19).

The query is built from developer-controlled constants only; every user value is a Cypher
parameter. A malicious text/id is stored as DATA, never executed. Also: with-task links a
CONCERNS_TASK edge; without-task works; missing task -> explicit error (not a crash).

Env: ORCH_NEO4J_URI, ORCH_NEO4J_DB, ORCH_REDIS_HOST/PORT, ORCH_DASHBOARD_URL.
"""
from __future__ import annotations

import os
import sys
import uuid

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.orch_schema import (  # noqa: E402
    create_project, create_phase, create_task, create_question,
    TaskParentNotFoundError, get_neo4j_driver,
)
from lib.config import OrchConfig  # noqa: E402

CFG = OrchConfig()
_PFX = f"qinj-ci-{uuid.uuid4().hex[:8]}"
_FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: str = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        _FAILURES.append(label)


def main() -> int:
    drv = get_neo4j_driver(CFG)

    def count_questions():
        with drv.session(database=CFG.neo4j_db) as s:
            return s.run("MATCH (q:OrchQuestion) WHERE q.id STARTS WITH $p RETURN count(q) AS n", p=_PFX).single()["n"]

    def text_of(qid):
        with drv.session(database=CFG.neo4j_db) as s:
            r = s.run("MATCH (q:OrchQuestion {id:$i}) RETURN q.text AS t", i=qid).single()
            return r["t"] if r else None

    with drv.session(database=CFG.neo4j_db) as s:
        s.run("MATCH (n) WHERE n.id STARTS WITH $p DETACH DELETE n", p=_PFX)
    try:
        create_project(project_id=_PFX, name=_PFX, config=CFG)
        create_phase(project_id=_PFX, phase_id=f"{_PFX}::ph", name="ph", config=CFG)
        T = f"{_PFX}::t"
        create_task(phase_id=f"{_PFX}::ph", task_id=T, description="x", owner="a", wake_owner_if_ready=False, config=CFG)

        # 1. no-task path
        q1 = f"{_PFX}-q1"
        create_question(question_id=q1, text="plain question", config=CFG)
        _check("create_question without task works", text_of(q1) == "plain question")

        # 2. with-task path links CONCERNS_TASK
        q2 = f"{_PFX}-q2"
        create_question(question_id=q2, text="about the task", task_id=T, config=CFG)
        with drv.session(database=CFG.neo4j_db) as s:
            linked = s.run("MATCH (q:OrchQuestion {id:$q})-[:CONCERNS_TASK]->(t:OrchTask {id:$t}) RETURN count(*) AS n", q=q2, t=T).single()["n"]
        _check("create_question with task links CONCERNS_TASK", linked == 1, str(linked))

        # 3. INJECTION attempt: malicious text must be stored as DATA, not executed
        before = count_questions()
        evil = "x'}) DETACH DELETE (q) MERGE (h:Hacked {id:'pwned'}) //"
        q3 = f"{_PFX}-q3"
        create_question(question_id=q3, text=evil, context="'} MATCH (n) DETACH DELETE n //", config=CFG)
        _check("injection text stored verbatim as data (parameterized)", text_of(q3) == evil, text_of(q3))
        with drv.session(database=CFG.neo4j_db) as s:
            hacked = s.run("MATCH (h:Hacked {id:'pwned'}) RETURN count(h) AS n").single()["n"]
        _check("injection did NOT execute (no Hacked node created)", hacked == 0, str(hacked))
        _check("injection did NOT delete questions", count_questions() == before + 1, f"{count_questions()} vs {before+1}")

        # 4. missing task -> explicit error, not crash
        raised = False
        try:
            create_question(question_id=f"{_PFX}-q4", text="x", task_id=f"{_PFX}::nope", config=CFG)
        except TaskParentNotFoundError:
            raised = True
        _check("missing task -> TaskParentNotFoundError (explicit, not crash)", raised)
    finally:
        with drv.session(database=CFG.neo4j_db) as s:
            s.run("MATCH (n) WHERE n.id STARTS WITH $p DETACH DELETE n", p=_PFX)
            s.run("MATCH (h:Hacked {id:'pwned'}) DETACH DELETE h")
    if _FAILURES:
        print(f"\nFAIL — {len(_FAILURES)}: {_FAILURES}")
        return 1
    print("\nPASS — create_question parameterized; injection stored as data, never executed (F19).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
