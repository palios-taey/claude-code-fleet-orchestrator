"""Acceptance: dispatch and record_outcome append causal provenance events."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import fleet_orchestrator.current_task_binding as current_task_binding  # noqa: E402
import fleet_orchestrator.dispatch as dispatch_module  # noqa: E402
from fleet_orchestrator.causal_ledger import UNKNOWN, append_event, verify_chain  # noqa: E402


FAILURES: list[str] = []


class FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: object, **_kwargs: object) -> bool:
        self.store[key] = str(value)
        return True

    def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            if key in self.store:
                del self.store[key]
                deleted += 1
        return deleted


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


def _events_for_task(rows: list[dict], task_id: str) -> list[dict]:
    return [
        row["event"]
        for row in rows
        if row["event"].get("subject", {}).get("task_id") == task_id
    ]


def _fake_bind(fake: FakeRedis):
    def bind_current_task(*, worker: str, task_id: str, description: str, supervisor: str | None,
                          set_parent: bool, force: bool, guard_existing: bool,
                          dispatcher: str | None) -> float:
        started_at = 123.0
        current_task = {
            "task_id": task_id,
            "description": description,
            "supervisor": supervisor,
            "started_at": started_at,
            "dispatcher": dispatcher,
        }
        fake.set(dispatch_module._state_key(worker, "current_task"), json.dumps(current_task))
        return started_at

    return bind_current_task


def _fake_assemble(*_args: object, **_kwargs: object) -> tuple[str, dict]:
    return "rendered wake body", {
        "cli": "codex",
        "packet_id": "packet-1",
        "provenance_hash": "provenance-1",
        "injection_receipt": {"line": "loaded refs: none"},
        "size_report": {"bytes": 18},
        "rules": [{"scope": "global", "path": "rules.md"}],
        "refs": {"overall": []},
    }


def main() -> int:
    fake = FakeRedis()
    captured_receipts: list[dict] = []
    captured_rollbacks: list[tuple[str, str, float | None]] = []
    worker = "provenance-worker-codex"
    supervisor = "provenance-supervisor"
    success_task = "provenance::success"
    capture_failure_task = "provenance::capture-failure"
    assemble_failure_task = "provenance::assemble-failure"
    failure_task = "provenance::failure"
    commit_sha = "0123456789abcdef0123456789abcdef01234567"

    def fake_run(args: list[str], **_kwargs: object) -> SimpleNamespace:
        if args[2] == "rendered wake body" and args[1] == worker and args[-2] == "--actionable-inputs":
            return SimpleNamespace(returncode=0, stdout="delivered", stderr="")
        return SimpleNamespace(returncode=0, stdout="delivered", stderr="")

    def fake_failure_run(_args: list[str], **_kwargs: object) -> SimpleNamespace:
        return SimpleNamespace(returncode=17, stdout="", stderr="notify failed")

    def fake_assemble_failure(*_args: object, **_kwargs: object) -> tuple[str, dict]:
        raise RuntimeError("assemble failed")

    def append_with_wake_failure(event_type: str, **kwargs: object) -> dict:
        if event_type == "wake_delivered":
            raise RuntimeError("simulated wake_delivered append failure")
        return append_event(event_type, **kwargs)

    def fake_rollback(worker_arg: str, task_id_arg: str, binding_nonce: float | None) -> None:
        captured_rollbacks.append((worker_arg, task_id_arg, binding_nonce))
        fake.delete(dispatch_module._state_key(worker_arg, "current_task"))

    def fake_clear(worker_arg: str, task_id_arg: str, redis_client: FakeRedis, reason: str) -> bool:
        fake.delete(dispatch_module._state_key(worker_arg, "current_task"))
        return True

    with tempfile.TemporaryDirectory(prefix="provenance-dispatch-") as raw:
        ledger_path = Path(raw) / "causal.jsonl"
        old_ledger = os.environ.get("ORCH_CAUSAL_LEDGER_PATH")
        old_roots = os.environ.get("ORCH_SESSION_ROOTS")
        os.environ["ORCH_CAUSAL_LEDGER_PATH"] = str(ledger_path)
        os.environ.pop("ORCH_SESSION_ROOTS", None)
        try:
            with mock.patch.object(dispatch_module, "_redis_connect", return_value=fake), \
                 mock.patch.object(dispatch_module, "_resolve_product_id", return_value=None), \
                 mock.patch.object(dispatch_module, "_claim_ready_orch_task"), \
                 mock.patch.object(dispatch_module, "bind_current_task", side_effect=_fake_bind(fake)), \
                 mock.patch.object(dispatch_module, "_assemble_dispatch_prompt", side_effect=_fake_assemble), \
                 mock.patch.object(dispatch_module, "mark_superseded_for_task"), \
                 mock.patch.object(dispatch_module, "notify_cli", return_value="notify"), \
                 mock.patch.object(dispatch_module, "hook_installation_status", return_value=SimpleNamespace(ok=True, detail="hooked")), \
                 mock.patch.object(dispatch_module.subprocess, "run", side_effect=fake_run), \
                 mock.patch.object(dispatch_module, "_clear_replaced_force_bindings"), \
                 mock.patch.object(dispatch_module, "maybe_emit_decision_receipt", side_effect=lambda _kind, body: captured_receipts.append(body)):
                dispatch_module.dispatch(
                    worker,
                    success_task,
                    "successful provenance dispatch",
                    supervisor=supervisor,
                    priority="high",
                )

            rows = _rows(ledger_path)
            event_types = [row["event"]["event_type"] for row in rows]
            _check("success dispatch event order", event_types == [
                "dispatch_claimed",
                "wake_packet_assembled",
                "wake_delivered",
            ], event_types)
            attestation = rows[1]["event"]["payload"]["attestation"]
            _check("actor id is worker session", attestation["actor_id"] == worker, attestation)
            _check("git author is not actor field", "git_author" not in attestation, attestation)
            _check("model endpoint is explicit Unknown", attestation["model_endpoint"]["endpoint_ref"] == UNKNOWN, attestation)
            _check("runtime generation is explicit Unknown", attestation["runtime"]["runtime_generation_id"] == UNKNOWN, attestation)
            current_task = json.loads(fake.get(dispatch_module._state_key(worker, "current_task")) or "{}")
            _check("current_task stores attestation id", current_task["causal"]["attestation_id"] == attestation["attestation_id"], current_task)
            _check("wake receipt carries causal ids", captured_receipts[0]["observable_state"]["causal_event_ids"] == [
                rows[0]["event"]["event_id"],
                rows[1]["event"]["event_id"],
                rows[2]["event"]["event_id"],
            ], captured_receipts)

            with mock.patch.object(dispatch_module, "_redis_connect", return_value=fake), \
                 mock.patch.object(dispatch_module, "_notify_supervisor_response_ready"), \
                 mock.patch.object(current_task_binding, "clear_matching_current_task", side_effect=fake_clear):
                dispatch_module.record_outcome(worker, "done", f"RESPONSE_READY branch=codex/example sha={commit_sha} verify=ok")

            rows = _rows(ledger_path)
            outcome_event = rows[-1]["event"]
            _check("record_outcome appends child event", outcome_event["event_type"] == "worker_outcome_recorded", outcome_event)
            _check("outcome event binds reported commit", outcome_event["payload"]["reported_commit_sha"] == commit_sha, outcome_event)
            _check("outcome event points at delivered wake", outcome_event["parents"][0] == rows[2]["event"]["event_id"], outcome_event)

            with mock.patch.object(dispatch_module, "_redis_connect", return_value=fake), \
                 mock.patch.object(dispatch_module, "_resolve_product_id", return_value=None), \
                 mock.patch.object(dispatch_module, "_claim_ready_orch_task"), \
                 mock.patch.object(dispatch_module, "bind_current_task", side_effect=_fake_bind(fake)), \
                 mock.patch.object(dispatch_module, "_assemble_dispatch_prompt", side_effect=_fake_assemble), \
                 mock.patch.object(dispatch_module, "mark_superseded_for_task"), \
                 mock.patch.object(dispatch_module, "notify_cli", return_value="notify"), \
                 mock.patch.object(dispatch_module, "hook_installation_status", return_value=SimpleNamespace(ok=True, detail="hooked")), \
                 mock.patch.object(dispatch_module.subprocess, "run", side_effect=fake_run), \
                 mock.patch.object(dispatch_module, "_clear_replaced_force_bindings"), \
                 mock.patch.object(dispatch_module, "append_causal_event", side_effect=append_with_wake_failure), \
                 mock.patch.object(dispatch_module, "maybe_emit_decision_receipt", side_effect=lambda _kind, body: captured_receipts.append(body)):
                dispatch_module.dispatch(
                    worker,
                    capture_failure_task,
                    "provenance dispatch with post-delivery capture failure",
                    supervisor=supervisor,
                )

            rows = _rows(ledger_path)
            capture_events = _events_for_task(rows, capture_failure_task)
            _check("wake-delivered append failure does not raise", [event["event_type"] for event in capture_events] == [
                "dispatch_claimed",
                "wake_packet_assembled",
            ], capture_events)
            capture_state = captured_receipts[-1]["observable_state"]
            _check("wake receipt marks capture failure", capture_state["capture_failure"]["marker"] == "capture_failure", capture_state)
            current_task = json.loads(fake.get(dispatch_module._state_key(worker, "current_task")) or "{}")
            _check("current_task marks capture failure", current_task["causal"]["capture_failure"]["marker"] == "capture_failure", current_task)

            with mock.patch.object(dispatch_module, "_redis_connect", return_value=fake), \
                 mock.patch.object(dispatch_module, "_resolve_product_id", return_value=None), \
                 mock.patch.object(dispatch_module, "_claim_ready_orch_task"), \
                 mock.patch.object(dispatch_module, "bind_current_task", side_effect=_fake_bind(fake)), \
                 mock.patch.object(dispatch_module, "_assemble_dispatch_prompt", side_effect=fake_assemble_failure), \
                 mock.patch.object(dispatch_module, "hook_installation_status", return_value=SimpleNamespace(ok=True, detail="hooked")), \
                 mock.patch.object(dispatch_module, "_rollback_claim", side_effect=fake_rollback):
                try:
                    dispatch_module.dispatch(worker, assemble_failure_task, "assembly failure", supervisor=supervisor)
                    _check("assembly failure raises", False)
                except RuntimeError:
                    _check("assembly failure raises", True)

            rows = _rows(ledger_path)
            assembly_events = _events_for_task(rows, assemble_failure_task)
            _check("assembly rollback appends terminal child", [event["event_type"] for event in assembly_events] == [
                "dispatch_claimed",
                "dispatch_delivery_failed",
            ], assembly_events)
            _check("assembly failure child parents claim", assembly_events[-1]["parents"] == [assembly_events[0]["event_id"]], assembly_events)
            _check("assembly failure terminates delivery", assembly_events[-1]["payload"]["outcome"]["status"] == "delivery_failed", assembly_events[-1])

            with mock.patch.object(dispatch_module, "_redis_connect", return_value=fake), \
                 mock.patch.object(dispatch_module, "_resolve_product_id", return_value=None), \
                 mock.patch.object(dispatch_module, "_claim_ready_orch_task"), \
                 mock.patch.object(dispatch_module, "bind_current_task", side_effect=_fake_bind(fake)), \
                 mock.patch.object(dispatch_module, "_assemble_dispatch_prompt", side_effect=_fake_assemble), \
                 mock.patch.object(dispatch_module, "mark_superseded_for_task"), \
                 mock.patch.object(dispatch_module, "notify_cli", return_value="notify"), \
                 mock.patch.object(dispatch_module, "hook_installation_status", return_value=SimpleNamespace(ok=True, detail="hooked")), \
                 mock.patch.object(dispatch_module.subprocess, "run", side_effect=fake_failure_run), \
                 mock.patch.object(dispatch_module, "_rollback_claim", side_effect=fake_rollback):
                try:
                    dispatch_module.dispatch(worker, failure_task, "failed provenance dispatch", supervisor=supervisor)
                    _check("delivery failure raises", False)
                except RuntimeError:
                    _check("delivery failure raises", True)

            rows = _rows(ledger_path)
            _check("delivery failure rollback called", captured_rollbacks[-1][:2] == (worker, failure_task), captured_rollbacks)
            delivery_failure_event = _events_for_task(rows, failure_task)[-1]
            _check("delivery failure event appended", delivery_failure_event["event_type"] == "dispatch_delivery_failed", delivery_failure_event)
            _check("delivery failure terminates attestation", delivery_failure_event["payload"]["outcome"]["status"] == "delivery_failed", delivery_failure_event)
            _check(
                "delivery failure references attestation",
                delivery_failure_event["payload"]["attestation_id"] == delivery_failure_event["actor_attestation_id"] != UNKNOWN,
                delivery_failure_event,
            )
            _check("final ledger verifies", verify_chain(str(ledger_path)) == {"ok": True, "rows": len(rows)}, verify_chain(str(ledger_path)))
        finally:
            if old_ledger is None:
                os.environ.pop("ORCH_CAUSAL_LEDGER_PATH", None)
            else:
                os.environ["ORCH_CAUSAL_LEDGER_PATH"] = old_ledger
            if old_roots is None:
                os.environ.pop("ORCH_SESSION_ROOTS", None)
            else:
                os.environ["ORCH_SESSION_ROOTS"] = old_roots

    if FAILURES:
        print("FAILURES:", ", ".join(FAILURES))
        return 1
    print("provenance_dispatch_acceptance: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
