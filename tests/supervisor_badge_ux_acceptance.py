#!/usr/bin/env python3
"""Acceptance: Sessions strip renders one lower-case supervisor badge."""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _function_body(source: str, name: str) -> str:
    match = re.search(rf"function {re.escape(name)}\([^)]*\) \{{", source)
    if not match:
        raise AssertionError(f"{name} not found")
    depth = 0
    start = match.start()
    for index in range(match.end() - 1, len(source)):
        char = source[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return source[start:index + 1]
    raise AssertionError(f"{name} body did not close")


def _css_block(source: str, selector: str) -> str:
    start = source.find(selector)
    if start == -1:
        raise AssertionError(f"{selector} not found")
    end = source.find("}", start)
    if end == -1:
        raise AssertionError(f"{selector} body did not close")
    return source[start:end + 1]


def main() -> int:
    app_js = (ROOT / "ui/static/app.js").read_text(encoding="utf-8")
    app_css = (ROOT / "ui/static/app.css").read_text(encoding="utf-8")
    render_session_cards = _function_body(app_js, "renderSessionCards")
    render_supervisor_badge = _function_body(app_js, "renderSupervisorBadge")
    supervisor_badge_css = _css_block(app_css, ".status-badge.supervisor-badge")

    _check(
        "session card uses one effective supervisor badge",
        "renderSupervisorBadge(sessionBadge)" in render_session_cards,
        render_session_cards,
    )
    _check(
        "session card does not render old activity badge beside supervisor badge",
        "renderActivityBadge(" not in render_session_cards,
        render_session_cards,
    )
    _check(
        "session card does not render a second fallback needs-you pill",
        "showFallbackNeedsYou" not in render_session_cards,
        render_session_cards,
    )
    _check(
        "supervisor badge renders label text from API or effective badge",
        "const label = badge.label || badge.state" in render_supervisor_badge,
        render_supervisor_badge,
    )
    _check(
        "supervisor badge CSS stays lower-case",
        "text-transform: lowercase" in supervisor_badge_css and "text-transform: uppercase" not in supervisor_badge_css,
        supervisor_badge_css,
    )

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)}: {FAILURES}")
        return 1
    print("\nPASS - session cards render one lower-case supervisor badge.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
