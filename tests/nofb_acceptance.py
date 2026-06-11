#!/usr/bin/env python3
"""No-fallback acceptance: missing parents stay explicit 4xx, never TypeError/500."""
from __future__ import annotations

import os
import sys
import uuid

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from lib.config import OrchConfig, get_neo4j_driver  # noqa: E402
from lib.orch_schema import (  # noqa: E402
    TaskParentNotFoundError,
    create_question,
    init_schema,
)
from lib.tasks_api import app  # noqa: E402

CFG = OrchConfig()
PREFIX = f"nofb-ci-{uuid.uuid4().hex[:8]}"
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: str = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _cleanup() -> None:
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=PREFIX)


def main() -> int:
    init_schema(config=CFG)
    _cleanup()
    client = TestClient(app)

    try:
        missing_task = f"{PREFIX}-missing-task"
        patch_response = client.patch(
            f"/api/task/{missing_task}",
            json={"status": "in_progress", "from": "nofb-acceptance"},
        )
        _check(
            "PATCH missing task returns 404, not 500",
            patch_response.status_code == 404,
            f"status={patch_response.status_code} body={patch_response.text[:160]}",
        )
        _check(
            "PATCH missing task names the missing id",
            missing_task in patch_response.text and "not found" in patch_response.text,
            patch_response.text[:160],
        )

        missing_phase = f"{PREFIX}-missing-phase"
        create_response = client.post(
            "/api/task/create",
            json={
                "description": "should fail loud on missing phase",
                "phase_id": missing_phase,
                "from": "nofb-acceptance",
            },
        )
        _check(
            "POST task under missing phase returns 404, not 500",
            create_response.status_code == 404,
            f"status={create_response.status_code} body={create_response.text[:160]}",
        )
        _check(
            "POST task under missing phase is explicit, not TypeError",
            missing_phase in create_response.text and "TypeError" not in create_response.text,
            create_response.text[:160],
        )

        missing_parent_task = f"{PREFIX}-missing-parent-task"
        try:
            create_question(
                question_id=f"{PREFIX}-question",
                text="should fail loud on missing task",
                task_id=missing_parent_task,
                config=CFG,
            )
        except TaskParentNotFoundError as exc:
            question_error = str(exc)
        else:
            question_error = ""
        _check(
            "create_question under missing task raises explicit parent error",
            missing_parent_task in question_error and "not found" in question_error,
            question_error or "no exception",
        )
        with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
            orphan_question = session.run(
                "MATCH (q:OrchQuestion {id: $id}) RETURN q.id AS id",
                id=f"{PREFIX}-question",
            ).single()
        _check("missing-task question create does not leave an orphan question", orphan_question is None, str(orphan_question))
    finally:
        _cleanup()

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - no-fallback missing-parent acceptance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
