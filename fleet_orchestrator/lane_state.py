"""Passive lane survival-state estimates for model-routing measurement.

This module deliberately does not choose a lane. It reads existing fleet
signals, estimates each lane's current survival state, and appends calibration
events that later policy code can consume.

STATUS (2026-06-15): a measurement SCAFFOLD shipped ahead of integration — NOT
yet wired. Nothing in the production tree imports it (only its acceptance test
does): no producer emits calibration events and no consumer reads the estimates.
It is intentionally additive/decoupled (passive-first v1); "added" means the
module + tests exist, NOT that the fleet is collecting data. Wiring producers
(notify-hook events -> observe) and consumers (routing policy) is a separate
tracked follow-up.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional

from fleet_orchestrator.config import OrchConfig
from fleet_orchestrator.notify_state import key as notify_key
from fleet_orchestrator.notify_state import key_prefix as _notify_key_prefix
from fleet_orchestrator.notify_state import redis_connect as notify_redis_connect
from fleet_orchestrator.notify_state import state_key as notify_state_key


TTL_BUCKETS = ("expired", "lt_5m", "5m_30m", "gt_30m", "unknown")
DEFAULT_STALE_SECONDS = 300.0
DEFAULT_COOLDOWN_SECONDS = 300.0
CALIBRATION_STREAM_SUFFIX = "lane_state:calibration"
_THROTTLE_RE = re.compile(
    r"\b(429|rate[- ]?limit|quota|usage limit|too many requests|try again|cooldown|temporar(?:y|ily)|unavailable)\b",
    re.IGNORECASE,
)
_COOLDOWN_RE = re.compile(r"\b(?:(\d+)\s*(h|hr|hour|hours|m|min|minute|minutes|s|sec|second|seconds))\b", re.IGNORECASE)
_MODEL_SELECTOR_RE = re.compile(r"current model is\s+(.+)$", re.IGNORECASE)
_SEND_RE = re.compile(r"\b(send|submit|arrow)\b", re.IGNORECASE)


@dataclass
class LaneRef:
    lane_id: str
    kind: str = "session"
    session_id: Optional[str] = None
    platform: Optional[str] = None
    display: Optional[str] = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def parse(cls, lane: str | "LaneRef") -> "LaneRef":
        if isinstance(lane, LaneRef):
            return lane
        raw = lane.strip()
        if raw.startswith("session:"):
            session = raw.split(":", 1)[1]
            return cls(lane_id=raw, kind="session", session_id=session)
        if raw.startswith("chat:"):
            parts = raw.split(":")
            platform = parts[1] if len(parts) > 1 else None
            display = None
            if "display" in parts:
                idx = parts.index("display")
                if idx + 1 < len(parts):
                    display = parts[idx + 1]
            session_id = raw
            return cls(lane_id=raw, kind="chat", session_id=session_id, platform=platform, display=display)
        return cls(lane_id=raw, kind="session", session_id=raw)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane_id": self.lane_id,
            "kind": self.kind,
            "session_id": self.session_id,
            "platform": self.platform,
            "display": self.display,
            "metadata": dict(self.metadata),
        }


@dataclass
class UISignal:
    observed_at: float
    available: Optional[bool] = None
    send_button_disabled: Optional[bool] = None
    limit_banner: Optional[str] = None
    cooldown_remaining: Optional[float] = None
    current_model: Optional[str] = None
    downgrade_detected: Optional[bool] = None
    raw: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], now: Optional[float] = None) -> "UISignal":
        return cls(
            observed_at=_float_or_none(value.get("observed_at")) or (time.time() if now is None else now),
            available=_optional_bool(value.get("available")),
            send_button_disabled=_optional_bool(value.get("send_button_disabled")),
            limit_banner=str(value["limit_banner"]) if value.get("limit_banner") else None,
            cooldown_remaining=_float_or_none(value.get("cooldown_remaining")),
            current_model=str(value["current_model"]) if value.get("current_model") else None,
            downgrade_detected=_optional_bool(value.get("downgrade_detected")),
            raw=dict(value.get("raw") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "observed_at": self.observed_at,
            "available": self.available,
            "send_button_disabled": self.send_button_disabled,
            "limit_banner": self.limit_banner,
            "cooldown_remaining": self.cooldown_remaining,
            "current_model": self.current_model,
            "downgrade_detected": self.downgrade_detected,
            "raw": dict(self.raw),
        }


@dataclass
class LaneState:
    lane_id: str
    available_now: bool
    ttl_distribution: dict[str, float]
    cooldown_remaining: float
    confidence: float
    observed_at: float
    signals: dict[str, Any] = field(default_factory=dict)
    source_freshness: dict[str, Optional[float]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane_id": self.lane_id,
            "available_now": self.available_now,
            "ttl_distribution": dict(self.ttl_distribution),
            "cooldown_remaining": self.cooldown_remaining,
            "confidence": self.confidence,
            "observed_at": self.observed_at,
            "signals": dict(self.signals),
            "source_freshness": dict(self.source_freshness),
        }


UIProbe = Callable[[LaneRef, float], Optional[UISignal | Mapping[str, Any]]]


def notify_key_prefix() -> str:
    return _notify_key_prefix()


def state_key(node_id: str, suffix: str, *, prefix: Optional[str] = None) -> str:
    return notify_state_key(node_id, suffix, prefix=prefix)


def lane_key(suffix: str, *, prefix: Optional[str] = None) -> str:
    return notify_key(suffix, prefix=prefix)


def calibration_stream_key(*, prefix: Optional[str] = None) -> str:
    return lane_key(CALIBRATION_STREAM_SUFFIX, prefix=prefix)


def estimate_lane(
    lane: str | LaneRef,
    *,
    redis_client: Any = None,
    config: Optional[OrchConfig] = None,
    now: Optional[float] = None,
    ui_probe: Optional[UIProbe] = None,
    prefix: Optional[str] = None,
) -> LaneState:
    observed_at = time.time() if now is None else now
    lane_ref = LaneRef.parse(lane)
    client = redis_client or notify_redis_connect()
    session_signals = read_session_signals(lane_ref, client, now=observed_at, prefix=prefix)
    ui_signal = _read_ui_signal(lane_ref, observed_at, ui_probe)

    cooldown_remaining = max(
        _cooldown_from_redis(lane_ref, client, observed_at, prefix=prefix),
        _cooldown_from_last_outcome(session_signals.get("last_outcome"), observed_at),
        float(ui_signal.cooldown_remaining or 0.0) if ui_signal else 0.0,
    )
    tool_running = _truthy(session_signals.get("tool_running"))
    ui_unavailable = bool(ui_signal and (
        ui_signal.available is False
        or ui_signal.send_button_disabled is True
        or ui_signal.limit_banner
        or ui_signal.downgrade_detected is True
    ))
    available_now = cooldown_remaining <= 0 and not tool_running and not ui_unavailable
    confidence, freshness = _confidence(session_signals, ui_signal, observed_at)
    ttl_distribution = _ttl_distribution(
        available_now=available_now,
        cooldown_remaining=cooldown_remaining,
        confidence=confidence,
        ui_signal=ui_signal,
        tool_running=tool_running,
    )
    signals: dict[str, Any] = {
        "session": _jsonable(session_signals),
        "ui": ui_signal.to_dict() if ui_signal else None,
        "lane": lane_ref.to_dict(),
    }
    return LaneState(
        lane_id=lane_ref.lane_id,
        available_now=available_now,
        ttl_distribution=ttl_distribution,
        cooldown_remaining=round(cooldown_remaining, 3),
        confidence=confidence,
        observed_at=observed_at,
        signals=signals,
        source_freshness=freshness,
    )


def estimate_lanes(
    lanes: Iterable[str | LaneRef],
    *,
    redis_client: Any = None,
    config: Optional[OrchConfig] = None,
    now: Optional[float] = None,
    ui_probe: Optional[UIProbe] = None,
    prefix: Optional[str] = None,
) -> dict[str, LaneState]:
    observed_at = time.time() if now is None else now
    client = redis_client or notify_redis_connect()
    result: dict[str, LaneState] = {}
    for lane in lanes:
        state = estimate_lane(lane, redis_client=client, now=observed_at, ui_probe=ui_probe, prefix=prefix)
        result[state.lane_id] = state
    return result


def read_session_signals(
    lane: str | LaneRef,
    redis_client: Any,
    *,
    now: Optional[float] = None,
    prefix: Optional[str] = None,
) -> dict[str, Any]:
    lane_ref = LaneRef.parse(lane)
    node_id = lane_ref.session_id or lane_ref.lane_id
    observed_at = time.time() if now is None else now
    raw = {
        "idle": redis_client.get(state_key(node_id, "idle", prefix=prefix)),
        "tool_running": redis_client.get(state_key(node_id, "tool_running", prefix=prefix)),
        "last_activity": redis_client.get(state_key(node_id, "last_activity", prefix=prefix)),
        "last_tool_activity": redis_client.get(state_key(node_id, "last_tool_activity", prefix=prefix)),
        "current_task": _read_json_key(redis_client, state_key(node_id, "current_task", prefix=prefix)),
        "last_outcome": _read_json_key(redis_client, state_key(node_id, "last_outcome", prefix=prefix)),
    }
    raw["observed_at"] = observed_at
    return raw


def record_calibration(
    lane: str | LaneRef,
    signal: str | Mapping[str, Any],
    outcome: str,
    *,
    redis_client: Any = None,
    config: Optional[OrchConfig] = None,
    ts: Optional[float] = None,
    exit_code: Optional[int] = None,
    ttl_remaining: Optional[float] = None,
    metadata: Optional[Mapping[str, Any]] = None,
    prefix: Optional[str] = None,
) -> str:
    client = redis_client or notify_redis_connect()
    lane_ref = LaneRef.parse(lane)
    observed_at = time.time() if ts is None else ts
    fields: dict[str, Any] = {
        "lane": lane_ref.lane_id,
        "kind": lane_ref.kind,
        "signal": signal if isinstance(signal, str) else json.dumps(dict(signal), sort_keys=True, separators=(",", ":")),
        "outcome": outcome,
        "ts": f"{observed_at:.6f}",
    }
    if exit_code is not None:
        fields["exit_code"] = str(int(exit_code))
    if ttl_remaining is not None:
        fields["ttl_remaining"] = f"{float(ttl_remaining):.6f}"
    if metadata:
        fields["metadata"] = json.dumps(dict(metadata), sort_keys=True, separators=(",", ":"))
    return str(client.xadd(calibration_stream_key(prefix=prefix), fields))


def record_exit_code(
    lane: str | LaneRef,
    exit_code: int,
    *,
    redis_client: Any = None,
    config: Optional[OrchConfig] = None,
    ts: Optional[float] = None,
    metadata: Optional[Mapping[str, Any]] = None,
    prefix: Optional[str] = None,
) -> str:
    outcome = "success" if int(exit_code) == 0 else "exit_error"
    signal = {"source": "exit_code", "exit_code": int(exit_code)}
    return record_calibration(
        lane,
        signal,
        outcome,
        redis_client=redis_client,
        config=config,
        ts=ts,
        exit_code=exit_code,
        metadata=metadata,
        prefix=prefix,
    )


def discover_lanes(
    *,
    redis_client: Any = None,
    config: Optional[OrchConfig] = None,
    prefix: Optional[str] = None,
    taeys_hands_root: Optional[Path | str] = None,
) -> list[LaneRef]:
    lanes: dict[str, LaneRef] = {}
    for session_id in _configured_session_ids():
        ref = LaneRef(lane_id=session_id, kind="session", session_id=session_id)
        lanes[ref.lane_id] = ref
    client = redis_client or notify_redis_connect()
    for key in _scan_keys(client, f"{prefix or notify_key_prefix()}:*:last_activity"):
        node_id = _node_from_state_key(key, "last_activity", prefix=prefix)
        if node_id:
            lanes.setdefault(node_id, LaneRef(lane_id=node_id, kind="session", session_id=node_id))
    for ref in discover_chat_lanes(taeys_hands_root=taeys_hands_root):
        lanes[ref.lane_id] = ref
    return sorted(lanes.values(), key=lambda item: item.lane_id)


def discover_chat_lanes(*, taeys_hands_root: Optional[Path | str] = None) -> list[LaneRef]:
    root = _taeys_hands_root(taeys_hands_root)
    refs: dict[str, LaneRef] = {}
    for display, spec in _read_machine_env_displays(root).items():
        ref = _chat_lane_from_display_spec(display, spec)
        if ref:
            refs[ref.lane_id] = ref
    systemd_dir = root / "systemd" / "user"
    for path in sorted(systemd_dir.glob("taey-display-*.service")):
        ref = _chat_lane_from_service(path)
        if ref:
            refs.setdefault(ref.lane_id, ref)
    return sorted(refs.values(), key=lambda item: item.lane_id)


def probe_taeys_hands_ui(lane: str | LaneRef, now: Optional[float] = None) -> Optional[UISignal]:
    lane_ref = LaneRef.parse(lane)
    if lane_ref.kind != "chat" or not lane_ref.platform:
        return None
    observed_at = time.time() if now is None else now
    root = _taeys_hands_root(None)
    if not root.is_dir():
        return UISignal(observed_at=0.0, available=None, raw={"error": "taeys_hands_not_found", "attempted_at": observed_at})
    added_path = False
    old_display = os.environ.get("DISPLAY")
    old_bus = os.environ.get("DBUS_SESSION_BUS_ADDRESS")
    try:
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
            added_path = True
        if lane_ref.display:
            os.environ["DISPLAY"] = f":{lane_ref.display.lstrip(':')}"
            bus_path = Path(f"/tmp/a11y_bus_:{lane_ref.display.lstrip(':')}")
            if bus_path.exists():
                os.environ["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus_path}"
        from consultation_v2.snapshot import build_snapshot

        _firefox, _doc, snapshot = build_snapshot(lane_ref.platform)
        elements = []
        for collection in (getattr(snapshot, "mapped", {}) or {}).values():
            elements.extend(collection)
        elements.extend(getattr(snapshot, "unknown", []) or [])
        elements.extend(getattr(snapshot, "sidebar", []) or [])
        elements.extend(getattr(snapshot, "menu_items", []) or [])
        return _ui_signal_from_elements(elements, observed_at)
    except Exception as exc:
        return UISignal(observed_at=0.0, available=None, raw={"error": f"{type(exc).__name__}: {exc}", "attempted_at": observed_at})
    finally:
        if added_path:
            try:
                sys.path.remove(str(root))
            except ValueError:
                pass
        _restore_env("DISPLAY", old_display)
        _restore_env("DBUS_SESSION_BUS_ADDRESS", old_bus)


def _read_ui_signal(lane: LaneRef, now: float, ui_probe: Optional[UIProbe]) -> Optional[UISignal]:
    if lane.kind != "chat":
        return None
    probe = ui_probe or probe_taeys_hands_ui
    value = probe(lane, now)
    if value is None:
        return None
    if isinstance(value, UISignal):
        return value
    return UISignal.from_mapping(value, now=now)


def _ui_signal_from_elements(elements: Iterable[Any], observed_at: float) -> UISignal:
    names: list[str] = []
    send_disabled = None
    limit_banner = None
    current_model = None
    for element in elements:
        name = str(getattr(element, "name", "") or (element.get("name") if isinstance(element, Mapping) else "") or "").strip()
        role = str(getattr(element, "role", "") or (element.get("role") if isinstance(element, Mapping) else "") or "").strip()
        states_raw = getattr(element, "states", None)
        if states_raw is None and isinstance(element, Mapping):
            states_raw = element.get("states")
        states = {str(item).lower() for item in (states_raw or [])}
        if name:
            names.append(name)
        model_match = _MODEL_SELECTOR_RE.search(name)
        if model_match:
            current_model = model_match.group(1).strip()
        if _THROTTLE_RE.search(name):
            limit_banner = name
        if _SEND_RE.search(name) and "button" in role.lower():
            if "enabled" not in states or "sensitive" not in states:
                send_disabled = True
            elif send_disabled is None:
                send_disabled = False
    cooldown = _parse_cooldown(limit_banner) if limit_banner else None
    return UISignal(
        observed_at=observed_at,
        available=False if send_disabled or limit_banner else None,
        send_button_disabled=send_disabled,
        limit_banner=limit_banner,
        cooldown_remaining=cooldown,
        current_model=current_model,
        raw={"matched_names": names[:20]},
    )


def _configured_session_ids() -> list[str]:
    raw = os.environ.get("ORCH_SESSION_IDS", "")
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        parsed = [item.strip() for item in raw.split(",")]
    if isinstance(parsed, list):
        return [str(item).strip() for item in parsed if str(item).strip()]
    return []


def _read_machine_env_displays(root: Path) -> dict[str, str]:
    candidates = [
        Path.home() / ".taey" / "machine.env",
        root / "systemd" / "machine.env",
        root / "systemd" / "machine.env.template",
    ]
    result: dict[str, str] = {}
    pattern = re.compile(r"^\s*(?:export\s+)?TAEY_DISPLAY_(\d+)\s*=\s*['\"]?([^'\"]+)['\"]?\s*$")
    for path in candidates:
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            match = pattern.match(line)
            if match:
                result[match.group(1)] = match.group(2).strip()
    return result


def _chat_lane_from_display_spec(display: str, spec: str) -> Optional[LaneRef]:
    role, _, rest = spec.partition(":")
    if not role.strip():
        return None
    platform = _platform_from_role(role)
    lane_id = f"chat:{platform}:display:{display}"
    return LaneRef(
        lane_id=lane_id,
        kind="chat",
        session_id=lane_id,
        platform=platform,
        display=display,
        metadata={"role": role.strip(), "display_spec": spec, "tail": rest},
    )


def _chat_lane_from_service(path: Path) -> Optional[LaneRef]:
    name_match = re.search(r"taey-display-(\d+)\.service$", path.name)
    if not name_match:
        return None
    display = name_match.group(1)
    text = path.read_text(encoding="utf-8", errors="replace")
    url_match = re.search(r"https://([^/\s]+)", text)
    profile_match = re.search(r"ff-profile-[\w-]*?(chatgpt|claude|gemini|grok|perplexity)[\w-]*", text, re.IGNORECASE)
    platform = None
    if url_match:
        platform = _platform_from_url(url_match.group(1))
    if not platform and profile_match:
        platform = profile_match.group(1).lower()
    if not platform:
        return None
    lane_id = f"chat:{platform}:display:{display}"
    return LaneRef(
        lane_id=lane_id,
        kind="chat",
        session_id=lane_id,
        platform=platform,
        display=display,
        metadata={"service": str(path), "url_host": url_match.group(1) if url_match else None},
    )


def _taeys_hands_root(explicit: Optional[Path | str]) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    configured = os.environ.get("TAEYS_HANDS_ROOT")
    if configured and configured.strip():
        return Path(configured).expanduser().resolve()
    raise RuntimeError("TAEYS_HANDS_ROOT must be set or an explicit taeys_hands_root passed")


def _platform_from_role(role: str) -> str:
    lower = role.strip().lower()
    for platform in ("chatgpt", "claude", "gemini", "grok", "perplexity"):
        if platform in lower:
            return platform
    return lower.split("_", 1)[0].split("-", 1)[0]


def _platform_from_url(host: str) -> Optional[str]:
    host = host.lower()
    if "chatgpt" in host or "openai" in host:
        return "chatgpt"
    for platform in ("claude", "gemini", "grok", "perplexity"):
        if platform in host:
            return platform
    return None


def _node_from_state_key(key: str, suffix: str, *, prefix: Optional[str] = None) -> Optional[str]:
    raw = key.decode() if isinstance(key, bytes) else str(key)
    pre = f"{prefix or notify_key_prefix()}:"
    tail = f":{suffix}"
    if raw.startswith(pre) and raw.endswith(tail):
        return raw[len(pre):-len(tail)]
    return None


def _scan_keys(redis_client: Any, pattern: str) -> list[str]:
    if hasattr(redis_client, "scan_iter"):
        return [key.decode() if isinstance(key, bytes) else str(key) for key in redis_client.scan_iter(match=pattern)]
    return []


def _read_json_key(redis_client: Any, key: str) -> Optional[dict[str, Any]]:
    raw = redis_client.get(key)
    if not raw:
        return None
    if isinstance(raw, bytes):
        raw = raw.decode()
    try:
        value = json.loads(str(raw))
    except ValueError:
        return {"raw": str(raw)}
    return value if isinstance(value, dict) else {"raw": value}


def _cooldown_from_redis(lane: LaneRef, redis_client: Any, now: float, *, prefix: Optional[str] = None) -> float:
    candidates = [
        lane_key(f"lane_state:cooldown_until:{lane.lane_id}", prefix=prefix),
        state_key(lane.session_id or lane.lane_id, "cooldown_until", prefix=prefix),
    ]
    for key in candidates:
        raw = redis_client.get(key)
        until = _float_or_none(raw)
        if until is not None and until > now:
            return until - now
    return 0.0


def _cooldown_from_last_outcome(outcome: Any, now: float) -> float:
    if not isinstance(outcome, Mapping):
        return 0.0
    details = str(outcome.get("details") or outcome.get("error") or "")
    if not _THROTTLE_RE.search(details):
        return 0.0
    ts = _float_or_none(outcome.get("ts") or outcome.get("recorded_at") or outcome.get("created_at"))
    if ts is not None and now - ts > DEFAULT_COOLDOWN_SECONDS:
        return 0.0
    parsed = _parse_cooldown(details)
    return parsed if parsed is not None else DEFAULT_COOLDOWN_SECONDS


def _parse_cooldown(text: Optional[str]) -> Optional[float]:
    if not text:
        return None
    total = 0.0
    for amount, unit in _COOLDOWN_RE.findall(text):
        value = float(amount)
        unit = unit.lower()
        if unit.startswith("h"):
            total += value * 3600
        elif unit.startswith("m"):
            total += value * 60
        else:
            total += value
    return total if total > 0 else None


def _confidence(signals: Mapping[str, Any], ui_signal: Optional[UISignal], now: float) -> tuple[float, dict[str, Optional[float]]]:
    timestamps = {
        "last_activity": _float_or_none(signals.get("last_activity")),
        "last_tool_activity": _float_or_none(signals.get("last_tool_activity")),
        "current_task_started_at": _float_or_none((signals.get("current_task") or {}).get("started_at") if isinstance(signals.get("current_task"), Mapping) else None),
        "last_outcome_ts": _float_or_none((signals.get("last_outcome") or {}).get("ts") if isinstance(signals.get("last_outcome"), Mapping) else None),
        "ui_observed_at": ui_signal.observed_at if ui_signal else None,
    }
    ages = {key: (round(max(0.0, now - value), 3) if value is not None else None) for key, value in timestamps.items()}
    freshest = min((age for age in ages.values() if age is not None), default=None)
    if freshest is None:
        return 0.2, ages
    if freshest <= 30:
        return 1.0, ages
    if freshest <= DEFAULT_STALE_SECONDS:
        score = 1.0 - ((freshest - 30.0) / (DEFAULT_STALE_SECONDS - 30.0)) * 0.35
        return round(score, 3), ages
    if freshest <= 1800:
        score = 0.65 - ((freshest - DEFAULT_STALE_SECONDS) / 1500.0) * 0.4
        return round(max(0.25, score), 3), ages
    return 0.1, ages


def _ttl_distribution(
    *,
    available_now: bool,
    cooldown_remaining: float,
    confidence: float,
    ui_signal: Optional[UISignal],
    tool_running: bool,
) -> dict[str, float]:
    if cooldown_remaining > 0 or (ui_signal and ui_signal.limit_banner):
        base = {
            "expired": 0.70,
            "lt_5m": 0.20,
            "5m_30m": 0.05,
            "gt_30m": 0.0,
            "unknown": 0.05,
        }
    elif not available_now and tool_running:
        base = {
            "expired": 0.05,
            "lt_5m": 0.15,
            "5m_30m": 0.25,
            "gt_30m": 0.25,
            "unknown": 0.30,
        }
    elif available_now:
        base = {
            "expired": 0.03,
            "lt_5m": 0.10,
            "5m_30m": 0.22,
            "gt_30m": 0.50,
            "unknown": 0.15,
        }
    else:
        base = {
            "expired": 0.20,
            "lt_5m": 0.20,
            "5m_30m": 0.15,
            "gt_30m": 0.05,
            "unknown": 0.40,
        }
    unknown_boost = round((1.0 - confidence) * 0.35, 6)
    if unknown_boost > 0:
        known_total = sum(value for key, value in base.items() if key != "unknown")
        for key in TTL_BUCKETS:
            if key != "unknown":
                base[key] = base[key] * (1.0 - unknown_boost) / known_total * (1.0 - base["unknown"])
        base["unknown"] = min(1.0, base["unknown"] + unknown_boost)
    return _normalize_distribution(base)


def _normalize_distribution(values: Mapping[str, float]) -> dict[str, float]:
    clipped = {key: max(0.0, float(values.get(key, 0.0))) for key in TTL_BUCKETS}
    total = sum(clipped.values())
    if total <= 0:
        return {key: (1.0 if key == "unknown" else 0.0) for key in TTL_BUCKETS}
    normalized = {key: round(value / total, 6) for key, value in clipped.items()}
    drift = round(1.0 - sum(normalized.values()), 6)
    normalized["unknown"] = round(normalized["unknown"] + drift, 6)
    return normalized


def _jsonable(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, bytes):
        return value.decode()
    return value


def _float_or_none(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bytes):
        value = value.decode()
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _optional_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, bytes):
        value = value.decode()
    lowered = str(value).strip().lower()
    if lowered in {"1", "true", "yes", "y", "on"}:
        return True
    if lowered in {"0", "false", "no", "n", "off"}:
        return False
    return None


def _truthy(value: Any) -> bool:
    parsed = _optional_bool(value)
    if parsed is not None:
        return parsed
    return bool(value)


def _restore_env(name: str, value: Optional[str]) -> None:
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
