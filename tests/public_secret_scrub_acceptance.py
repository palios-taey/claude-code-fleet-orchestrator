#!/usr/bin/env python3
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet_orchestrator.public_readonly import _scrub_public_text  # noqa: E402

FAILURES: list[str] = []


def _check(label: str, condition: bool, detail: object = "") -> None:
    print(("PASS " if condition else "FAIL ") + label + ("" if condition else f" -> {detail}"))
    if not condition:
        FAILURES.append(label)


def main() -> int:
    sample = (
        "password: hunter2 token: ghp_1234567890abcdefABCDEF secret: keepme "
        "api_key=sk-1234567890abcdef"
    )
    scrubbed = _scrub_public_text(sample)
    _check("colon password redacted", "hunter2" not in scrubbed, scrubbed)
    _check("colon token redacted", "ghp_1234567890abcdefABCDEF" not in scrubbed, scrubbed)
    _check("colon secret redacted", "keepme" not in scrubbed, scrubbed)
    _check("equals api key still redacted", "sk-1234567890abcdef" not in scrubbed, scrubbed)
    _check("redaction marker emitted", scrubbed.count("[secret]") >= 4, scrubbed)

    benign = _scrub_public_text("status: active owner: conductor")
    _check("benign colon labels remain visible", "status: active" in benign and "owner: conductor" in benign, benign)

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - public readonly scrubber redacts colon-delimited secrets.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
