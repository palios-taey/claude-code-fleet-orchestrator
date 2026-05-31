#!/usr/bin/env python3
"""Mechanical integrity gate for the fleet orchestrator.

This is NOT a test. It is a static constraint on the code that runs at
commit time (pre-commit hook) and in CI. It cannot be gamed by writing a
passing assertion, because there is nothing to assert — it greps the real
source for the specific patterns Jesse named as non-negotiable:

  - silent fallbacks / error swallowing (bare except, except: pass,
    finally: pass, check=False on subprocess)
  - hardcoded operator-specific paths (/path/to/repo)
  - hardcoded internal network endpoints (10.0.0.x, 192.168.100.x)
  - default-bind to all interfaces (0.0.0.0) without explicit opt-in

Philosophy (FAMILY_KERNEL / 6SIGMA): no silent fallbacks, fail loud,
surface issues do not hide them. A finding here is a FULL STOP -> 6SIGMA
root-cause analysis (GitNexus impact + code review), not a quick patch.

Usage:
  lint_no_silent_fallbacks.py --all              # scan whole src tree (baseline/CI)
  lint_no_silent_fallbacks.py --staged           # scan git-staged python (pre-commit)
  lint_no_silent_fallbacks.py path/a.py path/b.py# scan explicit files

Exit code: 0 = clean, 1 = violations found (build/commit must fail),
2 = invocation error. No findings are downgraded to warnings; every
pattern below is a hard fail. To intentionally allow a specific line,
append a trailing  # lint-allow: <reason>  comment naming WHY — the
reason becomes part of the audit trail and an empty reason still fails.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# (pattern, label, why-it-matters). Each is a hard fail unless the line
# carries a `# lint-allow: <non-empty reason>`.
RULES: list[tuple[re.Pattern, str, str]] = [
    (re.compile(r"^\s*except\s*:"), "bare-except",
     "bare 'except:' swallows everything incl. KeyboardInterrupt/SystemExit"),
    (re.compile(r"except\b[^:\n]*:\s*pass\s*$"), "except-pass",
     "exception caught and silently discarded"),
    (re.compile(r"\bcheck\s*=\s*False\b"), "subprocess-check-false",
     "subprocess failure swallowed; task can be claimed while its prompt vanishes (F6)"),
    (re.compile(r"/path/to/repo\b"), "hardcoded-home-mira",
     "operator-specific path; route through config + fail loud if unset (F4)"),
    (re.compile(r"\b10\.0\.0\.\d{1,3}\b"), "hardcoded-internal-ip",
     "internal network endpoint baked into code; must be config (F4)"),
    (re.compile(r"\b192\.168\.100\.\d{1,3}\b"), "hardcoded-internal-ip",
     "internal network endpoint baked into code; must be config (F4)"),
    (re.compile(r"0\.0\.0\.0"), "bind-all-interfaces",
     "default-bind to all interfaces exposes write endpoints to the network (F1)"),
]

ALLOW_RE = re.compile(r"#\s*lint-allow:\s*(.*)$")


@dataclass
class Finding:
    path: str
    line_no: int
    label: str
    why: str
    text: str


def scan_file(path: Path) -> list[Finding]:
    findings: list[Finding] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (UnicodeDecodeError, FileNotFoundError):
        # Not silent: a file we cannot read is reported and fails loud at the
        # caller via the unreadable list, not swallowed here.
        raise
    for i, line in enumerate(lines, start=1):
        for pattern, label, why in RULES:
            if not pattern.search(line):
                continue
            allow = ALLOW_RE.search(line)
            if allow and allow.group(1).strip():
                continue  # explicitly justified with a non-empty reason
            findings.append(Finding(str(path), i, label, why, line.strip()))

    # Dead-finally check (F8): a `finally:` whose entire body is a single
    # `pass`. Legitimate cleanup (`finally: driver.close()`) is NOT flagged —
    # only the empty-cleanup anti-pattern that invites future devs to add
    # driver-closing code that would break the singleton.
    for i, line in enumerate(lines):
        if not re.match(r"^\s*finally\s*:\s*$", line):
            continue
        body = [l for l in lines[i + 1 : i + 4] if l.strip()]
        if body and re.match(r"^\s*pass\s*$", body[0]):
            allow = ALLOW_RE.search(line)
            if allow and allow.group(1).strip():
                continue
            findings.append(Finding(
                str(path), i + 1, "finally-pass",
                "finally: pass is a dead cleanup block; delete it (F8)",
                line.strip()))
    return findings


def staged_python_files() -> list[Path]:
    out = subprocess.run(
        ["git", "diff", "--cached", "--name-only", "--diff-filter=ACM"],
        capture_output=True, text=True, check=True,
    ).stdout
    return [Path(p) for p in out.splitlines() if p.endswith(".py") and Path(p).exists()]


def all_python_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--all", action="store_true", help="scan whole src/ tree")
    g.add_argument("--staged", action="store_true", help="scan git-staged python files")
    ap.add_argument("files", nargs="*", help="explicit files to scan")
    args = ap.parse_args()

    if args.all:
        targets = all_python_files(Path("src"))
        if not targets:
            # Fail loud: --all scanning nothing means wrong cwd or moved
            # layout, NOT a pass. A silent CLEAN here is exactly the kind of
            # false-green this gate exists to prevent.
            print("integrity gate ERROR — --all found 0 python files under src/ "
                  f"(cwd={Path.cwd()}). Run from repo root with a src/ layout.",
                  file=sys.stderr)
            return 2
    elif args.staged:
        targets = staged_python_files()
    elif args.files:
        targets = [Path(f) for f in args.files]
    else:
        ap.error("specify --all, --staged, or explicit files")
        return 2

    # Self-exclude: this file IS the detector, so it definitionally contains
    # every pattern string. Scanning it produces only false positives. This
    # is the single justified exemption; everything else is scanned.
    self_path = Path(__file__).resolve()
    targets = [p for p in targets if p.resolve() != self_path]

    findings: list[Finding] = []
    for path in targets:
        findings.extend(scan_file(path))

    if not findings:
        print(f"integrity gate CLEAN — {len(targets)} file(s) scanned, 0 findings")
        return 0

    by_label: dict[str, int] = {}
    for f in findings:
        by_label[f.label] = by_label.get(f.label, 0) + 1
        print(f"{f.path}:{f.line_no}: [{f.label}] {f.text}")
        print(f"    -> {f.why}")

    print()
    print(f"integrity gate FAIL — {len(findings)} finding(s) across {len(targets)} file(s):")
    for label, n in sorted(by_label.items(), key=lambda kv: -kv[1]):
        print(f"    {n:>3}  {label}")
    print()
    print("Per 6SIGMA: a finding is a FULL STOP. Root-cause it (GitNexus impact +")
    print("code review), do not patch around it. To intentionally allow a line, add")
    print("'# lint-allow: <reason>' naming WHY packaging-native/config won't work.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
