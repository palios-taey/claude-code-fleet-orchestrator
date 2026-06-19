"""Native CLI usage-log adapters feeding passive lane calibration.

The adapters read local, CLI-owned token/rate-limit logs and normalize them to
LaneUsage records. They are measurement producers only: recording a LaneUsage
appends to lane_state's calibration stream and does not influence routing.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from fleet_orchestrator.lane_state import record_calibration
from fleet_orchestrator.notify_state import redis_connect as notify_redis_connect


CLI_NAMES = ("claude_code", "gemini", "grok", "codex")
THROTTLED_OUTCOME = "throttled"
OBSERVED_OUTCOME = "observed"

_THROTTLE_RE = re.compile(
    r"\b(429|rate[- ]?limit|quota|usage limit|too many requests|try again|cooldown|temporar(?:y|ily)|unavailable)\b",
    re.IGNORECASE,
)
_ISO_Z_RE = re.compile(r"Z$")


@dataclass(frozen=True)
class UsageTokens:
    prompt: int = 0
    completion: int = 0
    reasoning: Optional[int] = None
    cache: Optional[int] = None
    total: int = 0

    def to_dict(self) -> dict[str, int]:
        result = {
            "prompt": int(self.prompt),
            "completion": int(self.completion),
            "total": int(self.total),
        }
        if self.reasoning is not None:
            result["reasoning"] = int(self.reasoning)
        if self.cache is not None:
            result["cache"] = int(self.cache)
        return result


@dataclass(frozen=True)
class LaneUsage:
    lane_id: str
    cli: str
    tokens: UsageTokens
    cost_usd: Optional[float] = None
    throttled: bool = False
    throttle_source: str = "none"
    reset_hint: Optional[str] = None
    model: Optional[str] = None
    last_event_ts: Optional[float] = None
    source_confidence: float = 0.9

    def to_dict(self) -> dict[str, Any]:
        return {
            "lane_id": self.lane_id,
            "cli": self.cli,
            "tokens": self.tokens.to_dict(),
            "cost_usd": self.cost_usd,
            "throttled": bool(self.throttled),
            "throttle_source": self.throttle_source,
            "reset_hint": self.reset_hint,
            "model": self.model,
            "last_event_ts": self.last_event_ts,
            "source_confidence": float(self.source_confidence),
        }

    def calibration_signal(self) -> dict[str, Any]:
        return self.to_dict()

    @property
    def outcome(self) -> str:
        return THROTTLED_OUTCOME if self.throttled else OBSERVED_OUTCOME


def collect_lane_usage(
    *,
    home: Optional[Path | str] = None,
    clis: Iterable[str] = CLI_NAMES,
    gemini_exit_codes: Optional[Mapping[str, int]] = None,
) -> list[LaneUsage]:
    root = Path(home).expanduser() if home is not None else Path.home()
    usages: list[LaneUsage] = []
    for raw_cli in clis:
        cli = _normalize_cli_name(raw_cli)
        if cli == "claude_code":
            usages.extend(read_claude_code_usage(root / ".claude" / "projects"))
        elif cli == "gemini":
            usages.extend(read_gemini_usage(root / ".gemini" / "tmp", exit_codes=gemini_exit_codes))
        elif cli == "grok":
            usages.extend(read_grok_usage(root / ".grok" / "logs" / "unified.jsonl"))
        elif cli == "codex":
            usages.extend(read_codex_usage(root / ".codex" / "sessions"))
        else:
            raise ValueError(f"unknown cli adapter: {cli}")
    return sorted(usages, key=lambda item: (item.cli, item.lane_id))


def record_usage_calibrations(
    usages: Iterable[LaneUsage],
    *,
    redis_client: Any = None,
    prefix: Optional[str] = None,
) -> list[str]:
    client = redis_client or notify_redis_connect()
    ids: list[str] = []
    for usage in usages:
        ids.append(
            record_calibration(
                usage.lane_id,
                usage.calibration_signal(),
                usage.outcome,
                redis_client=client,
                ts=usage.last_event_ts,
                metadata={
                    "cli": usage.cli,
                    "source_confidence": usage.source_confidence,
                    "tokens_total": usage.tokens.total,
                },
                prefix=prefix,
            )
        )
    return ids


def read_claude_code_usage(root: Path) -> list[LaneUsage]:
    aggregates: dict[str, dict[str, Any]] = {}
    if not root.is_dir():
        return []
    for path in sorted(root.glob("*/*.jsonl")):
        fallback_lane = path.stem
        for record in _iter_jsonl(path):
            session_id = _string_or_none(record.get("sessionId")) or _string_or_none(record.get("session_id")) or fallback_lane
            lane_id = f"claude_code:{session_id}"
            agg = _usage_aggregate(aggregates, lane_id, cli="claude_code", confidence=0.95)
            ts = _coerce_ts(record.get("timestamp") or record.get("created_at") or record.get("time"))
            _set_latest(agg, ts)
            usage = _nested_mapping(record, ("message", "usage")) or _mapping_or_none(record.get("usage"))
            if usage:
                event_key = _claude_event_key(record)
                if event_key and event_key in agg["seen_event_ids"]:
                    continue
                if event_key:
                    agg["seen_event_ids"].add(event_key)
                agg["prompt"] += _int(usage.get("input_tokens"))
                agg["completion"] += _int(usage.get("output_tokens"))
                agg["cache"] += (
                    _int(usage.get("cache_creation_input_tokens"))
                    + _int(usage.get("cache_read_input_tokens"))
                    + _int(usage.get("cached_input_tokens"))
                )
                model = _string_or_none(_nested_value(record, ("message", "model"))) or _string_or_none(record.get("model"))
                if model:
                    agg["model"] = model
            cost = _extract_cost(record)
            if cost is not None:
                agg["cost_usd"] = float(agg.get("cost_usd") or 0.0) + cost
            if _claude_record_throttled(record):
                agg["throttled"] = True
                agg["throttle_source"] = "jsonl_output_text"
    return [_aggregate_to_lane_usage(lane_id, agg) for lane_id, agg in aggregates.items() if _has_usage_or_throttle(agg)]


def read_gemini_usage(root: Path, *, exit_codes: Optional[Mapping[str, int]] = None) -> list[LaneUsage]:
    aggregates: dict[str, dict[str, Any]] = {}
    if not root.is_dir():
        return []
    exit_codes = exit_codes or {}
    for path in sorted(root.glob("*/chats/**/*.jsonl")):
        project = _safe_lane_part(path.relative_to(root).parts[0])
        chat = _safe_lane_part(path.stem)
        lane_id = f"gemini:{project}:{chat}"
        agg = _usage_aggregate(aggregates, lane_id, cli="gemini", confidence=0.95)
        for record in _iter_jsonl(path):
            ts = _coerce_ts(record.get("timestamp") or record.get("time"))
            _set_latest(agg, ts)
            if record.get("type") != "gemini":
                continue
            tokens = _mapping_or_none(record.get("tokens"))
            if not tokens:
                continue
            event_key = _string_or_none(record.get("id"))
            if event_key and event_key in agg["seen_event_ids"]:
                continue
            if event_key:
                agg["seen_event_ids"].add(event_key)
            agg["prompt"] += _int(tokens.get("input"))
            agg["completion"] += _int(tokens.get("output"))
            agg["reasoning"] += _int(tokens.get("thoughts"))
            agg["cache"] += _int(tokens.get("cached"))
            total = _int(tokens.get("total"))
            if total:
                agg["explicit_total"] += total
            model = _string_or_none(record.get("model"))
            if model:
                agg["model"] = model
        exit_code = _lookup_exit_code(exit_codes, lane_id, path.stem, project)
        if exit_code == 8:
            agg["throttled"] = True
            agg["throttle_source"] = "exit_code_8"
        if exit_code is not None:
            agg["exit_code"] = int(exit_code)
    return [_aggregate_to_lane_usage(lane_id, agg) for lane_id, agg in aggregates.items() if _has_usage_or_throttle(agg)]


def read_grok_usage(path: Path) -> list[LaneUsage]:
    aggregates: dict[str, dict[str, Any]] = {}
    if not path.is_file():
        return []
    for record in _iter_jsonl(path):
        sid = _string_or_none(record.get("sid"))
        if not sid:
            continue
        lane_id = f"grok:{sid}"
        agg = _usage_aggregate(aggregates, lane_id, cli="grok", confidence=0.9)
        ts = _coerce_ts(record.get("ts") or record.get("timestamp") or record.get("time"))
        _set_latest(agg, ts)
        ctx = _mapping_or_none(record.get("ctx")) or {}
        agg["prompt"] += _int(ctx.get("prompt_tokens"))
        agg["completion"] += _int(ctx.get("completion_tokens"))
        agg["reasoning"] += _int(ctx.get("reasoning_tokens"))
        agg["cache"] += _int(ctx.get("cached_prompt_tokens"))
        model = _string_or_none(ctx.get("model")) or _string_or_none(record.get("model"))
        if model:
            agg["model"] = model
        if _grok_record_throttled(record):
            agg["throttled"] = True
            agg["throttle_source"] = "log_429_event"
    return [_aggregate_to_lane_usage(lane_id, agg) for lane_id, agg in aggregates.items() if _has_usage_or_throttle(agg)]


def read_codex_usage(root: Path) -> list[LaneUsage]:
    usages: list[LaneUsage] = []
    if not root.is_dir():
        return usages
    for path in sorted(root.rglob("rollout-*.jsonl")):
        latest_usage: Optional[Mapping[str, Any]] = None
        latest_rate_limits: Any = None
        latest_ts: Optional[float] = None
        model: Optional[str] = None
        for record in _iter_jsonl(path):
            payload = _mapping_or_none(record.get("payload")) or {}
            if payload.get("type") != "token_count":
                continue
            latest_ts = _coerce_ts(record.get("timestamp") or record.get("ts")) or latest_ts
            info = _mapping_or_none(payload.get("info")) or {}
            total_usage = _mapping_or_none(info.get("total_token_usage"))
            if total_usage:
                latest_usage = total_usage
            latest_rate_limits = payload.get("rate_limits")
            model = _string_or_none(payload.get("model")) or _string_or_none(info.get("model")) or model
        if not latest_usage:
            continue
        session = _codex_session_id(path)
        throttled, reset_hint = _codex_throttle(latest_rate_limits)
        tokens = UsageTokens(
            prompt=_int(latest_usage.get("input_tokens")),
            completion=_int(latest_usage.get("output_tokens")),
            reasoning=_int(latest_usage.get("reasoning_output_tokens")),
            cache=_int(latest_usage.get("cached_input_tokens")),
            total=_int(latest_usage.get("total_tokens")),
        )
        usages.append(
            LaneUsage(
                lane_id=f"codex:{session}",
                cli="codex",
                tokens=tokens,
                throttled=throttled,
                throttle_source="rate_limits" if throttled else "none",
                reset_hint=reset_hint,
                model=model,
                last_event_ts=latest_ts,
                source_confidence=0.75,
            )
        )
    return usages


def _usage_aggregate(aggregates: dict[str, dict[str, Any]], lane_id: str, *, cli: str, confidence: float) -> dict[str, Any]:
    if lane_id not in aggregates:
        aggregates[lane_id] = {
            "cli": cli,
            "prompt": 0,
            "completion": 0,
            "reasoning": 0,
            "cache": 0,
            "explicit_total": 0,
            "cost_usd": None,
            "throttled": False,
            "throttle_source": "none",
            "reset_hint": None,
            "model": None,
            "last_event_ts": None,
            "source_confidence": confidence,
            "seen_event_ids": set(),
        }
    return aggregates[lane_id]


def _aggregate_to_lane_usage(lane_id: str, agg: Mapping[str, Any]) -> LaneUsage:
    reasoning = _int(agg.get("reasoning"))
    cache = _int(agg.get("cache"))
    explicit_total = _int(agg.get("explicit_total"))
    prompt = _int(agg.get("prompt"))
    completion = _int(agg.get("completion"))
    total = explicit_total or (prompt + completion + reasoning)
    return LaneUsage(
        lane_id=lane_id,
        cli=str(agg.get("cli")),
        tokens=UsageTokens(prompt=prompt, completion=completion, reasoning=reasoning, cache=cache, total=total),
        cost_usd=_float_or_none(agg.get("cost_usd")),
        throttled=bool(agg.get("throttled")),
        throttle_source=str(agg.get("throttle_source") or "none"),
        reset_hint=_string_or_none(agg.get("reset_hint")),
        model=_string_or_none(agg.get("model")),
        last_event_ts=_float_or_none(agg.get("last_event_ts")),
        source_confidence=float(agg.get("source_confidence") or 0.9),
    )


def _has_usage_or_throttle(agg: Mapping[str, Any]) -> bool:
    return (
        _int(agg.get("prompt")) > 0
        or _int(agg.get("completion")) > 0
        or _int(agg.get("reasoning")) > 0
        or _int(agg.get("cache")) > 0
        or bool(agg.get("throttled"))
    )


def _iter_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    value = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if isinstance(value, dict):
                    yield value
    except OSError:
        return


def _claude_record_throttled(record: Mapping[str, Any]) -> bool:
    record_type = _string_or_none(record.get("type")) or ""
    subtype = _string_or_none(record.get("subtype")) or ""
    level = (_string_or_none(record.get("level")) or "").lower()
    if record_type not in {"system", "error"} and "error" not in subtype and level not in {"error", "warn", "warning"}:
        return False
    return _any_throttle_text(
        record.get("error"),
        record.get("message") if not isinstance(record.get("message"), Mapping) else None,
        record.get("reason"),
        record.get("status"),
        record.get("code"),
    )


def _grok_record_throttled(record: Mapping[str, Any]) -> bool:
    ctx = _mapping_or_none(record.get("ctx")) or {}
    status = _string_or_none(ctx.get("status")) or _string_or_none(record.get("status"))
    if status == "429":
        return True
    return _any_throttle_text(record.get("msg"), ctx.get("error"), ctx.get("message"), ctx.get("status"), ctx.get("code"))


def _codex_throttle(rate_limits: Any) -> tuple[bool, Optional[str]]:
    entries = rate_limits if isinstance(rate_limits, list) else [rate_limits]
    throttled = False
    reset_hints: list[str] = []
    for entry in entries:
        item = _mapping_or_none(entry)
        if not item:
            continue
        reached = _string_or_none(item.get("rate_limit_reached_type"))
        if reached:
            throttled = True
            reset_hints.append(f"{reached} reached")
        for name in ("primary", "secondary"):
            bucket = _mapping_or_none(item.get(name))
            if not bucket:
                continue
            used = _float_or_none(bucket.get("used_percent"))
            if used is not None and used >= 100.0:
                throttled = True
            resets_at = _string_or_none(bucket.get("resets_at"))
            window = _string_or_none(bucket.get("window_minutes"))
            if resets_at:
                reset_hints.append(f"{name} resets_at {resets_at}")
            elif window:
                reset_hints.append(f"{name} window_minutes {window}")
    return throttled, "; ".join(reset_hints) if reset_hints else None


def _any_throttle_text(*values: Any) -> bool:
    for value in values:
        if value is None:
            continue
        if isinstance(value, (list, tuple, set)):
            if _any_throttle_text(*value):
                return True
            continue
        if isinstance(value, Mapping):
            if _any_throttle_text(*value.values()):
                return True
            continue
        if _THROTTLE_RE.search(str(value)):
            return True
    return False


def _nested_mapping(record: Mapping[str, Any], path: tuple[str, ...]) -> Optional[Mapping[str, Any]]:
    value: Any = record
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value if isinstance(value, Mapping) else None


def _nested_value(record: Mapping[str, Any], path: tuple[str, ...]) -> Any:
    value: Any = record
    for key in path:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _mapping_or_none(value: Any) -> Optional[Mapping[str, Any]]:
    return value if isinstance(value, Mapping) else None


def _extract_cost(record: Mapping[str, Any]) -> Optional[float]:
    for key in ("total_cost_usd", "cost_usd", "costUSD", "cost"):
        value = _float_or_none(record.get(key))
        if value is not None:
            return value
    message = _mapping_or_none(record.get("message"))
    if message:
        for key in ("total_cost_usd", "cost_usd", "costUSD", "cost"):
            value = _float_or_none(message.get(key))
            if value is not None:
                return value
    return None


def _claude_event_key(record: Mapping[str, Any]) -> Optional[str]:
    message_id = _string_or_none(_nested_value(record, ("message", "id")))
    if message_id:
        return message_id
    return _string_or_none(record.get("uuid"))


def _lookup_exit_code(codes: Mapping[str, int], *keys: str) -> Optional[int]:
    for key in keys:
        if key in codes:
            return int(codes[key])
    return None


def _coerce_ts(value: Any) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = float(value)
        return number / 1000.0 if number > 10_000_000_000 else number
    text = str(value).strip()
    if not text:
        return None
    try:
        number = float(text)
    except ValueError:
        pass
    else:
        return number / 1000.0 if number > 10_000_000_000 else number
    try:
        normalized = _ISO_Z_RE.sub("+00:00", text)
        return datetime.fromisoformat(normalized).timestamp()
    except ValueError:
        return None


def _set_latest(agg: dict[str, Any], ts: Optional[float]) -> None:
    if ts is None:
        return
    current = _float_or_none(agg.get("last_event_ts"))
    if current is None or ts > current:
        agg["last_event_ts"] = ts


def _int(value: Any) -> int:
    if value is None or value is False:
        return 0
    try:
        return int(float(value))
    except (TypeError, ValueError, OverflowError):
        return 0


def _float_or_none(value: Any) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _string_or_none(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _safe_lane_part(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return text or "unknown"


def _codex_session_id(path: Path) -> str:
    stem = path.stem
    if stem.startswith("rollout-"):
        return _safe_lane_part(stem[len("rollout-"):])
    return _safe_lane_part(stem)


def _normalize_cli_name(value: str) -> str:
    name = value.strip().lower().replace("-", "_")
    if name == "claude":
        return "claude_code"
    if name not in CLI_NAMES:
        raise ValueError(f"unknown cli adapter: {value}")
    return name


def _parse_exit_code_arg(values: Optional[list[str]]) -> dict[str, int]:
    result: dict[str, int] = {}
    for raw in values or []:
        key, sep, value = raw.partition("=")
        if not sep or not key.strip():
            raise SystemExit(f"--gemini-exit-code must be lane=code, got {raw!r}")
        try:
            result[key.strip()] = int(value)
        except ValueError as exc:
            raise SystemExit(f"--gemini-exit-code code must be an integer, got {raw!r}") from exc
    return result


def _limit_per_cli(usages: list[LaneUsage], limit: Optional[int]) -> list[LaneUsage]:
    if limit is None or limit <= 0:
        return usages
    counts: dict[str, int] = {}
    selected: list[LaneUsage] = []
    for usage in sorted(usages, key=lambda item: (item.cli, -(item.last_event_ts or 0.0), item.lane_id)):
        count = counts.get(usage.cli, 0)
        if count >= limit:
            continue
        counts[usage.cli] = count + 1
        selected.append(usage)
    return sorted(selected, key=lambda item: (item.cli, item.lane_id))


def _format_ts(value: Optional[float]) -> str:
    if value is None:
        return "-"
    return datetime.fromtimestamp(value, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def _print_text(usages: list[LaneUsage], *, recorded: int = 0) -> None:
    if not usages:
        print("No CLI usage records found.")
        return
    print(f"{'CLI':<12} {'Lane':<52} {'Prompt':>10} {'Completion':>10} {'Reasoning':>10} {'Cache':>10} {'Total':>10} {'Throttle':<10} Last event")
    print("-" * 145)
    for usage in usages:
        print(
            f"{usage.cli:<12} {usage.lane_id[:52]:<52} "
            f"{usage.tokens.prompt:>10} {usage.tokens.completion:>10} "
            f"{(usage.tokens.reasoning or 0):>10} {(usage.tokens.cache or 0):>10} "
            f"{usage.tokens.total:>10} {str(usage.throttled):<10} {_format_ts(usage.last_event_ts)}"
        )
    if recorded:
        print(f"\nRecorded {recorded} calibration event(s).")


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        prog="taey-lane-usage",
        description="Read native CLI token/rate-limit logs and optionally append lane calibration events.",
    )
    parser.add_argument("--home", type=Path, default=Path.home(), help="Home directory containing .claude/.gemini/.grok/.codex logs")
    parser.add_argument("--cli", action="append", choices=CLI_NAMES, help="CLI adapter to run; repeatable; default runs all")
    parser.add_argument("--record", action="store_true", help="Append records to the lane_state calibration Redis stream")
    parser.add_argument("--prefix", help="Notify Redis key prefix for recorded calibration events")
    parser.add_argument("--json", action="store_true", help="Print normalized LaneUsage JSON")
    parser.add_argument("--limit-per-cli", type=int, help="Only emit the most recent N lanes per CLI")
    parser.add_argument(
        "--gemini-exit-code",
        action="append",
        default=[],
        metavar="LANE=CODE",
        help="Optional Gemini lane/session exit code; code 8 marks that lane throttled",
    )
    args = parser.parse_args(argv)

    usages = collect_lane_usage(
        home=args.home,
        clis=args.cli or CLI_NAMES,
        gemini_exit_codes=_parse_exit_code_arg(args.gemini_exit_code),
    )
    usages = _limit_per_cli(usages, args.limit_per_cli)

    recorded_ids: list[str] = []
    if args.record:
        recorded_ids = record_usage_calibrations(usages, prefix=args.prefix)

    if args.json:
        print(json.dumps([usage.to_dict() for usage in usages], sort_keys=True, indent=2))
    else:
        _print_text(usages, recorded=len(recorded_ids))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
