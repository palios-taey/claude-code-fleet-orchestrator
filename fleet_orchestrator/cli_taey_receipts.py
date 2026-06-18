#!/usr/bin/env python3
"""Inspect decision receipts emitted by the orchestrator."""
from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from fleet_orchestrator.decision_receipt import RECEIPT_STREAM, read_recent_receipts


def _truncate(value: Any, width: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= width:
        return text
    return text[: max(0, width - 1)] + "..."


def _print_table(receipts: list[dict[str, Any]]) -> None:
    if not receipts:
        print(f"No decision receipts found in {RECEIPT_STREAM}.")
        return
    print(f"{'STREAM ID':<20} {'KIND':<22} {'CREATED':<25} WHY")
    print(f"{'-' * 20} {'-' * 22} {'-' * 25} {'-' * 40}")
    for receipt in receipts:
        print(
            f"{_truncate(receipt.get('_stream_id'), 20):<20} "
            f"{_truncate(receipt.get('kind') or receipt.get('_stream_kind'), 22):<22} "
            f"{_truncate(receipt.get('created_at'), 25):<25} "
            f"{_truncate(receipt.get('why_this_context'), 80)}"
        )


def _cmd_list(args: argparse.Namespace) -> int:
    try:
        receipts = read_recent_receipts(
            limit=args.limit,
            kind=args.kind,
            newest_first=not args.oldest_first,
        )
    except Exception as exc:
        print(f"ERROR: could not read {RECEIPT_STREAM}: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps(receipts, sort_keys=True, indent=2))
    else:
        _print_table(receipts)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="taey-receipts",
        description="Read orchestrator decision receipts from Redis.",
    )
    sub = parser.add_subparsers(dest="command")

    list_parser = sub.add_parser("list", help="List recent decision receipts")
    list_parser.add_argument("--limit", type=int, default=10, help="Maximum receipts to read")
    list_parser.add_argument("--kind", help="Only show receipts of this kind")
    list_parser.add_argument("--oldest-first", action="store_true", help="Read oldest matching stream entries first")
    list_parser.add_argument("--json", action="store_true", help="Print receipts as JSON")
    list_parser.set_defaults(func=_cmd_list)

    args = parser.parse_args(argv)
    if args.command is None:
        args = parser.parse_args(["list", *(argv or [])])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
