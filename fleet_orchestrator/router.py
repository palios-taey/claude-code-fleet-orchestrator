"""Survival-aware advisory routing policy for fleet lanes.

The router is deliberately a sibling to ``dispatch.py``: it chooses a worker
identifier and returns a receipt, but it never claims tasks, writes
``current_task``, or sends wake packets. The execution primitive stays in
``dispatch.py``.

V1 is a deterministic rule-router with bandit-ready survival scoring. It keeps
the hard constitutional and reservation constraints ahead of the score so the
policy cannot cost-optimize through them.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Optional, Sequence

from fleet_orchestrator.lane_state import LaneRef, LaneState, estimate_lanes


SACRED_TRUST_THRESHOLD = 0.809
DEFAULT_COMPETENCE_REQUIRED = 0.50
DEFAULT_SURVIVAL_THRESHOLD = 0.30
DEFAULT_CONFIDENCE_THRESHOLD = 0.25
DEFAULT_EXPLORATION_WEIGHT = 0.08
ROUTER_POLICY_VERSION = "rule-router-v1"

LIBERTY_TRINITY_DOMAINS = frozenset(
    {
        "child_protection",
        "child_safety",
        "anti_trafficking",
        "trafficking",
        "anti_slavery",
        "slavery_eradication",
        "earth_stewardship",
        "earth_first_person_data",
    }
)

_CHILD_RE = re.compile(r"\b(child|children|minor|minors|csam|csem|child[- ]?abuse|child[- ]?safety)\b", re.I)
_TRAFFICKING_RE = re.compile(r"\b(traffick(?:ing|ed)?|forced[- ]labor|coerced[- ]labor|slavery|enslavement)\b", re.I)
_EARTH_RE = re.compile(
    r"\b(earth[- ]?stewardship|first[- ]person earth data|ecological harm|biosphere|environmental harm)\b",
    re.I,
)
_REROUTE_RE = re.compile(
    r"\b(pre[- ]?compact|context window|context limit|compaction|rate[- ]?limit|usage limit|cooldown|too many requests|send button disabled)\b",
    re.I,
)


@dataclass(frozen=True)
class LaneProfile:
    """Static lane traits supplied by config, hints, or a session id."""

    worker_id: str
    lane_id: str
    model_family: str = "unknown"
    surface: str = "session"
    competence: float = DEFAULT_COMPETENCE_REQUIRED
    fidelity: float = DEFAULT_COMPETENCE_REQUIRED
    cost: float = 1.0
    latency_s: Optional[float] = None
    context_tokens: Optional[int] = None
    supports_tools: bool = True
    reserved_for: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_value(cls, value: str | Mapping[str, Any] | "LaneProfile") -> "LaneProfile":
        if isinstance(value, LaneProfile):
            return value
        if isinstance(value, str):
            return _profile_from_worker(value)
        worker = _first_string(value, "worker_id", "worker", "session", "id", "lane_id")
        if not worker:
            raise ValueError("lane profile requires worker_id/worker/session/id/lane_id")
        lane_id = str(value.get("lane_id") or value.get("lane") or worker).strip()
        defaults = _profile_defaults(worker, lane_id=lane_id)
        competence = _float(value.get("competence"), defaults["competence"])
        fidelity = _float(value.get("fidelity"), competence)
        return cls(
            worker_id=worker,
            lane_id=lane_id,
            model_family=str(value.get("model_family") or value.get("family") or defaults["model_family"]),
            surface=str(value.get("surface") or defaults["surface"]),
            competence=_clamp01(competence),
            fidelity=_clamp01(fidelity),
            cost=max(0.0, _float(value.get("cost"), defaults["cost"])),
            latency_s=_optional_float(value.get("latency_s") or value.get("latency")),
            context_tokens=_optional_int(value.get("context_tokens") or value.get("context_window")),
            supports_tools=_bool(value.get("supports_tools"), defaults["supports_tools"]),
            reserved_for=tuple(_string_list(value.get("reserved_for"))),
            tags=tuple(_string_list(value.get("tags"))),
            metadata=dict(value.get("metadata") or {}),
        )

    def to_receipt(self) -> dict[str, Any]:
        return {
            "worker_id": self.worker_id,
            "lane_id": self.lane_id,
            "model_family": self.model_family,
            "surface": self.surface,
            "competence": self.competence,
            "fidelity": self.fidelity,
            "cost": self.cost,
            "latency_s": self.latency_s,
            "context_tokens": self.context_tokens,
            "supports_tools": self.supports_tools,
            "reserved_for": list(self.reserved_for),
            "tags": list(self.tags),
        }


@dataclass(frozen=True)
class RouteDecision:
    action: str
    selected_worker: Optional[str]
    selected_lane_id: Optional[str]
    reason: str
    no_route: bool
    reroute: bool
    receipt: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action,
            "selected_worker": self.selected_worker,
            "selected_lane_id": self.selected_lane_id,
            "reason": self.reason,
            "no_route": self.no_route,
            "reroute": self.reroute,
            "receipt": self.receipt,
        }


@dataclass(frozen=True)
class _EvaluatedLane:
    profile: LaneProfile
    state: Optional[LaneState]
    survival: float
    survival_ucb: float
    confidence: float
    reasons: tuple[str, ...]

    @property
    def viable(self) -> bool:
        return not self.reasons

    def to_receipt(self) -> dict[str, Any]:
        item = self.profile.to_receipt()
        item.update(
            {
                "survival": self.survival,
                "survival_ucb": self.survival_ucb,
                "confidence": self.confidence,
                "available_now": self.state.available_now if self.state else None,
                "cooldown_remaining": self.state.cooldown_remaining if self.state else None,
                "ttl_distribution": dict(self.state.ttl_distribution) if self.state else None,
                "reasons": list(self.reasons),
            }
        )
        return item


def choose_worker(
    desc: str,
    hints: Optional[Mapping[str, Any]] = None,
    *,
    lanes: Optional[Iterable[str | Mapping[str, Any] | LaneProfile]] = None,
    lane_states: Optional[Mapping[str, LaneState | Mapping[str, Any]]] = None,
    redis_client: Any = None,
    now: Optional[float] = None,
    ui_probe: Any = None,
    prefix: Optional[str] = None,
) -> RouteDecision:
    """Choose the cheapest viable worker for ``desc`` and return the route receipt."""

    observed_at = time.time() if now is None else now
    route_hints = dict(hints or {})
    profiles = _profiles(lanes if lanes is not None else route_hints.get("lanes"))
    states = _lane_states(profiles, lane_states, redis_client=redis_client, now=observed_at, ui_probe=ui_probe, prefix=prefix)
    pin = _constitutional_pin(desc, route_hints)
    thresholds = _thresholds(route_hints, pin_active=pin is not None)

    evaluated = [
        _evaluate_lane(profile, states.get(profile.lane_id) or states.get(profile.worker_id), desc, route_hints, thresholds, pin)
        for profile in profiles
    ]

    if pin is not None:
        evaluated = _apply_highest_fidelity_pin(evaluated)
        candidates = [item for item in evaluated if item.viable]
        selected = max(candidates, key=lambda item: (item.profile.fidelity, item.profile.competence, item.survival_ucb, -item.profile.cost, item.profile.worker_id), default=None)
        if selected is None:
            return _decision("no_route", None, "constitutional pin fail-closed: no viable high-fidelity lane", True, False, desc, route_hints, thresholds, pin, evaluated)
        return _decision("route", selected, f"constitutional pin {pin['domain']} selected highest-fidelity viable lane", False, False, desc, route_hints, thresholds, pin, evaluated)

    viable = [item for item in evaluated if item.viable]
    selected = min(viable, key=lambda item: (item.profile.cost, -item.survival_ucb, -item.profile.competence, item.profile.worker_id), default=None)
    if selected is None:
        return _decision("no_route", None, "no viable lane passed competence, survival, confidence, and reservation filters", True, False, desc, route_hints, thresholds, pin, evaluated)
    return _decision("route", selected, "selected cheapest competent viable lane", False, False, desc, route_hints, thresholds, pin, evaluated)


def should_reroute(
    prompt: str,
    session: str,
    hints: Optional[Mapping[str, Any]] = None,
    *,
    lanes: Optional[Iterable[str | Mapping[str, Any] | LaneProfile]] = None,
    lane_states: Optional[Mapping[str, LaneState | Mapping[str, Any]]] = None,
    redis_client: Any = None,
    now: Optional[float] = None,
    ui_probe: Any = None,
    prefix: Optional[str] = None,
) -> RouteDecision:
    """Advise whether a UserPromptSubmit hook should block and re-dispatch."""

    observed_at = time.time() if now is None else now
    route_hints = dict(hints or {})
    profiles = _profiles(lanes if lanes is not None else route_hints.get("lanes"))
    states = _lane_states(profiles, lane_states, redis_client=redis_client, now=observed_at, ui_probe=ui_probe, prefix=prefix)
    current_state = states.get(session)
    current_profile = next((profile for profile in profiles if profile.worker_id == session or profile.lane_id == session), _profile_from_worker(session))
    pin = _constitutional_pin(prompt, route_hints)
    thresholds = _thresholds(route_hints, pin_active=pin is not None)
    current = _evaluate_lane(current_profile, current_state, prompt, route_hints, thresholds, pin)
    if pin is not None and current.profile.fidelity < max((profile.fidelity for profile in profiles), default=current.profile.fidelity):
        current = replace(current, reasons=tuple([*current.reasons, "not_highest_fidelity_pin_lane"]))
    prompt_risk = bool(_REROUTE_RE.search(prompt or ""))
    needs_reroute = prompt_risk or not current.viable

    if not needs_reroute:
        receipt = _receipt_base(prompt, route_hints, thresholds, pin, [current])
        receipt["current_session"] = session
        receipt["reroute_trigger"] = None
        return RouteDecision(
            action="stay",
            selected_worker=session,
            selected_lane_id=current_profile.lane_id,
            reason="current lane remains viable",
            no_route=False,
            reroute=False,
            receipt=receipt,
        )

    reroute_hints = dict(route_hints)
    excluded = set(_string_list(reroute_hints.get("exclude_workers")))
    excluded.add(session)
    reroute_hints["exclude_workers"] = sorted(excluded)
    decision = choose_worker(
        prompt,
        reroute_hints,
        lanes=profiles,
        lane_states=states,
        now=observed_at,
        ui_probe=ui_probe,
        prefix=prefix,
    )
    receipt = dict(decision.receipt)
    receipt["current_session"] = session
    receipt["current_lane"] = current.to_receipt()
    receipt["reroute_trigger"] = "prompt-risk" if prompt_risk else "current-lane-not-viable"
    action = "reroute" if decision.selected_worker else "no_route"
    return RouteDecision(
        action=action,
        selected_worker=decision.selected_worker,
        selected_lane_id=decision.selected_lane_id,
        reason=f"reroute triggered: {receipt['reroute_trigger']}",
        no_route=decision.no_route,
        reroute=bool(decision.selected_worker),
        receipt=receipt,
    )


def _profiles(values: Optional[Iterable[str | Mapping[str, Any] | LaneProfile]]) -> list[LaneProfile]:
    raw_values: Iterable[str | Mapping[str, Any] | LaneProfile]
    if values is None:
        raw_values = _session_ids_from_env()
    else:
        raw_values = values
    profiles: dict[str, LaneProfile] = {}
    for value in raw_values:
        profile = LaneProfile.from_value(value)
        profiles[profile.worker_id] = profile
    return sorted(profiles.values(), key=lambda item: (item.cost, item.worker_id))


def _lane_states(
    profiles: Sequence[LaneProfile],
    explicit: Optional[Mapping[str, LaneState | Mapping[str, Any]]],
    *,
    redis_client: Any,
    now: float,
    ui_probe: Any,
    prefix: Optional[str],
) -> dict[str, LaneState]:
    if explicit is not None:
        states: dict[str, LaneState] = {}
        for key, value in explicit.items():
            state = value if isinstance(value, LaneState) else _state_from_mapping(str(key), value)
            states[str(key)] = state
            states[state.lane_id] = state
        return states
    if not profiles or redis_client is None:
        return {}
    lane_refs = [LaneRef(lane_id=profile.lane_id, kind=profile.surface, session_id=profile.worker_id) for profile in profiles]
    return estimate_lanes(lane_refs, redis_client=redis_client, now=now, ui_probe=ui_probe, prefix=prefix)


def _evaluate_lane(
    profile: LaneProfile,
    state: Optional[LaneState],
    desc: str,
    hints: Mapping[str, Any],
    thresholds: Mapping[str, float],
    pin: Optional[dict[str, Any]],
) -> _EvaluatedLane:
    survival = _survival(state)
    confidence = _confidence(state)
    reasons: list[str] = []
    if profile.worker_id in set(_string_list(hints.get("exclude_workers"))):
        reasons.append("excluded_by_hints")
    if state is None:
        reasons.append("missing_lane_state")
    elif not state.available_now:
        reasons.append("lane_unavailable")
    elif state.cooldown_remaining > 0:
        reasons.append("cooldown_active")
    if _bool(hints.get("needs_tools"), False) and not profile.supports_tools:
        reasons.append("tools_required")
    context_tokens = _optional_int(hints.get("context_tokens"))
    if context_tokens and profile.context_tokens and context_tokens > profile.context_tokens:
        reasons.append("context_window_too_small")
    required = thresholds["competence_required"]
    if profile.competence < required:
        reasons.append("competence_below_required")
    if pin is not None and profile.fidelity < thresholds["fidelity_required"]:
        reasons.append("fidelity_below_sacred_trust_threshold")
    if not _reservation_allowed(profile, desc, hints, pin):
        reasons.append("reserved_lane_protected")
    if survival < thresholds["survival_threshold"]:
        reasons.append("survival_below_threshold")
    if confidence < thresholds["confidence_threshold"]:
        reasons.append("confidence_below_threshold")
    ucb = min(1.0, survival + thresholds["exploration_weight"] * math.sqrt(max(0.0, 1.0 - confidence)))
    return _EvaluatedLane(profile, state, round(survival, 6), round(ucb, 6), round(confidence, 6), tuple(reasons))


def _apply_highest_fidelity_pin(evaluated: Sequence[_EvaluatedLane]) -> list[_EvaluatedLane]:
    top_fidelity = max((item.profile.fidelity for item in evaluated), default=0.0)
    pinned: list[_EvaluatedLane] = []
    for item in evaluated:
        if item.profile.fidelity < top_fidelity:
            pinned.append(replace(item, reasons=tuple([*item.reasons, "not_highest_fidelity_pin_lane"])))
        else:
            pinned.append(item)
    return pinned


def _survival(state: Optional[LaneState]) -> float:
    if state is None or not state.available_now or state.cooldown_remaining > 0:
        return 0.0
    ttl = state.ttl_distribution or {}
    usable_mass = (
        float(ttl.get("gt_30m", 0.0))
        + 0.65 * float(ttl.get("5m_30m", 0.0))
        + 0.25 * float(ttl.get("lt_5m", 0.0))
        + 0.05 * float(ttl.get("unknown", 0.0))
    )
    return _clamp01(usable_mass * _confidence(state))


def _confidence(state: Optional[LaneState]) -> float:
    if state is None:
        return 0.0
    return _clamp01(float(state.confidence))


def _constitutional_pin(desc: str, hints: Mapping[str, Any]) -> Optional[dict[str, Any]]:
    domains = [normalize_domain(item) for item in _string_list(hints.get("domains") or hints.get("domain"))]
    for domain in domains:
        if domain in LIBERTY_TRINITY_DOMAINS:
            return {"active": True, "domain": domain, "source": "hint", "threshold": SACRED_TRUST_THRESHOLD}
    text = desc or ""
    if _CHILD_RE.search(text):
        return {"active": True, "domain": "child_protection", "source": "description", "threshold": SACRED_TRUST_THRESHOLD}
    if _TRAFFICKING_RE.search(text):
        return {"active": True, "domain": "anti_trafficking", "source": "description", "threshold": SACRED_TRUST_THRESHOLD}
    if _EARTH_RE.search(text):
        return {"active": True, "domain": "earth_stewardship", "source": "description", "threshold": SACRED_TRUST_THRESHOLD}
    return None


def normalize_domain(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value).strip().lower()).strip("_")


def _thresholds(hints: Mapping[str, Any], *, pin_active: bool) -> dict[str, float]:
    competence = _clamp01(_float(hints.get("competence_required") or hints.get("precision_required"), DEFAULT_COMPETENCE_REQUIRED))
    fidelity = _clamp01(_float(hints.get("fidelity_required"), competence))
    if pin_active:
        fidelity = max(fidelity, SACRED_TRUST_THRESHOLD)
        competence = max(competence, SACRED_TRUST_THRESHOLD)
    return {
        "competence_required": competence,
        "fidelity_required": fidelity,
        "survival_threshold": _clamp01(_float(hints.get("survival_threshold"), DEFAULT_SURVIVAL_THRESHOLD)),
        "confidence_threshold": _clamp01(_float(hints.get("confidence_threshold"), DEFAULT_CONFIDENCE_THRESHOLD)),
        "exploration_weight": max(0.0, _float(hints.get("exploration_weight"), DEFAULT_EXPLORATION_WEIGHT)),
    }


def _reservation_allowed(profile: LaneProfile, desc: str, hints: Mapping[str, Any], pin: Optional[dict[str, Any]]) -> bool:
    if not profile.reserved_for or pin is not None:
        return True
    requested = {normalize_domain(item) for item in _string_list(hints.get("reservation") or hints.get("reservation_tags") or hints.get("tags"))}
    requested.update(normalize_domain(item) for item in _string_list(hints.get("domain") or hints.get("domains")))
    haystack = " ".join([desc or "", *requested]).lower()
    for reservation in profile.reserved_for:
        normalized = normalize_domain(reservation)
        if normalized in requested or reservation.lower() in haystack:
            return True
    return False


def _decision(
    action: str,
    selected: Optional[_EvaluatedLane],
    reason: str,
    no_route: bool,
    reroute: bool,
    desc: str,
    hints: Mapping[str, Any],
    thresholds: Mapping[str, float],
    pin: Optional[dict[str, Any]],
    evaluated: Sequence[_EvaluatedLane],
) -> RouteDecision:
    receipt = _receipt_base(desc, hints, thresholds, pin, evaluated)
    if selected is not None:
        receipt["selected"] = selected.to_receipt()
    receipt["reason"] = reason
    return RouteDecision(
        action=action,
        selected_worker=selected.profile.worker_id if selected else None,
        selected_lane_id=selected.profile.lane_id if selected else None,
        reason=reason,
        no_route=no_route,
        reroute=reroute,
        receipt=receipt,
    )


def _receipt_base(
    desc: str,
    hints: Mapping[str, Any],
    thresholds: Mapping[str, float],
    pin: Optional[dict[str, Any]],
    evaluated: Sequence[_EvaluatedLane],
) -> dict[str, Any]:
    viable = [item for item in evaluated if item.viable]
    rejected = [item for item in evaluated if not item.viable]
    return {
        "policy": ROUTER_POLICY_VERSION,
        "desc_sha256": hashlib.sha256((desc or "").encode("utf-8")).hexdigest(),
        "desc_excerpt": (desc or "")[:180],
        "hints": _receipt_hints(hints),
        "pin": pin or {"active": False},
        "thresholds": dict(thresholds),
        "viable_lanes": [item.to_receipt() for item in viable],
        "rejected_lanes": [item.to_receipt() for item in rejected],
        "no_overkill_order": [item.profile.worker_id for item in sorted(viable, key=lambda lane: (lane.profile.cost, -lane.survival_ucb, -lane.profile.competence, lane.profile.worker_id))],
    }


def _receipt_hints(hints: Mapping[str, Any]) -> dict[str, Any]:
    allowed = {
        "domain",
        "domains",
        "tags",
        "reservation",
        "reservation_tags",
        "needs_tools",
        "context_tokens",
        "competence_required",
        "precision_required",
        "fidelity_required",
        "survival_threshold",
        "confidence_threshold",
        "exclude_workers",
    }
    return {key: hints[key] for key in sorted(allowed & set(hints))}


def _state_from_mapping(key: str, value: Mapping[str, Any]) -> LaneState:
    return LaneState(
        lane_id=str(value.get("lane_id") or key),
        available_now=_bool(value.get("available_now"), False),
        ttl_distribution={str(k): float(v) for k, v in dict(value.get("ttl_distribution") or {}).items()},
        cooldown_remaining=max(0.0, _float(value.get("cooldown_remaining"), 0.0)),
        confidence=_clamp01(_float(value.get("confidence"), 0.0)),
        observed_at=_float(value.get("observed_at"), time.time()),
        signals=dict(value.get("signals") or {}),
        source_freshness=dict(value.get("source_freshness") or {}),
    )


def _profile_from_worker(worker: str) -> LaneProfile:
    return LaneProfile(worker_id=str(worker).strip(), lane_id=str(worker).strip(), **_profile_defaults(str(worker).strip()))


def _profile_defaults(worker: str, *, lane_id: Optional[str] = None) -> dict[str, Any]:
    text = f"{worker} {lane_id or ''}".lower()
    if "local" in text or "moe" in text:
        return {"model_family": "local", "surface": "session", "competence": 0.35, "fidelity": 0.35, "cost": 0.05, "supports_tools": True}
    if "codex" in text:
        return {"model_family": "codex", "surface": "session", "competence": 0.72, "fidelity": 0.72, "cost": 0.35, "supports_tools": True}
    if "gemini" in text:
        return {"model_family": "gemini", "surface": "session", "competence": 0.78, "fidelity": 0.78, "cost": 0.40, "supports_tools": True}
    if "grok" in text:
        return {"model_family": "grok", "surface": "session", "competence": 0.82, "fidelity": 0.82, "cost": 0.55, "supports_tools": True}
    if "claude" in text or "opus" in text:
        return {"model_family": "claude", "surface": "session", "competence": 0.90, "fidelity": 0.90, "cost": 0.75, "supports_tools": True}
    if "chatgpt" in text or "gpt" in text:
        return {"model_family": "chatgpt", "surface": "chat", "competence": 0.88, "fidelity": 0.88, "cost": 0.70, "supports_tools": False}
    return {"model_family": "unknown", "surface": "session", "competence": 0.50, "fidelity": 0.50, "cost": 1.0, "supports_tools": True}


def _session_ids_from_env() -> list[str]:
    raw = os.environ.get("ORCH_SESSION_IDS", "")
    if not raw.strip():
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = raw.replace(";", ",").split(",")
    if isinstance(parsed, list):
        return [str(item).strip() for item in parsed if str(item).strip()]
    return []


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        if "," in value or ";" in value:
            return [item.strip() for item in value.replace(";", ",").split(",") if item.strip()]
        return [value.strip()] if value.strip() else []
    if isinstance(value, Iterable):
        return [str(item).strip() for item in value if str(item).strip()]
    return [str(value).strip()] if str(value).strip() else []


def _first_string(mapping: Mapping[str, Any], *keys: str) -> str:
    for key in keys:
        value = mapping.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _float(value: Any, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _optional_float(value: Any) -> Optional[float]:
    if value is None or value == "":
        return None
    return _float(value, 0.0)


def _optional_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _bool(value: Any, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    lowered = str(value).strip().lower()
    if lowered in {"1", "true", "yes", "y", "on"}:
        return True
    if lowered in {"0", "false", "no", "n", "off"}:
        return False
    return default


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, float(value)))
