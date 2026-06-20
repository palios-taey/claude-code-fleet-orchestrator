#!/usr/bin/env python3
from __future__ import annotations

import os
import sys

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet_orchestrator.tasks_api import app  # noqa: E402

FAILURES: list[str] = []


def _check(label: str, condition: bool, detail: object = "") -> None:
    print(("PASS " if condition else "FAIL ") + label + ("" if condition else f" -> {detail}"))
    if not condition:
        FAILURES.append(label)


def main() -> int:
    client = TestClient(app)

    malformed = client.patch(
        "/api/task/body-probe",
        content=b'{"status":',
        headers={"content-type": "application/json"},
    )
    _check("malformed JSON returns 400", malformed.status_code == 400, malformed.text)
    malformed_body = malformed.json()
    _check("malformed JSON response is structured", malformed_body.get("ok") is False, malformed_body)
    _check("malformed JSON teaches valid JSON", "valid JSON" in malformed_body.get("error", ""), malformed_body)
    _check("malformed JSON gives next step", "PATCH /api/task/body-probe" in malformed_body.get("next_step", ""), malformed_body)

    non_object = client.patch("/api/task/body-probe", json=[])
    _check("array JSON returns 422", non_object.status_code == 422, non_object.text)
    non_object_body = non_object.json()
    _check("array JSON response is structured", non_object_body.get("ok") is False, non_object_body)
    _check("array JSON teaches object body", "JSON object" in non_object_body.get("error", ""), non_object_body)
    _check("array JSON gives next step", "terminal statuses require evidence" in non_object_body.get("next_step", ""), non_object_body)

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - malformed task update bodies return teaching 4xx responses before runtime state access.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
