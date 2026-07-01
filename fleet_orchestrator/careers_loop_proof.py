from __future__ import annotations

import base64
import json
import os
import re
import shlex
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple


VERIFIED = "VERIFIED"
UNVERIFIED = "UNVERIFIED"
SOURCE = "careers-loop-proof"
_TIMEOUT_SEC = 10
_MAX_SCAN_BYTES = 512 * 1024
_DEFAULT_STATUS_PATHS = ("foundations/careers",)
_EXFIL_PATTERNS = (
    re.compile(r"/home/"),
    re.compile(r"\bapi[_-]?key\b", re.IGNORECASE),
    re.compile(r"\bsk-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bPALIOS\b"),
)


@dataclass(frozen=True)
class CheckResult:
    check: str
    command: str
    expected: Any
    observed: Any
    ok: bool
    detail: str = ""
    verifier: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "check": self.check,
            "command": self.command,
            "expected": self.expected,
            "observed": self.observed,
            "ok": self.ok,
            "detail": self.detail,
            "verifier": self.verifier,
        }


Verifier = Callable[[List[str], Dict[str, Any], Path, str], CheckResult]


def normalize_loop_proof_evidence(value: Any) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("completion evidence loop_proof must be a JSON object")
    normalized = _json_roundtrip(value)
    checks = normalized.get("checks")
    if not isinstance(checks, list) or not checks:
        raise ValueError("completion evidence loop_proof.checks must be a non-empty list")
    for index, check in enumerate(checks):
        if not isinstance(check, dict):
            raise ValueError(f"completion evidence loop_proof.checks[{index}] must be an object")
        if not str(check.get("command") or "").strip():
            raise ValueError(f"completion evidence loop_proof.checks[{index}].command must be non-empty")
    return normalized


def verify_loop_proof_receipt(
    evidence: Dict[str, Any],
    *,
    producer: str = "",
) -> Dict[str, Any]:
    receipt = evidence.get("loop_proof")
    if not isinstance(receipt, dict):
        return _unverified("completion evidence loop_proof must be a JSON object", producer=producer)
    try:
        normalized = normalize_loop_proof_evidence(receipt)
    except ValueError as exc:
        return _unverified(str(exc), producer=producer)
    if normalized is None:
        return _unverified("completion evidence loop_proof is missing", producer=producer)

    step = str(normalized.get("step") or "").strip()
    cycle_id = str(normalized.get("cycle_id") or "").strip()
    canonical_root_text = str(normalized.get("canonical_root") or "").strip()
    canonical_root_error = _canonical_root_error(canonical_root_text)
    if canonical_root_error:
        return _unverified(canonical_root_error, producer=producer, step=step, cycle_id=cycle_id)
    canonical_root = Path(canonical_root_text)

    observations: List[Dict[str, Any]] = []
    failures: List[str] = []
    for index, check in enumerate(normalized["checks"]):
        result = _verify_receipt_check(check, canonical_root, cycle_id)
        observations.append(result.as_dict())
        if result.ok:
            continue
        failures.append(f"check[{index}] {result.check}: {result.detail or 'failed'}")

    drift = _verify_untracked_drift(canonical_root, cycle_id)
    observations.append(drift.as_dict())
    if not drift.ok:
        failures.append(f"untracked drift: {drift.detail or 'failed'}")

    if failures:
        return _unverified(
            "careers loop_proof verification failed: " + "; ".join(failures),
            producer=producer,
            step=step,
            cycle_id=cycle_id,
            canonical_root=canonical_root_text,
            checks=observations,
        )
    return {
        "status": VERIFIED,
        "verified": True,
        "source": SOURCE,
        "repo": str(evidence.get("repo") or "").strip(),
        "commit_sha": str(evidence.get("commit_sha") or "").strip(),
        "required_checks": [],
        "producer": producer,
        "verifier": SOURCE,
        "reason": "careers loop_proof receipt re-verified through trusted allowlisted check implementations",
        "step": step,
        "cycle_id": cycle_id,
        "canonical_root": canonical_root_text,
        "checks": observations,
    }


def _json_roundtrip(value: Dict[str, Any]) -> Dict[str, Any]:
    try:
        return json.loads(json.dumps(value, separators=(",", ":"), sort_keys=True))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"completion evidence loop_proof must be JSON-serializable: {exc}") from exc


def _unverified(
    reason: str,
    *,
    producer: str = "",
    step: str = "",
    cycle_id: str = "",
    canonical_root: str = "",
    checks: Optional[List[Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    return {
        "status": UNVERIFIED,
        "verified": False,
        "source": SOURCE,
        "repo": "",
        "commit_sha": "",
        "required_checks": [],
        "producer": producer,
        "verifier": SOURCE,
        "reason": reason,
        "step": step,
        "cycle_id": cycle_id,
        "canonical_root": canonical_root,
        "checks": checks or [],
    }


def _canonical_root_error(raw: str) -> str:
    if not raw:
        return "loop_proof.canonical_root is required"
    path = Path(raw)
    if not path.is_absolute():
        return "loop_proof.canonical_root must be an absolute canonical repo path"
    if ".peer-worktrees" in path.parts:
        return "loop_proof.canonical_root must not point at a peer worktree"
    return ""


def _verify_receipt_check(check: Dict[str, Any], canonical_root: Path, cycle_id: str) -> CheckResult:
    check_type = str(check.get("check") or check.get("name") or "").strip()
    command = str(check.get("command") or "").strip()
    expected = check.get("expected")
    try:
        tokens = shlex.split(command)
    except ValueError as exc:
        return CheckResult(check_type, command, expected, "", False, f"command is not parseable: {exc}", SOURCE)
    verifier = _REGISTRY.get(check_type)
    if verifier is None:
        return CheckResult(check_type, command, expected, "", False, "check type is not allowlisted", SOURCE)
    result = verifier(tokens, check, canonical_root, cycle_id)
    if not result.ok:
        return result
    if check.get("pass") is not True:
        return CheckResult(check_type, command, expected, result.observed, False, "executor receipt pass field was not true", SOURCE)
    receipt_observed = check.get("observed")
    if receipt_observed is not None and not _observed_matches(receipt_observed, result.observed):
        return CheckResult(
            check_type,
            command,
            expected,
            result.observed,
            False,
            f"executor observed {receipt_observed!r} does not match control observed {result.observed!r}",
            SOURCE,
        )
    return result


def _run(argv: List[str], cwd: Optional[Path] = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        check=False,
        timeout=_TIMEOUT_SEC,
    )


def _git(argv: List[str], root: Path) -> subprocess.CompletedProcess[str]:
    return _run(["git", "-C", str(root), *argv])


def _git_show_head_lines(tokens: List[str], check: Dict[str, Any], root: Path, _cycle_id: str) -> CheckResult:
    check_type, command, expected = _check_meta(check)
    parsed = _parse_git_root(tokens, root)
    if not parsed or len(parsed[1]) != 5 or parsed[1][0] != "show" or parsed[1][2:] != ["|", "wc", "-l"]:
        return CheckResult(check_type, command, expected, "", False, "command must match: git -C <canonical_root> show HEAD:<path> | wc -l", SOURCE)
    spec = parsed[1][1]
    if not spec.startswith("HEAD:"):
        return CheckResult(check_type, command, expected, "", False, "git show target must be HEAD:<relative-path>", SOURCE)
    rel_error = _relative_path_error(spec[5:])
    if rel_error:
        return CheckResult(check_type, command, expected, "", False, rel_error, SOURCE)
    result = _git(["show", spec], root)
    if result.returncode != 0:
        return CheckResult(check_type, command, expected, "", False, _stderr(result), SOURCE)
    observed = len(result.stdout.splitlines())
    ok, detail = _expected_matches(expected, observed)
    return CheckResult(check_type, command, expected, observed, ok, detail, SOURCE)


def _git_log_author(tokens: List[str], check: Dict[str, Any], root: Path, _cycle_id: str) -> CheckResult:
    check_type, command, expected = _check_meta(check)
    parsed = _parse_git_root(tokens, root)
    args = parsed[1] if parsed else []
    if (
        len(args) != 5
        or args[0] != "log"
        or args[1] != "-1"
        or args[2] != "--format=%an"
        or args[3] != "--"
        or _relative_path_error(args[4])
    ):
        return CheckResult(check_type, command, expected, "", False, "command must match: git -C <canonical_root> log -1 --format=%an -- <path>", SOURCE)
    result = _git(["log", "-1", "--format=%an", "--", args[4]], root)
    if result.returncode != 0:
        return CheckResult(check_type, command, expected, "", False, _stderr(result), SOURCE)
    observed = result.stdout.strip()
    ok, detail = _expected_matches(expected, observed)
    return CheckResult(check_type, command, expected, observed, ok, detail, SOURCE)


def _git_diff_stat_changed(tokens: List[str], check: Dict[str, Any], root: Path, _cycle_id: str) -> CheckResult:
    check_type, command, expected = _check_meta(check)
    parsed = _parse_git_root(tokens, root)
    args = parsed[1] if parsed else []
    if len(args) not in (2, 4) or args[:2] != ["diff", "--stat"]:
        return CheckResult(check_type, command, expected, "", False, "command must match: git -C <canonical_root> diff --stat [-- <path>]", SOURCE)
    if len(args) == 4 and (args[2] != "--" or _relative_path_error(args[3])):
        return CheckResult(check_type, command, expected, "", False, "git diff path must be a safe relative path", SOURCE)
    result = _git(args, root)
    if result.returncode != 0:
        return CheckResult(check_type, command, expected, "", False, _stderr(result), SOURCE)
    observed = result.stdout.strip()
    ok, detail = _expected_matches(expected, observed)
    return CheckResult(check_type, command, expected, observed, ok, detail, SOURCE)


def _canonical_commit_on_main(tokens: List[str], check: Dict[str, Any], root: Path, _cycle_id: str) -> CheckResult:
    check_type, command, expected = _check_meta(check)
    parsed = _parse_git_root(tokens, root)
    args = parsed[1] if parsed else []
    if len(args) != 4 or args[:2] != ["merge-base", "--is-ancestor"] or args[3] != "origin/main":
        return CheckResult(check_type, command, expected, "", False, "command must match: git -C <canonical_root> merge-base --is-ancestor <sha> origin/main", SOURCE)
    sha = args[2]
    if not re.fullmatch(r"[0-9a-fA-F]{7,64}", sha):
        return CheckResult(check_type, command, expected, "", False, "commit sha must be 7-64 hex characters", SOURCE)
    result = _git(["merge-base", "--is-ancestor", sha, "origin/main"], root)
    observed = result.returncode == 0
    ok, detail = _expected_matches(expected, observed)
    if result.returncode not in (0, 1):
        ok = False
        detail = _stderr(result)
    return CheckResult(check_type, command, expected, observed, ok, detail, SOURCE)


def _git_status_empty(tokens: List[str], check: Dict[str, Any], root: Path, cycle_id: str) -> CheckResult:
    check_type, command, expected = _check_meta(check)
    parsed = _parse_git_root(tokens, root)
    args = parsed[1] if parsed else []
    if len(args) not in (2, 4) or args[:2] != ["status", "--short"]:
        return CheckResult(check_type, command, expected, "", False, "command must match: git -C <canonical_root> status --short [-- <path>]", SOURCE)
    if len(args) == 4 and (args[2] != "--" or _relative_path_error(args[3])):
        return CheckResult(check_type, command, expected, "", False, "git status path must be a safe relative path", SOURCE)
    result = _git(args, root)
    if result.returncode != 0:
        return CheckResult(check_type, command, expected, "", False, _stderr(result), SOURCE)
    lines = [line for line in result.stdout.splitlines() if line and not _allowed_cycle_status_line(line, cycle_id)]
    observed = "\n".join(lines)
    ok, detail = _expected_matches(expected, observed)
    return CheckResult(check_type, command, expected, observed, ok, detail, SOURCE)


def _http_200(tokens: List[str], check: Dict[str, Any], _root: Path, _cycle_id: str) -> CheckResult:
    check_type, command, expected = _check_meta(check)
    if len(tokens) != 2 or tokens[0] not in {"GET", "http_200"}:
        return CheckResult(check_type, command, expected, "", False, "command must match: GET <http-url>", SOURCE)
    url = tokens[1]
    if not (url.startswith("http://") or url.startswith("https://")):
        return CheckResult(check_type, command, expected, "", False, "URL must be http or https", SOURCE)
    try:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=_TIMEOUT_SEC) as response:
            observed = int(response.status)
    except Exception as exc:
        return CheckResult(check_type, command, expected, "", False, str(exc), SOURCE)
    ok, detail = _expected_matches(expected, observed)
    return CheckResult(check_type, command, expected, observed, ok, detail, SOURCE)


def _gh_readme_substring(tokens: List[str], check: Dict[str, Any], _root: Path, _cycle_id: str) -> CheckResult:
    check_type, command, expected = _check_meta(check)
    if len(tokens) != 3 or tokens[0] != "gh-readme-substring":
        return CheckResult(check_type, command, expected, "", False, "command must match: gh-readme-substring <OWNER/REPO> <quote>", SOURCE)
    repo, quote = tokens[1], tokens[2]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
        return CheckResult(check_type, command, expected, "", False, "repo must be OWNER/REPO", SOURCE)
    result = _run(["gh", "api", f"repos/{repo}/readme"])
    if result.returncode != 0:
        return CheckResult(check_type, command, expected, "", False, _stderr(result), SOURCE)
    try:
        payload = json.loads(result.stdout)
        text = base64.b64decode(str(payload.get("content") or "")).decode("utf-8", "replace")
    except (ValueError, TypeError) as exc:
        return CheckResult(check_type, command, expected, "", False, f"invalid gh readme payload: {exc}", SOURCE)
    observed = quote in text
    ok, detail = _expected_matches(expected, observed)
    return CheckResult(check_type, command, expected, observed, ok, detail, SOURCE)


def _regex_exfil_scan(tokens: List[str], check: Dict[str, Any], root: Path, _cycle_id: str) -> CheckResult:
    check_type, command, expected = _check_meta(check)
    if len(tokens) != 2 or tokens[0] != "regex-exfil-scan" or _relative_path_error(tokens[1]):
        return CheckResult(check_type, command, expected, "", False, "command must match: regex-exfil-scan <relative-path>", SOURCE)
    path = root / tokens[1]
    try:
        data = path.read_text(encoding="utf-8", errors="replace")[:_MAX_SCAN_BYTES]
    except OSError as exc:
        return CheckResult(check_type, command, expected, "", False, str(exc), SOURCE)
    matches = [pattern.pattern for pattern in _EXFIL_PATTERNS if pattern.search(data)]
    observed = "pass" if not matches else ",".join(matches)
    ok, detail = _expected_matches(expected, observed)
    return CheckResult(check_type, command, expected, observed, ok, detail, SOURCE)


def _emdash_count(tokens: List[str], check: Dict[str, Any], root: Path, _cycle_id: str) -> CheckResult:
    check_type, command, expected = _check_meta(check)
    if len(tokens) != 2 or tokens[0] != "emdash-count" or _relative_path_error(tokens[1]):
        return CheckResult(check_type, command, expected, "", False, "command must match: emdash-count <relative-path>", SOURCE)
    try:
        observed = (root / tokens[1]).read_text(encoding="utf-8", errors="replace")[:_MAX_SCAN_BYTES].count("\u2014")
    except OSError as exc:
        return CheckResult(check_type, command, expected, "", False, str(exc), SOURCE)
    ok, detail = _expected_matches(expected, observed)
    return CheckResult(check_type, command, expected, observed, ok, detail, SOURCE)


def _neo4j_placeholder(tokens: List[str], check: Dict[str, Any], _root: Path, _cycle_id: str) -> CheckResult:
    check_type, command, expected = _check_meta(check)
    if len(tokens) < 2 or tokens[0] not in {"neo4j-count", "neo4j-stage"}:
        return CheckResult(check_type, command, expected, "", False, "command must match an allowlisted neo4j-* template", SOURCE)
    return CheckResult(check_type, command, expected, "", False, "neo4j loop-proof checks require a concrete trusted template before use", SOURCE)


def _external_placeholder(tokens: List[str], check: Dict[str, Any], _root: Path, _cycle_id: str) -> CheckResult:
    check_type, command, expected = _check_meta(check)
    if len(tokens) < 2 or tokens[0] not in {"gmail-msgid", "atspi-dom-confirm"}:
        return CheckResult(check_type, command, expected, "", False, "command must match an allowlisted external-observation template", SOURCE)
    return CheckResult(check_type, command, expected, "", False, "external loop-proof checks require a concrete trusted adapter before use", SOURCE)


_REGISTRY: Dict[str, Verifier] = {
    "committed_store_grew": _git_show_head_lines,
    "committed_labels_grew": _git_show_head_lines,
    "git_log_author": _git_log_author,
    "single_writer_author": _git_log_author,
    "git_diff_stat_changed": _git_diff_stat_changed,
    "canonical_commit_on_main": _canonical_commit_on_main,
    "untracked_drift": _git_status_empty,
    "http_200": _http_200,
    "gh_readme_substring": _gh_readme_substring,
    "regex_exfil_scan": _regex_exfil_scan,
    "emdash_count": _emdash_count,
    "neo4j_count": _neo4j_placeholder,
    "neo4j_stage": _neo4j_placeholder,
    "gmail_msgid": _external_placeholder,
    "atspi_dom_confirm": _external_placeholder,
}


def _check_meta(check: Dict[str, Any]) -> Tuple[str, str, Any]:
    return (
        str(check.get("check") or check.get("name") or "").strip(),
        str(check.get("command") or "").strip(),
        check.get("expected"),
    )


def _parse_git_root(tokens: List[str], root: Path) -> Optional[Tuple[Path, List[str]]]:
    if len(tokens) < 4 or tokens[0] != "git" or tokens[1] != "-C":
        return None
    command_root = Path(tokens[2])
    if command_root != root:
        return None
    return command_root, tokens[3:]


def _relative_path_error(path: str) -> str:
    if not path:
        return "relative path is required"
    candidate = Path(path)
    if candidate.is_absolute():
        return "path must be relative to canonical_root"
    if ".." in candidate.parts or ".git" in candidate.parts:
        return "path must not contain '..' or '.git'"
    return ""


def _expected_matches(expected: Any, observed: Any) -> Tuple[bool, str]:
    if expected is None:
        return False, "expected value is required"
    text = str(expected).strip()
    if text.startswith(">"):
        try:
            threshold = float(text[1:].strip())
            actual = float(observed)
        except (TypeError, ValueError):
            return False, f"observed {observed!r} is not numeric for {text!r}"
        return actual > threshold, "" if actual > threshold else f"{actual} is not > {threshold}"
    if text.startswith("=="):
        target = text[2:].strip()
        return str(observed) == target, "" if str(observed) == target else f"{observed!r} != {target!r}"
    if text == "HTTP200":
        return observed == 200, "" if observed == 200 else f"HTTP status {observed!r} != 200"
    if text == "nonempty":
        return bool(str(observed).strip()), "observed value is empty"
    if text == "empty":
        return not str(observed).strip(), f"observed value was not empty: {observed!r}"
    if text.lower() in {"true", "pass", "exit0"}:
        return observed is True or str(observed).lower() in {"true", "pass", "0"}, f"observed {observed!r} is not true/pass"
    return str(observed) == text, "" if str(observed) == text else f"{observed!r} != {text!r}"


def _observed_matches(receipt_observed: Any, control_observed: Any) -> bool:
    return receipt_observed == control_observed or str(receipt_observed) == str(control_observed)


def _verify_untracked_drift(root: Path, cycle_id: str) -> CheckResult:
    status_paths = _status_paths()
    result = _git(["status", "--short", "--", *status_paths], root)
    command = "git -C <canonical_root> status --short -- " + " ".join(status_paths)
    if result.returncode != 0:
        return CheckResult("untracked_drift_smell", command, "empty", "", False, _stderr(result), SOURCE)
    lines = [line for line in result.stdout.splitlines() if line and not _allowed_cycle_status_line(line, cycle_id)]
    observed = "\n".join(lines)
    ok, detail = _expected_matches("empty", observed)
    return CheckResult("untracked_drift_smell", command, "empty", observed, ok, detail, SOURCE)


def _status_paths() -> Tuple[str, ...]:
    raw = os.environ.get("ORCH_CAREERS_CANONICAL_STATUS_PATHS", "")
    values = tuple(part.strip() for part in raw.split(",") if part.strip())
    return values or _DEFAULT_STATUS_PATHS


def _allowed_cycle_status_line(line: str, cycle_id: str) -> bool:
    if not cycle_id:
        return False
    path = line[3:] if len(line) > 3 else line
    normalized = path.strip().strip('"')
    return (
        line.startswith("?? ")
        and "/cycles/" in normalized
        and f"/{cycle_id}/" in normalized
        and "jobseeker" in normalized
    )


def _stderr(result: subprocess.CompletedProcess[str]) -> str:
    return (result.stderr or result.stdout or f"exit {result.returncode}").strip()
