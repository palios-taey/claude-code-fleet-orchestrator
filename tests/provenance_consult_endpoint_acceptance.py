"""Acceptance: consult completion ingress appends through the orchestrator ledger."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet_orchestrator import tasks_api  # noqa: E402
from fleet_orchestrator.causal_ledger import read_ledger_rows, verify_chain  # noqa: E402


FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _oid(ch: str) -> str:
    return "sha256:" + ch * 64


def _event(ch: str) -> str:
    return "event:" + ch * 64


def main() -> int:
    saved = {
        "ORCH_CAUSAL_LEDGER_PATH": os.environ.get("ORCH_CAUSAL_LEDGER_PATH"),
        "ORCH_AUTH_TOKEN": os.environ.get("ORCH_AUTH_TOKEN"),
    }
    try:
        with tempfile.TemporaryDirectory(prefix="consult-ingress-") as raw:
            ledger_path = Path(raw) / "causal.jsonl"
            os.environ["ORCH_CAUSAL_LEDGER_PATH"] = str(ledger_path)
            os.environ.pop("ORCH_AUTH_TOKEN", None)
            body = {
                "source_family_id": "family-consult-1",
                "request_oid": _oid("a"),
                "rendered_prompt_oid": _oid("b"),
                "response_oid": _oid("c"),
                "platform": "chatgpt",
                "seat_role": "HORIZON",
                "requester": "taeys-hands",
                "session_url": "https://chat.openai.com/c/example",
                "parents": [_event("d")],
            }
            client = TestClient(tasks_api.app)

            created = client.post("/api/provenance/consult-event", json=body)
            created_body = created.json()
            _check("consult endpoint accepts canonical body", created.status_code == 200, created_body)
            _check("consult endpoint returns event id", str(created_body.get("event_id", "")).startswith("event:"), created_body)
            _check("consult endpoint returns attestation id", str(created_body.get("attestation_id", "")).startswith("attestation:"), created_body)
            _check("consult endpoint returns row hash", len(str(created_body.get("row_hash", ""))) == 64, created_body)

            rows = read_ledger_rows(str(ledger_path))
            event = rows[0]["event"]
            payload = event["payload"]
            attestation = payload["attestation"]
            _check("consult append writes one causal row", len(rows) == 1, rows)
            _check("consult event type recorded", event["event_type"] == "consult_completed", event)
            _check("consult event carries request oid", payload["request_oid"] == body["request_oid"], payload)
            _check("consult event carries authority roots", event["authority_roots"] == [body["request_oid"], body["rendered_prompt_oid"], body["response_oid"]], event)
            _check("orchestrator issues source-seat attestation", attestation["issued_by"] == "orchestrator-runtime" and attestation["source_seat"] == "taeys-hands", attestation)
            _check("attestation id is bound into event", event["actor_attestation_id"] == created_body["attestation_id"] == attestation["attestation_id"], event)
            _check("ledger chain verifies after consult append", verify_chain(str(ledger_path)) == {"ok": True, "rows": 1}, verify_chain(str(ledger_path)))

            replay = client.post("/api/provenance/consult-event", json=body)
            replay_body = replay.json()
            _check("retry returns same event id", replay.status_code == 200 and replay_body["event_id"] == created_body["event_id"], replay_body)
            _check("retry returns same row hash", replay_body["row_hash"] == created_body["row_hash"], replay_body)
            _check("retry does not double append", len(read_ledger_rows(str(ledger_path))) == 1, read_ledger_rows(str(ledger_path)))

            bad_oid = {**body, "request_oid": "not-a-sha"}
            rejected = client.post("/api/provenance/consult-event", json=bad_oid)
            _check("bad request oid rejects 400", rejected.status_code == 400 and "request_oid" in json.dumps(rejected.json()), rejected.text)
            _check("bad oid does not append", len(read_ledger_rows(str(ledger_path))) == 1, read_ledger_rows(str(ledger_path)))

            bad_extra_oid = {**body, "unused_oid": "not-a-sha"}
            rejected_extra = client.post("/api/provenance/consult-event", json=bad_extra_oid)
            _check("any extra oid field is validated", rejected_extra.status_code == 400 and "unused_oid" in json.dumps(rejected_extra.json()), rejected_extra.text)

            bad_parent = {**body, "request_oid": _oid("e"), "parents": ["not-an-event"]}
            rejected_parent = client.post("/api/provenance/consult-event", json=bad_parent)
            _check("bad parent id rejects 400", rejected_parent.status_code == 400 and "parents[0]" in json.dumps(rejected_parent.json()), rejected_parent.text)

            non_object = client.post("/api/provenance/consult-event", json=[])
            _check("array body rejects 422", non_object.status_code == 422, non_object.text)
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value

    if FAILURES:
        print("FAILURES:", ", ".join(FAILURES))
        return 1
    print("provenance_consult_endpoint_acceptance: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
