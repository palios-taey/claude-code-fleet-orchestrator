"""Acceptance: causal ledger is append-only, chained, and type-checked."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet_orchestrator.causal_ledger import (  # noqa: E402
    CAUSAL_EVENT_TYPES,
    append_event,
    extract_reported_commit_sha,
    payload_oid,
    verify_chain,
)


FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.startswith("#"):
            continue
        rows.append(json.loads(raw))
    return rows


def main() -> int:
    event_types = [
        "dispatch_claimed",
        "wake_packet_assembled",
        "wake_delivered",
        "dispatch_delivery_failed",
        "worker_outcome_recorded",
        "commit_observed",
        "completion_evidence_verified",
        "ledger_checkpoint",
        "external_witness_anchor",
        "world_manifest_published",
        "consult_completed",
    ]
    _check("all slice event types are registered", set(event_types) == set(CAUSAL_EVENT_TYPES), CAUSAL_EVENT_TYPES)
    with tempfile.TemporaryDirectory(prefix="causal-ledger-") as raw:
        path = Path(raw) / "causal.jsonl"
        parent = ""
        for index, event_type in enumerate(event_types):
            row = append_event(
                event_type,
                subject={"task_id": "task-1", "index": index},
                parents=[parent] if parent else [],
                payload={"index": index},
                path=str(path),
            )
            parent = row["event"]["event_id"]
        rows = _rows(path)
        _check("one row per event type", len(rows) == len(event_types), len(rows))
        _check("chain verifies", verify_chain(str(path)) == {"ok": True, "rows": len(event_types)}, verify_chain(str(path)))
        _check("row has causal row hash fields", {"prev_row_hash", "row_hash"} <= set(rows[-1]), rows[-1])
        _check("payload oid is recorded", rows[-1]["event"]["payload_oid"].startswith("sha256:"), rows[-1]["event"])
        expected_oid = payload_oid({"index": len(event_types) - 1})
        _check("payload oid matches canonical payload", rows[-1]["event"]["payload_oid"] == expected_oid, rows[-1]["event"])
        try:
            append_event("not_registered", subject={"task_id": "task-1"}, path=str(path))
            _check("unknown event type rejected", False)
        except ValueError:
            _check("unknown event type rejected", True)
        before_forge_rows = len(_rows(path))
        try:
            append_event(
                "dispatch_claimed",
                subject={"task_id": "task-1"},
                payload={"truth": "canonical"},
                payload_hash="sha256:" + "0" * 64,
                path=str(path),
            )
            _check("forged payload oid rejected", False)
        except ValueError:
            _check("forged payload oid rejected", True)
        _check("forged payload oid not appended", len(_rows(path)) == before_forge_rows, _rows(path))

    valid_sha = "0123456789abcdef0123456789abcdef01234567"
    other_sha = "abcdef0123456789abcdef0123456789abcdef01"
    _check(
        "structured sha marker extracted",
        extract_reported_commit_sha(f"RESPONSE_READY branch=main sha={valid_sha} verify=ok") == valid_sha,
    )
    _check(
        "unrelated 40-hex details not extracted",
        extract_reported_commit_sha(f"observed unrelated commit {valid_sha} but no marker") is None,
    )
    _check(
        "multiple sha markers not guessed",
        extract_reported_commit_sha(f"sha={valid_sha} sha={other_sha}") is None,
    )
    if FAILURES:
        print("FAILURES:", ", ".join(FAILURES))
        return 1
    print("causal_ledger_acceptance: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
