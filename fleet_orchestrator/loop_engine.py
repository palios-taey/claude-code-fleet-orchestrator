from __future__ import annotations

import argparse
import ast
import copy
import datetime as dt
import json
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from fleet_orchestrator.config import OrchConfig, get_neo4j_driver, get_redis_sync


TRIGGER_KINDS = {"clock", "signal", "task-state"}
RETIREMENT_STATES = {"active", "retired", "superseded"}
OBSERVED_ARTIFACT_KINDS = {"file", "redis", "neo4j", "signed-status"}
COMPARISON_OPS = {"<", "<=", "==", "!=", ">=", ">"}
WRITABLE_ROOTS = {
    "cycle_state.cycle_n",
    "cycle_state.counters",
    "cycle_state.exclude_sets",
    "cycle_state.hold_list",
    "cycle_state.cooldowns",
    "cycle_state.ledger",
    "cycle_state.levers",
    "cycle_n",
    "counters",
    "exclude_sets",
    "hold_list",
    "cooldowns",
    "ledger",
    "levers",
}
TRUE_ENV_VALUES = {"1", "true", "yes", "on"}
NotifySender = Callable[[str, str, str], None]


class LoopDeclarationError(ValueError):
    pass


class ArtifactNotObservedError(RuntimeError):
    pass


class LoopPersistenceError(RuntimeError):
    pass


@dataclass(frozen=True)
class Step:
    step: str
    requires_artifact: Optional[Dict[str, Any]] = None
    produces_artifact: Optional[Dict[str, Any]] = None
    human_gate: bool = False
    writes_state: Tuple[Dict[str, Any], ...] = ()

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Step":
        if not isinstance(raw, dict):
            raise LoopDeclarationError("step_bundle entries must be objects")
        name = str(raw.get("step") or "").strip()
        if not name:
            raise LoopDeclarationError("step.step is required")
        writes = raw.get("writes_state") or raw.get("writes") or ()
        if isinstance(writes, dict):
            writes = (writes,)
        return cls(
            step=name,
            requires_artifact=_artifact_ref(raw.get("requires_artifact")),
            produces_artifact=_artifact_ref(raw.get("produces_artifact")),
            human_gate=bool(raw.get("human_gate", False)),
            writes_state=tuple(dict(item) for item in writes if isinstance(item, dict)),
        )


@dataclass(frozen=True)
class Trigger:
    kind: str
    clock_signal: Optional[str] = None
    signal: Optional[str] = None
    task_state: Optional[str] = None

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Trigger":
        if not isinstance(raw, dict):
            raise LoopDeclarationError("trigger must be an object")
        trigger = cls(
            kind=str(raw.get("kind") or "").strip(),
            clock_signal=_clean_optional(raw.get("clock_signal")),
            signal=_clean_optional(raw.get("signal")),
            task_state=_clean_optional(raw.get("task_state")),
        )
        trigger.validate()
        return trigger

    def validate(self) -> None:
        if self.kind not in TRIGGER_KINDS:
            raise LoopDeclarationError(f"trigger.kind must be one of {sorted(TRIGGER_KINDS)}")
        if self.kind == "clock":
            if self.clock_signal != "orch-watch-tick":
                raise LoopDeclarationError("clock loops require external clock_signal=orch-watch-tick")
        elif self.clock_signal:
            raise LoopDeclarationError("clock_signal is only valid for trigger.kind=clock")
        if self.kind == "signal" and not self.signal:
            raise LoopDeclarationError("signal loops require trigger.signal")
        if self.kind != "signal" and self.signal:
            raise LoopDeclarationError("trigger.signal is only valid for trigger.kind=signal")
        if self.kind == "task-state" and self.task_state != "predecessor-completed":
            raise LoopDeclarationError("task-state loops require task_state=predecessor-completed")
        if self.kind != "task-state" and self.task_state:
            raise LoopDeclarationError("task_state is only valid for trigger.kind=task-state")


@dataclass
class Loop:
    id: str
    owner: str
    step_bundle: List[Step]
    swap_slots: Dict[str, List[Any]]
    trigger: Trigger
    cycle_state: Dict[str, Any]
    stop_condition: Any
    retirement: str = "active"

    @classmethod
    def declare(cls, raw: Dict[str, Any]) -> "Loop":
        loop = cls.from_dict(raw)
        loop.validate()
        return loop

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Loop":
        if not isinstance(raw, dict):
            raise LoopDeclarationError("loop declaration must be an object")
        loop_id = str(raw.get("id") or "").strip()
        owner = str(raw.get("owner") or "").strip()
        if not loop_id:
            raise LoopDeclarationError("Loop.id is required")
        if not owner:
            raise LoopDeclarationError("Loop.owner is required")
        step_bundle = [Step.from_dict(item) for item in raw.get("step_bundle") or []]
        if not step_bundle:
            raise LoopDeclarationError("Loop.step_bundle is required")
        swap_slots = _normalize_swap_slots(raw.get("swap_slots") or {})
        return cls(
            id=loop_id,
            owner=owner,
            step_bundle=step_bundle,
            swap_slots=swap_slots,
            trigger=Trigger.from_dict(raw.get("trigger") or {}),
            cycle_state=_normalize_cycle_state(raw.get("cycle_state") or {}),
            stop_condition=raw.get("stop_condition"),
            retirement=str(raw.get("retirement") or "active").strip(),
        )

    def validate(self) -> None:
        if self.retirement not in RETIREMENT_STATES:
            raise LoopDeclarationError(f"retirement must be one of {sorted(RETIREMENT_STATES)}")
        if not meetable(self.stop_condition, self.step_bundle, self.cycle_state):
            raise LoopDeclarationError("stop_condition is not meetable")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "owner": self.owner,
            "step_bundle": [
                {
                    "step": step.step,
                    "requires_artifact": step.requires_artifact,
                    "produces_artifact": step.produces_artifact,
                    "human_gate": step.human_gate,
                    "writes_state": list(step.writes_state),
                }
                for step in self.step_bundle
            ],
            "swap_slots": copy.deepcopy(self.swap_slots),
            "trigger": {
                "kind": self.trigger.kind,
                "clock_signal": self.trigger.clock_signal,
                "signal": self.trigger.signal,
                "task_state": self.trigger.task_state,
            },
            "cycle_state": copy.deepcopy(self.cycle_state),
            "stop_condition": copy.deepcopy(self.stop_condition),
            "retirement": self.retirement,
        }

    def ready_steps(self, artifact_store: "ArtifactStore") -> List[Step]:
        if self.retirement != "active":
            return []
        ready: List[Step] = []
        for step in self.step_bundle:
            required = step.requires_artifact
            if not required or artifact_store.present(required):
                ready.append(step)
        return ready

    def advance_step(self, step_name: str, artifact_store: "ArtifactStore") -> Dict[str, Any]:
        step = next((item for item in self.step_bundle if item.step == step_name), None)
        if step is None:
            raise ValueError(f"unknown step: {step_name}")
        if step.requires_artifact and not artifact_store.present(step.requires_artifact):
            raise ArtifactNotObservedError(f"required artifact absent for step {step_name}: {step.requires_artifact}")
        entry = {
            "at": _utc_now_iso(),
            "step": step.step,
            "requires_artifact": copy.deepcopy(step.requires_artifact),
            "produces_artifact": copy.deepcopy(step.produces_artifact),
        }
        self.cycle_state.setdefault("ledger", []).append(entry)
        _apply_step_writes(self.cycle_state, step)
        return entry

    def should_stop(self) -> bool:
        return bool(evaluate_stop_condition(self.stop_condition, self.cycle_state))

    def retire(self, state: str, reason: str, persistence: "CycleStateStore") -> Dict[str, Any]:
        if state not in {"retired", "superseded"}:
            raise ValueError("retirement state must be retired or superseded")
        receipt = {
            "loop_id": self.id,
            "owner": self.owner,
            "previous_state": self.retirement,
            "retirement": state,
            "reason": reason,
            "retired_at": _utc_now_iso(),
            "cycle_n": self.cycle_state.get("cycle_n"),
        }
        self.retirement = state
        self.cycle_state.setdefault("ledger", []).append({"retirement_receipt": receipt})
        persistence.save(self)
        persistence.save_retirement_receipt(self.id, receipt)
        return receipt


class ArtifactStore:
    def __init__(self, config: Optional[OrchConfig] = None):
        self.config = config

    def present(self, artifact: Dict[str, Any]) -> bool:
        ref = _artifact_ref(artifact)
        if not ref:
            return False
        kind = ref["kind"]
        if kind == "file":
            path = Path(str(ref.get("path") or "")).expanduser()
            return path.is_file()
        if kind == "redis":
            r = get_redis_sync(self.config)
            return bool(r.exists(str(ref.get("key") or "")))
        if kind in {"neo4j", "signed-status"}:
            return self._neo4j_present(ref)
        raise LoopDeclarationError(f"unsupported artifact kind: {kind}")

    def _neo4j_present(self, ref: Dict[str, Any]) -> bool:
        cfg = self.config or OrchConfig()
        query = ref.get("query")
        params = ref.get("params") or {}
        if query:
            # A presence check must be READ-ONLY. Reject mutating Cypher so a
            # declared/injected query can't write through the orchestrator's DB
            # credentials (review gate 2: _neo4j_present executed arbitrary Cypher).
            if re.search(r"\b(CREATE|MERGE|DELETE|DETACH|SET|REMOVE|DROP|CALL|LOAD\s+CSV)\b",
                         str(query), re.IGNORECASE):
                raise LoopDeclarationError("neo4j artifact query must be read-only (no write/CALL clauses)")
        else:
            label = str(ref.get("label") or "").strip()
            prop = str(ref.get("property") or "id").strip()
            value = ref.get("value")
            if not label or value is None:
                raise LoopDeclarationError("neo4j artifacts require query or label/property/value")
            # Validate identifiers before interpolation — close the injection sink
            # (only `value` was parameterized; label/property were interpolated raw).
            if not re.fullmatch(r"[A-Za-z0-9_]+", label) or not re.fullmatch(r"[A-Za-z0-9_]+", prop):
                raise LoopDeclarationError("neo4j label/property must be plain identifiers")
            query = f"MATCH (n:{label} {{{prop}: $value}}) RETURN count(n) AS c"
            params = {"value": value}
        driver = get_neo4j_driver(cfg)
        with driver.session(database=cfg.neo4j_db) as session:
            record = session.run(str(query), **dict(params)).single()
        if not record:
            return False
        if "c" in record:
            return int(record["c"] or 0) > 0
        return True


class CycleStateStore:
    def save(self, loop: Loop) -> None:
        raise NotImplementedError

    def load(self, loop_id: str) -> Optional[Dict[str, Any]]:
        raise NotImplementedError

    def save_retirement_receipt(self, loop_id: str, receipt: Dict[str, Any]) -> None:
        raise NotImplementedError


class RedisCycleStateStore(CycleStateStore):
    def __init__(self, config: Optional[OrchConfig] = None, prefix: str = "orch:loop"):
        self.config = config
        self.prefix = prefix

    def _key(self, loop_id: str) -> str:
        return f"{self.prefix}:{loop_id}"

    def _receipt_key(self, loop_id: str) -> str:
        return f"{self.prefix}:{loop_id}:retirement_receipts"

    def save(self, loop: Loop) -> None:
        r = get_redis_sync(self.config)
        r.set(self._key(loop.id), json.dumps(loop.to_dict(), separators=(",", ":"), sort_keys=True))

    def load(self, loop_id: str) -> Optional[Dict[str, Any]]:
        r = get_redis_sync(self.config)
        raw = r.get(self._key(loop_id))
        if not raw:
            return None
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        return json.loads(raw)

    def save_retirement_receipt(self, loop_id: str, receipt: Dict[str, Any]) -> None:
        r = get_redis_sync(self.config)
        r.rpush(self._receipt_key(loop_id), json.dumps(receipt, separators=(",", ":"), sort_keys=True))


class Neo4jCycleStateStore(CycleStateStore):
    def __init__(self, config: Optional[OrchConfig] = None):
        self.config = config

    def save(self, loop: Loop) -> None:
        cfg = self.config or OrchConfig()
        payload = loop.to_dict()
        driver = get_neo4j_driver(cfg)
        with driver.session(database=cfg.neo4j_db) as session:
            session.run(
                """
                MERGE (l:OrchLoop {id: $id})
                SET l.owner = $owner,
                    l.retirement = $retirement,
                    l.step_bundle = $step_bundle,
                    l.swap_slots = $swap_slots,
                    l.trigger = $trigger,
                    l.cycle_state = $cycle_state,
                    l.stop_condition = $stop_condition,
                    l.updated_at = $updated_at
                """,
                id=loop.id,
                owner=loop.owner,
                retirement=loop.retirement,
                step_bundle=json.dumps(payload["step_bundle"], separators=(",", ":"), sort_keys=True),
                swap_slots=json.dumps(payload["swap_slots"], separators=(",", ":"), sort_keys=True),
                trigger=json.dumps(payload["trigger"], separators=(",", ":"), sort_keys=True),
                cycle_state=json.dumps(payload["cycle_state"], separators=(",", ":"), sort_keys=True),
                stop_condition=json.dumps(payload["stop_condition"], separators=(",", ":"), sort_keys=True),
                updated_at=_utc_now_iso(),
            )

    def load(self, loop_id: str) -> Optional[Dict[str, Any]]:
        cfg = self.config or OrchConfig()
        driver = get_neo4j_driver(cfg)
        with driver.session(database=cfg.neo4j_db) as session:
            record = session.run("MATCH (l:OrchLoop {id: $id}) RETURN l", id=loop_id).single()
        if not record:
            return None
        node = dict(record["l"])
        return {
            "id": node.get("id"),
            "owner": node.get("owner"),
            "step_bundle": json.loads(node.get("step_bundle") or "[]"),
            "swap_slots": json.loads(node.get("swap_slots") or "{}"),
            "trigger": json.loads(node.get("trigger") or "{}"),
            "cycle_state": json.loads(node.get("cycle_state") or "{}"),
            "stop_condition": json.loads(node.get("stop_condition") or "null"),
            "retirement": node.get("retirement") or "active",
        }

    def save_retirement_receipt(self, loop_id: str, receipt: Dict[str, Any]) -> None:
        cfg = self.config or OrchConfig()
        driver = get_neo4j_driver(cfg)
        with driver.session(database=cfg.neo4j_db) as session:
            session.run(
                """
                MATCH (l:OrchLoop {id: $loop_id})
                CREATE (r:OrchLoopRetirementReceipt {id: $receipt_id})
                SET r.payload = $payload,
                    r.created_at = $created_at
                MERGE (l)-[:HAS_RETIREMENT_RECEIPT]->(r)
                """,
                loop_id=loop_id,
                receipt_id=f"{loop_id}:{receipt['retired_at']}",
                payload=json.dumps(receipt, separators=(",", ":"), sort_keys=True),
                created_at=_utc_now_iso(),
            )


def meetable(stop_condition: Any, step_bundle: Iterable[Any], cycle_state: Optional[Dict[str, Any]] = None) -> bool:
    try:
        refs = _condition_refs(stop_condition)
        if not refs:
            return False
        writes = _declared_writes(step_bundle)
        normalized_refs = {_normalize_ref(ref) for ref in refs}
        if not _all_refs_are_cycle_state(normalized_refs):
            return bool(_has_timeout_or_escalation(stop_condition))
        if not _all_refs_have_writers(normalized_refs, writes):
            return False
        if _has_increment_vs_floating(stop_condition, writes):
            return False
        return True
    except Exception:
        return False


def evaluate_stop_condition(stop_condition: Any, cycle_state: Dict[str, Any]) -> bool:
    if isinstance(stop_condition, dict):
        if "all" in stop_condition:
            return all(evaluate_stop_condition(item, cycle_state) for item in stop_condition["all"])
        if "any" in stop_condition:
            return any(evaluate_stop_condition(item, cycle_state) for item in stop_condition["any"])
        if "not" in stop_condition:
            return not evaluate_stop_condition(stop_condition["not"], cycle_state)
        if "var" in stop_condition and "op" in stop_condition:
            left = _get_state_value(cycle_state, _strip_cycle_state(str(stop_condition["var"])))
            right = stop_condition.get("value")
            if "other_var" in stop_condition:
                right = _get_state_value(cycle_state, _strip_cycle_state(str(stop_condition["other_var"])))
            return _compare(left, str(stop_condition["op"]), right)
    if isinstance(stop_condition, str):
        return bool(_SafeEvaluator(cycle_state).visit(ast.parse(stop_condition, mode="eval")))
    raise ValueError("unsupported stop_condition shape")


def adversarial_meetable_cases() -> Dict[str, bool]:
    cases = {
        "external-approval-no-timeout": {
            "stop_condition": {"var": "external.approval", "op": "==", "value": True},
            "step_bundle": [{"step": "gate", "human_gate": True, "writes_state": []}],
            "cycle_state": {},
        },
        "increment-vs-floating": {
            "stop_condition": {"var": "cycle_state.counters.surface.used", "op": ">=", "other_var": "cycle_state.levers.dynamic_threshold"},
            "step_bundle": [
                {"step": "post", "writes_state": [{"var": "cycle_state.counters.surface.used", "mode": "increment"}]},
                {"step": "tune", "writes_state": [{"var": "cycle_state.levers.dynamic_threshold", "mode": "external"}]},
            ],
            "cycle_state": {"counters": {"surface": {"used": 0}}, "levers": {"dynamic_threshold": 3}},
        },
        "var-no-step-writes": {
            "stop_condition": {"var": "cycle_state.counters.surface.used", "op": ">=", "value": 3},
            "step_bundle": [{"step": "observe", "writes_state": []}],
            "cycle_state": {"counters": {"surface": {"used": 0}}},
        },
    }
    return {
        name: meetable(case["stop_condition"], case["step_bundle"], case["cycle_state"])
        for name, case in cases.items()
    }


def loops_enabled() -> bool:
    return os.environ.get("ORCH_LOOPS_ENABLED", "").strip().lower() in TRUE_ENV_VALUES


def declare_loop(raw: Dict[str, Any], persistence: Optional[CycleStateStore] = None) -> Dict[str, Any]:
    if not loops_enabled():
        return {"ok": True, "enabled": False}
    loop = Loop.declare(raw)
    if persistence is not None:
        persistence.save(loop)
    return {"ok": True, "enabled": True, "loop": loop.to_dict()}


def advance_loop_step(
    loop_or_raw: Any,
    step_name: str,
    artifact_store: Optional[ArtifactStore] = None,
    persistence: Optional[CycleStateStore] = None,
    wake_target: Optional[str] = None,
    wake_message: Optional[str] = None,
    notify_sender: Optional[NotifySender] = None,
) -> Dict[str, Any]:
    if not loops_enabled():
        return {"ok": True, "enabled": False}
    loop = loop_or_raw if isinstance(loop_or_raw, Loop) else Loop.declare(loop_or_raw)
    entry = loop.advance_step(step_name, artifact_store or ArtifactStore())
    if persistence is not None:
        persistence.save(loop)
    if wake_target:
        send_loop_wake(
            wake_target,
            wake_message or f"LOOP ADVANCE [{loop.id}]: step={step_name}",
            notify_sender=notify_sender,
        )
    return {
        "ok": True,
        "enabled": True,
        "loop": loop.to_dict(),
        "entry": entry,
        "should_stop": loop.should_stop(),
    }


def send_loop_wake(
    target: str,
    message: str,
    notify_type: str = "command",
    notify_sender: Optional[NotifySender] = None,
) -> None:
    target = target.strip()
    message = message.strip()
    if not target:
        raise LoopDeclarationError("wake target must be non-empty")
    if not message:
        raise LoopDeclarationError("wake message must be non-empty")
    if notify_type not in {"standard", "escalation", "command", "response_ready"}:
        raise LoopDeclarationError("wake notify_type must be standard, escalation, command, or response_ready")
    if notify_sender is not None:
        notify_sender(target, message, notify_type)
        return
    result = subprocess.run(
        ["taey-notify", target, message, "--type", notify_type],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0:
        raise LoopPersistenceError(result.stderr.strip() or "taey-notify failed")


def _artifact_ref(raw: Any) -> Optional[Dict[str, Any]]:
    if raw in (None, "", False):
        return None
    if isinstance(raw, str):
        return {"kind": "file", "path": raw}
    if not isinstance(raw, dict):
        raise LoopDeclarationError("artifact references must be objects or file paths")
    ref = dict(raw)
    kind = str(ref.get("kind") or "").strip()
    if kind not in OBSERVED_ARTIFACT_KINDS:
        raise LoopDeclarationError(f"artifact kind must be one of {sorted(OBSERVED_ARTIFACT_KINDS)}")
    ref["kind"] = kind
    return ref


def _clean_optional(value: Any) -> Optional[str]:
    if value in (None, ""):
        return None
    return str(value).strip()


def _normalize_swap_slots(raw: Dict[str, Any]) -> Dict[str, List[Any]]:
    if not isinstance(raw, dict):
        raise LoopDeclarationError("swap_slots must be an object")
    return {
        "refs": list(raw.get("refs") or []),
        "packets": list(raw.get("packets") or []),
        "targets": list(raw.get("targets") or []),
    }


def _normalize_cycle_state(raw: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(raw, dict):
        raise LoopDeclarationError("cycle_state must be an object")
    state = copy.deepcopy(raw)
    state.setdefault("cycle_n", 0)
    state.setdefault("counters", {})
    state.setdefault("exclude_sets", {})
    state.setdefault("hold_list", [])
    state.setdefault("cooldowns", {})
    state.setdefault("ledger", [])
    state.setdefault("levers", {})
    return state


def _declared_writes(step_bundle: Iterable[Any]) -> Dict[str, Dict[str, Any]]:
    writes: Dict[str, Dict[str, Any]] = {}
    for raw_step in step_bundle:
        step = raw_step if isinstance(raw_step, Step) else Step.from_dict(raw_step)
        for write in step.writes_state:
            var = _normalize_ref(str(write.get("var") or ""))
            if var:
                writes[var] = dict(write)
        if step.produces_artifact and step.produces_artifact.get("state_var"):
            writes[_normalize_ref(str(step.produces_artifact["state_var"]))] = {"mode": "set"}
    return writes


def _condition_refs(condition: Any) -> Set[str]:
    if isinstance(condition, dict):
        refs: Set[str] = set()
        for key in ("all", "any"):
            for item in condition.get(key) or []:
                refs.update(_condition_refs(item))
        if "not" in condition:
            refs.update(_condition_refs(condition["not"]))
        for key in ("var", "other_var"):
            if condition.get(key):
                refs.add(str(condition[key]))
        return refs
    if isinstance(condition, str):
        tree = ast.parse(condition, mode="eval")
        return _ExpressionRefs().collect(tree)
    return set()


def _normalize_ref(ref: str) -> str:
    ref = ref.strip()
    if ref.startswith("cycle_state["):
        return "cycle_state." + ".".join(_subscript_parts(ref))
    return ref


def _all_refs_are_cycle_state(refs: Set[str]) -> bool:
    for ref in refs:
        if not any(ref == root or ref.startswith(f"{root}.") for root in WRITABLE_ROOTS):
            return False
    return True


def _all_refs_have_writers(refs: Set[str], writes: Dict[str, Dict[str, Any]]) -> bool:
    for ref in refs:
        if ref in {"cycle_state.cycle_n", "cycle_n"}:
            continue
        if not any(ref == written or ref.startswith(f"{written}.") or written.startswith(f"{ref}.") for written in writes):
            return False
    return True


def _has_timeout_or_escalation(condition: Any) -> bool:
    if isinstance(condition, dict):
        if condition.get("timeout_cycles") is not None or condition.get("escalation"):
            return True
        return any(_has_timeout_or_escalation(item) for key in ("all", "any") for item in condition.get(key) or [])
    return False


def _has_increment_vs_floating(condition: Any, writes: Dict[str, Dict[str, Any]]) -> bool:
    comparisons = _condition_comparisons(condition)
    for left, op, right in comparisons:
        if op not in {">", ">=", "<", "<="}:
            continue
        left_write = writes.get(_normalize_ref(left), {})
        if left_write.get("mode") == "increment" and isinstance(right, str):
            right_write = writes.get(_normalize_ref(right), {})
            if right_write.get("mode") in {"external", "floating", "unbounded", "increment"}:
                return True
            if not right_write and _normalize_ref(right) not in left_write:
                return True
    return False


def _condition_comparisons(condition: Any) -> List[Tuple[str, str, Any]]:
    if isinstance(condition, dict):
        found: List[Tuple[str, str, Any]] = []
        if "var" in condition and "op" in condition:
            right: Any = condition.get("other_var") if "other_var" in condition else condition.get("value")
            found.append((str(condition["var"]), str(condition["op"]), right))
        for key in ("all", "any"):
            for item in condition.get(key) or []:
                found.extend(_condition_comparisons(item))
        if "not" in condition:
            found.extend(_condition_comparisons(condition["not"]))
        return found
    if isinstance(condition, str):
        return _ExpressionRefs().comparisons(ast.parse(condition, mode="eval"))
    return []


def _apply_step_writes(cycle_state: Dict[str, Any], step: Step) -> None:
    for write in step.writes_state:
        var = _strip_cycle_state(str(write.get("var") or ""))
        if not var:
            continue
        mode = str(write.get("mode") or "set")
        if mode == "increment":
            current = _get_state_value(cycle_state, var, 0)
            _set_state_value(cycle_state, var, int(current or 0) + int(write.get("amount", 1)))
        elif mode == "append":
            current = _get_state_value(cycle_state, var, [])
            if not isinstance(current, list):
                raise LoopDeclarationError(f"append target is not a list: {var}")
            current.append(write.get("value"))
        elif mode == "set":
            _set_state_value(cycle_state, var, write.get("value"))


def _strip_cycle_state(ref: str) -> str:
    ref = _normalize_ref(ref)
    return ref[len("cycle_state."):] if ref.startswith("cycle_state.") else ref


def _get_state_value(cycle_state: Dict[str, Any], ref: str, default: Any = None) -> Any:
    current: Any = cycle_state
    for part in [item for item in ref.split(".") if item]:
        if isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return default
    return current


def _set_state_value(cycle_state: Dict[str, Any], ref: str, value: Any) -> None:
    parts = [item for item in ref.split(".") if item]
    current = cycle_state
    for part in parts[:-1]:
        current = current.setdefault(part, {})
    current[parts[-1]] = value


def _compare(left: Any, op: str, right: Any) -> bool:
    if op not in COMPARISON_OPS:
        raise ValueError(f"unsupported comparison operator: {op}")
    if op == "<":
        return left < right
    if op == "<=":
        return left <= right
    if op == "==":
        return left == right
    if op == "!=":
        return left != right
    if op == ">=":
        return left >= right
    return left > right


def _subscript_parts(ref: str) -> List[str]:
    try:
        node = ast.parse(ref, mode="eval").body
    except SyntaxError:
        return []
    parts: List[str] = []
    while isinstance(node, ast.Subscript):
        slice_node = node.slice
        if isinstance(slice_node, ast.Constant):
            parts.append(str(slice_node.value))
        node = node.value
    return list(reversed(parts))


def _utc_now_iso() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat()


class _ExpressionRefs(ast.NodeVisitor):
    def collect(self, tree: ast.AST) -> Set[str]:
        self.refs: Set[str] = set()
        self.found_comparisons: List[Tuple[str, str, Any]] = []
        self.visit(tree)
        return self.refs

    def comparisons(self, tree: ast.AST) -> List[Tuple[str, str, Any]]:
        self.refs: Set[str] = set()
        self.found_comparisons: List[Tuple[str, str, Any]] = []
        self.visit(tree)
        return self.found_comparisons

    def visit_Compare(self, node: ast.Compare) -> Any:
        left = self._ref(node.left)
        for op_node, comparator in zip(node.ops, node.comparators):
            op = _ast_op(op_node)
            right_ref = self._ref(comparator)
            if left and op:
                self.found_comparisons.append((left, op, right_ref if right_ref else None))
            if right_ref:
                left = right_ref
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> Any:
        self.refs.add(node.id)

    def visit_Attribute(self, node: ast.Attribute) -> Any:
        ref = self._ref(node)
        if ref:
            self.refs.add(ref)

    def visit_Subscript(self, node: ast.Subscript) -> Any:
        ref = self._ref(node)
        if ref:
            self.refs.add(ref)
        self.generic_visit(node)

    def _ref(self, node: ast.AST) -> Optional[str]:
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            parent = self._ref(node.value)
            return f"{parent}.{node.attr}" if parent else node.attr
        if isinstance(node, ast.Subscript):
            parent = self._ref(node.value)
            if not parent:
                return None
            if isinstance(node.slice, ast.Constant):
                return f"{parent}.{node.slice.value}"
        return None


class _SafeEvaluator(ast.NodeVisitor):
    def __init__(self, cycle_state: Dict[str, Any]):
        self.cycle_state = cycle_state

    def visit_Expression(self, node: ast.Expression) -> Any:
        return self.visit(node.body)

    def visit_BoolOp(self, node: ast.BoolOp) -> bool:
        values = [bool(self.visit(item)) for item in node.values]
        if isinstance(node.op, ast.And):
            return all(values)
        if isinstance(node.op, ast.Or):
            return any(values)
        raise ValueError("unsupported bool operator")

    def visit_UnaryOp(self, node: ast.UnaryOp) -> bool:
        if isinstance(node.op, ast.Not):
            return not bool(self.visit(node.operand))
        raise ValueError("unsupported unary operator")

    def visit_Compare(self, node: ast.Compare) -> bool:
        left = self.visit(node.left)
        for op_node, comparator in zip(node.ops, node.comparators):
            right = self.visit(comparator)
            if not _compare(left, _ast_op(op_node), right):
                return False
            left = right
        return True

    def visit_Name(self, node: ast.Name) -> Any:
        if node.id == "cycle_state":
            return self.cycle_state
        return _get_state_value(self.cycle_state, node.id)

    def visit_Attribute(self, node: ast.Attribute) -> Any:
        value = self.visit(node.value)
        if isinstance(value, dict):
            return value.get(node.attr)
        raise ValueError("attribute access is only supported on cycle_state dicts")

    def visit_Subscript(self, node: ast.Subscript) -> Any:
        value = self.visit(node.value)
        key = self.visit(node.slice)
        return value[key]

    def visit_Constant(self, node: ast.Constant) -> Any:
        return node.value

    def generic_visit(self, node: ast.AST) -> Any:
        raise ValueError(f"unsupported stop_condition expression: {type(node).__name__}")


def _ast_op(op_node: ast.AST) -> str:
    if isinstance(op_node, ast.Lt):
        return "<"
    if isinstance(op_node, ast.LtE):
        return "<="
    if isinstance(op_node, ast.Eq):
        return "=="
    if isinstance(op_node, ast.NotEq):
        return "!="
    if isinstance(op_node, ast.GtE):
        return ">="
    if isinstance(op_node, ast.Gt):
        return ">"
    raise ValueError("unsupported comparison operator")


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Orchestrator loop engine")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("adversarial-meetable", help="Run spec adversarial meetability checks")
    declare_parser = sub.add_parser("declare", help="Validate and persist a loop declaration JSON file")
    declare_parser.add_argument("path")
    declare_parser.add_argument("--store", choices=("redis", "neo4j"), default="redis")
    args = parser.parse_args(argv)

    if args.command == "adversarial-meetable":
        for name, result in adversarial_meetable_cases().items():
            print(f"{name}: {'accept' if result else 'reject'}")
        return 0

    if args.command == "declare":
        payload = json.loads(Path(args.path).read_text(encoding="utf-8"))
        loop = Loop.declare(payload)
        store: CycleStateStore = RedisCycleStateStore() if args.store == "redis" else Neo4jCycleStateStore()
        store.save(loop)
        print(f"declared {loop.id} owner={loop.owner} store={args.store}")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main())
