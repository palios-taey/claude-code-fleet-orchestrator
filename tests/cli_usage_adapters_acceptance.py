#!/usr/bin/env python3
from __future__ import annotations

import json
import sys
import tempfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fleet_orchestrator.cli_usage import collect_lane_usage, record_usage_calibrations  # noqa: E402
from fleet_orchestrator.lane_state import calibration_stream_key  # noqa: E402


class FakeRedis:
    def __init__(self) -> None:
        self.streams: dict[str, list[dict[str, str]]] = {}

    def xadd(self, key: str, fields: dict):
        entry_id = f"{len(self.streams.get(key, [])) + 1}-0"
        stored = {str(k): str(v) for k, v in fields.items()}
        stored["_id"] = entry_id
        self.streams.setdefault(key, []).append(stored)
        return entry_id


def _write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(row, sort_keys=True) for row in rows) + "\n", encoding="utf-8")


def _by_cli(usages):
    return {usage.cli: usage for usage in usages}


def _check(label: str, condition: bool, detail=None) -> None:
    if not condition:
        raise AssertionError(f"{label}: {detail!r}")
    print(f"PASS {label}")


def main() -> None:
    now = time.time()
    with tempfile.TemporaryDirectory(prefix="orch-cli-usage-") as tmp:
        home = Path(tmp)
        _write_jsonl(
            home / ".claude" / "projects" / "proj-a" / "sess-claude.jsonl",
            [
                {
                    "timestamp": now - 20,
                    "sessionId": "sess-claude",
                    "message": {
                        "id": "msg-claude-1",
                        "model": "claude-sonnet",
                        "usage": {
                            "input_tokens": 100,
                            "output_tokens": 30,
                            "cache_creation_input_tokens": 7,
                            "cache_read_input_tokens": 3,
                        },
                    },
                    "total_cost_usd": 0.12,
                },
                {
                    "timestamp": now - 19,
                    "uuid": "different-wrapper-id",
                    "sessionId": "sess-claude",
                    "message": {
                        "id": "msg-claude-1",
                        "model": "claude-sonnet",
                        "usage": {
                            "input_tokens": 100,
                            "output_tokens": 30,
                            "cache_creation_input_tokens": 7,
                            "cache_read_input_tokens": 3,
                        },
                    },
                },
                {
                    "timestamp": now - 10,
                    "sessionId": "sess-claude",
                    "type": "system",
                    "subtype": "api_error",
                    "level": "error",
                    "error": "Usage limit reached. Try again later.",
                },
            ],
        )
        _write_jsonl(
            home / ".gemini" / "tmp" / "proj-b" / "chats" / "nested" / "sess-gemini.jsonl",
            [
                {
                    "timestamp": now - 30,
                    "type": "gemini",
                    "id": "gemini-message-1",
                    "model": "gemini-pro",
                    "tokens": {"input": 200, "output": 55, "cached": 11, "thoughts": 13, "tool": 5, "total": 279},
                },
                {
                    "timestamp": now - 29,
                    "type": "gemini",
                    "id": "gemini-message-1",
                    "model": "gemini-pro",
                    "tokens": {"input": 200, "output": 55, "cached": 11, "thoughts": 13, "tool": 5, "total": 279},
                }
            ],
        )
        _write_jsonl(
            home / ".grok" / "logs" / "unified.jsonl",
            [
                {
                    "ts": now - 8,
                    "sid": "sess-grok",
                    "msg": "tokens",
                    "ctx": {
                        "prompt_tokens": 300,
                        "completion_tokens": 44,
                        "reasoning_tokens": 22,
                        "cached_prompt_tokens": 9,
                    },
                },
                {"ts": now - 7, "sid": "sess-grok", "msg": "429 rate-limit from provider", "ctx": {"status": 429}},
            ],
        )
        _write_jsonl(
            home / ".codex" / "sessions" / "2026" / "06" / "19" / "rollout-sess-codex.jsonl",
            [
                {
                    "timestamp": now - 6,
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 10,
                                "cached_input_tokens": 5,
                                "output_tokens": 1,
                                "reasoning_output_tokens": 0,
                                "total_tokens": 11,
                            }
                        },
                        "rate_limits": [{"primary": {"used_percent": 50, "resets_at": "2026-06-19T00:00:00Z"}}],
                    },
                },
                {
                    "timestamp": now - 5,
                    "payload": {
                        "type": "token_count",
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 25,
                                "cached_input_tokens": 8,
                                "output_tokens": 4,
                                "reasoning_output_tokens": 2,
                                "total_tokens": 29,
                            }
                        },
                        "rate_limits": [
                            {
                                "rate_limit_reached_type": "primary",
                                "primary": {"used_percent": 100, "resets_at": "2026-06-19T01:00:00Z"},
                            }
                        ],
                    },
                },
            ],
        )

        usages = collect_lane_usage(home=home, gemini_exit_codes={"gemini:proj-b:sess-gemini": 8})
        by_cli = _by_cli(usages)
        _check("all four adapters emit usage", set(by_cli) == {"claude_code", "gemini", "grok", "codex"}, by_cli)

        claude = by_cli["claude_code"]
        _check("Claude Code sums nested usage tokens", claude.tokens.prompt == 100 and claude.tokens.completion == 30 and claude.tokens.cache == 10, claude)
        _check("Claude Code throttles from JSONL output text", claude.throttled and claude.throttle_source == "jsonl_output_text", claude)

        gemini = by_cli["gemini"]
        _check("Gemini maps token fields", gemini.tokens.prompt == 200 and gemini.tokens.completion == 55 and gemini.tokens.reasoning == 13 and gemini.tokens.total == 279, gemini)
        _check("Gemini exit code 8 marks throttled", gemini.throttled and gemini.throttle_source == "exit_code_8", gemini)

        grok = by_cli["grok"]
        _check("Grok sums sid token records", grok.tokens.prompt == 300 and grok.tokens.completion == 44 and grok.tokens.reasoning == 22, grok)
        _check("Grok 429 log marks throttled", grok.throttled and grok.throttle_source == "log_429_event", grok)

        codex = by_cli["codex"]
        _check("Codex uses latest cumulative rollout total, not sum of events", codex.tokens.prompt == 25 and codex.tokens.total == 29, codex)
        _check("Codex rate_limits mark throttled with reset hint", codex.throttled and "resets_at" in (codex.reset_hint or ""), codex)

        fake = FakeRedis()
        ids = record_usage_calibrations(usages, redis_client=fake, prefix="cli-usage-acceptance")
        stream = fake.streams[calibration_stream_key(prefix="cli-usage-acceptance")]
        _check("record_usage_calibrations appends one stream event per usage", ids == ["1-0", "2-0", "3-0", "4-0"] and len(stream) == 4, stream)
        first_signal = json.loads(stream[0]["signal"])
        _check("calibration stream carries normalized LaneUsage schema", {"lane_id", "cli", "tokens", "throttled", "source_confidence"} <= set(first_signal), first_signal)
        _check("throttled usage records throttled outcome", any(row["outcome"] == "throttled" for row in stream), stream)

    print("\nPASS CLI usage adapters normalize native logs and append lane calibration events.")


if __name__ == "__main__":
    main()
