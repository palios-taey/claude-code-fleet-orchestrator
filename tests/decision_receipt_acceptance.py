"""Ship-gate e2e — decision receipts are typed, immutable, and enabled by default."""
from __future__ import annotations

import asyncio
import contextlib
import io
import json
import importlib
import os
import re
import sys
from types import SimpleNamespace
from unittest import mock

from fastapi.testclient import TestClient

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

chat_layer = importlib.import_module("fleet_orchestrator.chat_layer")  # noqa: E402
cli_receipts = importlib.import_module("fleet_orchestrator.cli_taey_receipts")  # noqa: E402
receipts = importlib.import_module("fleet_orchestrator.decision_receipt")  # noqa: E402
dispatch_module = importlib.import_module("fleet_orchestrator.dispatch")  # noqa: E402
tasks_api = importlib.import_module("fleet_orchestrator.tasks_api")  # noqa: E402


FAILURES: list[str] = []
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class FakeRedis:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict, int | None, bool | None]] = []

    def xadd(self, stream: str, fields: dict, maxlen: int | None = None, approximate: bool | None = None) -> str:
        self.events.append((stream, dict(fields), maxlen, approximate))
        return f"{len(self.events)}-0"

    def _entries(self, stream: str) -> list[tuple[str, dict]]:
        return [
            (f"{idx}-0", fields)
            for idx, (event_stream, fields, _maxlen, _approximate) in enumerate(self.events, start=1)
            if event_stream == stream
        ]

    def xrevrange(self, stream: str, max: str = "+", min: str = "-", count: int | None = None) -> list[tuple[str, dict]]:
        del max, min
        entries = list(reversed(self._entries(stream)))
        return entries[:count] if count is not None else entries

    def xrange(self, stream: str, min: str = "-", max: str = "+", count: int | None = None) -> list[tuple[str, dict]]:
        del min, max
        entries = self._entries(stream)
        return entries[:count] if count is not None else entries


class FailingRedis:
    def xadd(self, *args, **kwargs) -> str:
        raise RuntimeError("receipt sink down")


class FakeAsyncRedis:
    def __init__(self) -> None:
        self.lists: dict[str, list[str]] = {}
        self.values: dict[str, str] = {}

    async def rpush(self, key: str, value: str) -> None:
        self.lists.setdefault(key, []).append(value)

    async def set(self, key: str, value: str) -> None:
        self.values[key] = value


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _payload(fake: FakeRedis, index: int = -1) -> dict:
    stream, fields, maxlen, approximate = fake.events[index]
    body = json.loads(fields["receipt"])
    body["_stream"] = stream
    body["_maxlen"] = maxlen
    body["_approximate"] = approximate
    body["_event_type"] = fields["type"]
    return body


def _assert_schema(label: str, receipt: dict) -> None:
    missing = [field for field in receipts.RECEIPT_FIELDS if field not in receipt]
    _check(f"{label}: full schema present", not missing, missing)
    _check(f"{label}: observable_state_hash is sha256", bool(SHA256_RE.fullmatch(receipt.get("observable_state_hash", ""))), receipt)
    _check(f"{label}: append-only stream event", receipt.get("_stream") == receipts.RECEIPT_STREAM and receipt.get("_event_type") == "decision_receipt", receipt)


def _receipt_core_contract() -> None:
    fake = FakeRedis()
    ctx = {
        "why_this_context": "selected refs and rules for wake",
        "refs_used": [{"path": "plan.md", "content": "original"}],
        "rule_tier_applied": [{"scope": "project", "path": "rules/projects/dynctx.md"}],
        "observable_state": {"packet_id": "packet-1", "state": {"a": 1}},
        "world_id": "world:receipt-test",
        "attestation_id": "attestation:receipt-test",
        "causal_event_ids": ["event:one", "event:two"],
        "blocked_on": "blocked-task",
        "next_contract": "continue task",
    }
    receipt = receipts.emit_receipt("wake_packet_assembly", ctx, redis_client=fake)
    ctx["refs_used"][0]["content"] = "mutated-after-emit"
    payload = _payload(fake)

    _assert_schema("emit_receipt", payload)
    _check("emit_receipt returns same immutable payload", receipt["observable_state_hash"] == payload["observable_state_hash"], payload)
    _check("receipt payload captures refs before caller mutation", payload["refs_used"][0]["content"] == "original", payload["refs_used"])
    _check("receipt uses requested rule_tier_applied field", isinstance(payload["rule_tier_applied"], str) and "rules/projects/dynctx.md" in payload["rule_tier_applied"], payload)
    _check(
        "receipt surfaces proof capsule linkage fields",
        payload["world_id"] == "world:receipt-test"
        and payload["attestation_id"] == "attestation:receipt-test"
        and payload["causal_event_ids"] == ["event:one", "event:two"],
        payload,
    )

    old_enabled = os.environ.get("ORCH_DECISION_RECEIPTS_ENABLED")
    try:
        os.environ.pop("ORCH_DECISION_RECEIPTS_ENABLED", None)
        default_on = FakeRedis()
        with mock.patch.object(receipts, "get_redis_sync", return_value=default_on):
            result = receipts.maybe_emit_receipt("wake", ctx)
        _check("maybe_emit_receipt defaults on", result is not None and len(default_on.events) == 1, default_on.events)

        os.environ["ORCH_DECISION_RECEIPTS_ENABLED"] = "0"
        disabled = FakeRedis()
        with mock.patch.object(receipts, "get_redis_sync", return_value=disabled):
            result = receipts.maybe_emit_receipt("wake", ctx)
        _check("explicitly disabled receipts are a no-op", result is None and not disabled.events, disabled.events)
    finally:
        if old_enabled is None:
            os.environ.pop("ORCH_DECISION_RECEIPTS_ENABLED", None)
        else:
            os.environ["ORCH_DECISION_RECEIPTS_ENABLED"] = old_enabled


def _wake_packet_wiring_contract() -> None:
    fake = FakeRedis()
    os.environ["ORCH_WAKE_PACKET_ENDPOINT_ENABLED"] = "1"
    os.environ["ORCH_DECISION_RECEIPTS_ENABLED"] = "1"
    context = {
        "overall_refs": [],
        "supervisor_refs": [],
        "project_refs": [],
        "phase_refs": [],
        "task_refs": [{"path": "plans/w2.md", "content": "receipt ref"}],
        "memory": [],
        "rules": [{"scope": "project", "path": "rules/projects/dynctx.md", "text": "rule"}],
        "snapshot": {"repo_head": "abc123"},
        "budget_used": 0,
    }
    try:
        with mock.patch.object(tasks_api, "_cfg", return_value=SimpleNamespace(session_ids=["conductor-codex"])), \
             mock.patch.object(tasks_api, "select_wake_context", return_value=context), \
             mock.patch.object(receipts, "get_redis_sync", return_value=fake):
            response = TestClient(tasks_api.app).get("/api/sessions/conductor-codex/wake-packet?cli=codex")
    finally:
        os.environ.pop("ORCH_WAKE_PACKET_ENDPOINT_ENABLED", None)
        os.environ.pop("ORCH_DECISION_RECEIPTS_ENABLED", None)

    _check("wake-packet endpoint still succeeds with receipts enabled", response.status_code == 200 and response.json().get("ok") is True, response.text)
    payload = _payload(fake)
    _assert_schema("wake-packet receipt", payload)
    _check("wake-packet receipt records refs_used", payload["refs_used"] and payload["refs_used"][0]["path"] == "plans/w2.md", payload)


async def _chat_wiring_async(fake: FakeRedis) -> None:
    async_redis = FakeAsyncRedis()
    await chat_layer.append_message("conductor", "operator", "hello", redis_client=async_redis)
    await chat_layer.escalate("conductor", "need answer", redis_client=async_redis)


def _chat_wiring_contract() -> None:
    fake = FakeRedis()
    os.environ["ORCH_DECISION_RECEIPTS_ENABLED"] = "1"
    try:
        with mock.patch.object(receipts, "get_redis_sync", return_value=fake):
            asyncio.run(_chat_wiring_async(fake))
    finally:
        os.environ.pop("ORCH_DECISION_RECEIPTS_ENABLED", None)

    kinds = [json.loads(event[1]["receipt"])["kind"] for event in fake.events]
    _check("chat send and escalate emit receipts", kinds == ["chat_send", "chat_escalate"], kinds)
    for i, kind in enumerate(kinds):
        _assert_schema(f"{kind} receipt", _payload(fake, i))


def _wake_wiring_contract() -> None:
    fake = FakeRedis()
    os.environ["ORCH_DECISION_RECEIPTS_ENABLED"] = "1"
    try:
        with mock.patch.object(tasks_api, "_cfg", return_value=SimpleNamespace(session_ids=["conductor-codex"])), \
             mock.patch.object(tasks_api.subprocess, "run", return_value=SimpleNamespace(returncode=0, stderr="", stdout="")), \
             mock.patch.object(receipts, "get_redis_sync", return_value=fake):
            response = TestClient(tasks_api.app).post(
                "/api/sessions/conductor-codex/notify",
                json={"type": "command", "message": "wake up"},
            )
    finally:
        os.environ.pop("ORCH_DECISION_RECEIPTS_ENABLED", None)

    _check("wake notify endpoint still succeeds with receipts enabled", response.status_code == 200 and response.json().get("ok") is True, response.text)
    payload = _payload(fake)
    _assert_schema("wake receipt", payload)
    _check("wake receipt kind is wake", payload["kind"] == "wake", payload)


def _dispatch_wake_wiring_contract() -> None:
    fake = FakeRedis()
    os.environ["ORCH_DECISION_RECEIPTS_ENABLED"] = "1"
    try:
        with mock.patch.object(dispatch_module, "_resolve_product_id", return_value=None), \
             mock.patch.object(dispatch_module, "_redis_connect", return_value=SimpleNamespace(get=lambda _key: None)), \
             mock.patch.object(dispatch_module, "_claim_ready_orch_task"), \
             mock.patch.object(dispatch_module, "mark_superseded_for_task"), \
             mock.patch.object(dispatch_module, "bind_current_task", return_value=123.0), \
             mock.patch.object(dispatch_module, "_assemble_dispatch_prompt", return_value=(
                 "# AGENTS.md Dynamic Context\nDISPATCH_PACKET",
                 {
                     "cli": "codex",
                     "packet_id": "packet-dispatch-receipt",
                     "provenance_hash": "abc123",
                     "proof_capsule": {"world_id": "world:dispatch-receipt"},
                     "world_id": "world:dispatch-receipt",
                     "size_report": {"under_budget": True},
                     "rules": [{"scope": "global", "path": "rules/global.md"}],
                     "refs": {"overall": [], "supervisor": [], "project": [], "phase": [], "task": []},
                 },
             )), \
             mock.patch.object(dispatch_module, "OrchConfig", return_value=SimpleNamespace(notify_cli_path="notify")), \
             mock.patch.object(dispatch_module.subprocess, "run", return_value=SimpleNamespace(returncode=0, stderr="", stdout="")), \
             mock.patch.object(receipts, "get_redis_sync", return_value=fake):
            dispatch_module.dispatch(
                worker="worker-codex",
                task_id="dynctx::task",
                description="dispatch receipt",
                supervisor="conductor",
            )
    finally:
        os.environ.pop("ORCH_DECISION_RECEIPTS_ENABLED", None)

    payload = _payload(fake)
    _assert_schema("dispatch wake receipt", payload)
    _check("dispatch wake receipt records dispatch source", payload["kind"] == "wake", payload)
    _check("dispatch wake receipt records assembled packet metadata",
           "mandatory wake packet" in payload.get("why_this_context", "")
           and "rules/global.md" in payload.get("rule_tier_applied", ""),
           payload)
    _check(
        "dispatch wake receipt surfaces proof linkage fields",
        payload["world_id"] == "world:dispatch-receipt"
        and payload["attestation_id"].startswith("attestation:")
        and len(payload["causal_event_ids"]) == 3,
        payload,
    )


def _receipt_consumer_contract() -> None:
    fake = FakeRedis()
    receipts.emit_receipt(
        "wake",
        {
            "why_this_context": "wake explanation",
            "observable_state": {"session": "worker"},
        },
        redis_client=fake,
    )
    receipts.emit_receipt(
        "chat_send",
        {
            "why_this_context": "chat explanation",
            "observable_state": {"session": "worker"},
        },
        redis_client=fake,
    )

    recent = receipts.read_recent_receipts(limit=5, redis_client=fake)
    _check("receipt reader consumes Redis stream", [item["kind"] for item in recent] == ["chat_send", "wake"], recent)
    _check("receipt reader surfaces stream ids", all(item.get("_stream_id") for item in recent), recent)
    filtered = receipts.read_recent_receipts(limit=5, kind="wake", redis_client=fake)
    _check("receipt reader filters by kind", [item["kind"] for item in filtered] == ["wake"], filtered)

    out = io.StringIO()
    with mock.patch.object(cli_receipts, "read_recent_receipts", return_value=recent), contextlib.redirect_stdout(out):
        code = cli_receipts.main(["list", "--json", "--limit", "5"])
    rendered = json.loads(out.getvalue())
    _check("taey-receipts CLI exits cleanly", code == 0, code)
    _check("taey-receipts CLI surfaces receipts", [item["kind"] for item in rendered] == ["chat_send", "wake"], rendered)


async def _chat_fail_open_async() -> None:
    async_redis = FakeAsyncRedis()
    await chat_layer.append_message("conductor", "operator", "hello", redis_client=async_redis)
    await chat_layer.escalate("conductor", "need answer", redis_client=async_redis)


def _receipt_sink_fail_open_contract() -> None:
    os.environ["ORCH_DECISION_RECEIPTS_ENABLED"] = "1"
    try:
        with mock.patch.object(receipts, "get_redis_sync", return_value=FailingRedis()):
            direct = receipts.maybe_emit_receipt("wake", {"why_this_context": "sink fails"})
            _check("maybe_emit_receipt returns None when sink fails", direct is None, direct)
            try:
                asyncio.run(_chat_fail_open_async())
                chat_ok = True
            except Exception as exc:
                chat_ok = exc
            _check("chat append/escalate succeed when receipt sink fails", chat_ok is True, chat_ok)

            with mock.patch.object(tasks_api, "_cfg", return_value=SimpleNamespace(session_ids=["conductor-codex"])), \
                 mock.patch.object(tasks_api.subprocess, "run", return_value=SimpleNamespace(returncode=0, stderr="", stdout="")):
                response = TestClient(tasks_api.app).post(
                    "/api/sessions/conductor-codex/notify",
                    json={"type": "command", "message": "wake up"},
                )
            _check("session_notify succeeds when receipt sink fails", response.status_code == 200 and response.json().get("ok") is True, response.text)

            with mock.patch.object(dispatch_module, "_resolve_product_id", return_value=None), \
                 mock.patch.object(dispatch_module, "_redis_connect", return_value=SimpleNamespace(get=lambda _key: None)), \
                 mock.patch.object(dispatch_module, "_claim_ready_orch_task"), \
                 mock.patch.object(dispatch_module, "mark_superseded_for_task"), \
                 mock.patch.object(dispatch_module, "bind_current_task", return_value=123.0), \
                 mock.patch.object(dispatch_module, "_assemble_dispatch_prompt", return_value=(
                     "# AGENTS.md Dynamic Context\nDISPATCH_PACKET",
                     {
                         "cli": "codex",
                         "packet_id": "packet-dispatch-receipt",
                         "provenance_hash": "abc123",
                         "proof_capsule": {"world_id": "world:dispatch-receipt"},
                         "world_id": "world:dispatch-receipt",
                         "size_report": {"under_budget": True},
                         "rules": [{"scope": "global", "path": "rules/global.md"}],
                         "refs": {"overall": [], "supervisor": [], "project": [], "phase": [], "task": []},
                     },
                 )), \
                 mock.patch.object(dispatch_module, "OrchConfig", return_value=SimpleNamespace(notify_cli_path="notify")), \
                 mock.patch.object(dispatch_module.subprocess, "run", return_value=SimpleNamespace(returncode=0, stderr="", stdout="")):
                try:
                    dispatch_module.dispatch(
                        worker="worker-codex",
                        task_id="dynctx::task",
                        description="dispatch receipt",
                        supervisor="conductor",
                    )
                    dispatch_ok = True
                except Exception as exc:
                    dispatch_ok = exc
            _check("dispatch succeeds when receipt sink fails", dispatch_ok is True, dispatch_ok)
    finally:
        os.environ.pop("ORCH_DECISION_RECEIPTS_ENABLED", None)


def main() -> int:
    _receipt_core_contract()
    _wake_packet_wiring_contract()
    _chat_wiring_contract()
    _wake_wiring_contract()
    _dispatch_wake_wiring_contract()
    _receipt_consumer_contract()
    _receipt_sink_fail_open_contract()
    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - decision receipts are typed, immutable, schema-complete, and wired.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
