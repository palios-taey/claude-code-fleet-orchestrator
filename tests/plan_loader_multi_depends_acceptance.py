#!/usr/bin/env python3
"""Acceptance: a task may declare multiple dependencies — via repeated `[depends: x]`
tags AND/OR comma lists — and the loader must capture ALL of them.

Regression guard for the 2026-06-15 under-gating bug: `_parse_meta` did
`meta[key] = [...]` for depends/tags, OVERWRITING on each bracket, so a task with
`[depends: a] [depends: b]` kept only `b`. The dropped dependency was silently
not gated -> the task surfaced as ready while an unmet dep remained, blocking
stop_reason and forcing false forward motion. (refs were already accumulated
correctly; depends/tags now match that.)
"""
from __future__ import annotations

import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from fleet_orchestrator.plan_loader import _parse_meta, _parse_plan  # noqa: E402

FAILURES: list[str] = []


def _check(label: str, cond: bool, detail: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {detail}"))
    if not cond:
        FAILURES.append(label)


def main() -> int:
    # --- unit: _parse_meta accumulates repeated [depends:] + comma lists ---
    m = _parse_meta("[priority: 36] [owner: t] [depends: featured-image-prompts] [depends: image-gen-vendor-recon]")
    _check(
        "two separate [depends:] tags both captured",
        m.get("depends") == ["featured-image-prompts", "image-gen-vendor-recon"],
        m.get("depends"),
    )

    m2 = _parse_meta("[depends: a] [depends: b,c]")
    _check("repeated tag + comma list union to all three", m2.get("depends") == ["a", "b", "c"], m2.get("depends"))

    m3 = _parse_meta("[depends: a,b]")
    _check("single comma list still works", m3.get("depends") == ["a", "b"], m3.get("depends"))

    m4 = _parse_meta("[tags: x] [tags: y]")
    _check("repeated [tags:] also accumulate", m4.get("tags") == ["x", "y"], m4.get("tags"))

    # --- integration: a full plan with a multi-depends task carries both deps ---
    plan_md = """# Project: t-multidep - multidep probe
> probe

## Phase: p - p [order: 1]

### Task: a - first [priority: 10] [owner: t]
### Task: b - second [priority: 11] [owner: t]
### Task: c - needs both [priority: 12] [owner: t] [depends: a] [depends: b]
"""
    plan = _parse_plan(plan_md)
    tasks = {}
    for ph in plan.get("phases", []):
        for tk in ph.get("tasks", []):
            tasks[tk["id"]] = tk
    c_deps = sorted(tasks.get("c", {}).get("depends", []))
    _check("full-plan multi-depends task keeps BOTH deps", c_deps == ["a", "b"], c_deps)

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - loader captures all dependencies (repeated tags + comma lists).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
