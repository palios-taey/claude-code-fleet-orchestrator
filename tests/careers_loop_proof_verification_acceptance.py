#!/usr/bin/env python3
from __future__ import annotations

import inspect
import http.server
import shlex
import socketserver
import subprocess
import sys
import tempfile
import threading
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fleet_orchestrator import careers_loop_proof, evidence_verification  # noqa: E402
from fleet_orchestrator.orch_schema import _normalize_completion_evidence  # noqa: E402


FAILURES: list[str] = []
STORE = "foundations/careers/jobseeker/scorer_eval/continuous_eval_labels.jsonl"


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _run(argv: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(argv, cwd=str(cwd), capture_output=True, text=True, check=False)


def _must(argv: list[str], cwd: Path) -> None:
    result = _run(argv, cwd)
    if result.returncode != 0:
        raise AssertionError(f"{argv!r} failed: {result.stderr or result.stdout}")


def _repo(author: str, lines: int) -> Path:
    repo = Path(tempfile.mkdtemp(prefix="careers-loop-proof-"))
    _must(["git", "init", "-b", "main"], repo)
    _must(["git", "config", "user.email", f"{author}@example.invalid"], repo)
    _must(["git", "config", "user.name", author], repo)
    path = repo / STORE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(f"label-{i}\n" for i in range(lines)), encoding="utf-8")
    _must(["git", "add", STORE], repo)
    _must(["git", "commit", "-m", "labels"], repo)
    _must(["git", "remote", "add", "origin", str(repo)], repo)
    _must(["git", "update-ref", "refs/remotes/origin/main", "HEAD"], repo)
    return repo


def _receipt(repo: Path, *, lines: int, author: str = "job-seeker", extra_checks: list[dict] | None = None) -> dict:
    root = shlex.quote(str(repo))
    checks = [
        {
            "name": "committed_labels_grew",
            "command": f"git -C {root} show HEAD:{STORE} | wc -l",
            "expected": ">1",
            "observed": lines,
            "pass": True,
        },
        {
            "name": "single_writer_author",
            "command": f"git -C {root} log -1 --format=%an -- {STORE}",
            "expected": "job-seeker",
            "observed": author,
            "pass": True,
        },
        {
            "name": "untracked_drift",
            "command": f"git -C {root} status --short -- foundations/careers",
            "expected": "empty",
            "observed": "",
            "pass": True,
        },
    ]
    if extra_checks:
        checks.extend(extra_checks)
    return {
        "step": "js-7",
        "cycle_id": "cycle-001",
        "canonical_root": str(repo),
        "verdict": "PASS",
        "checks": checks,
    }


def _verify(receipt: dict) -> dict:
    normalized = _normalize_completion_evidence({"loop_proof": receipt})
    return evidence_verification.verify_completion_evidence(normalized, producer="acceptance") or {}


class _CountingHTTPHandler(http.server.BaseHTTPRequestHandler):
    hits: dict[str, int] = {}

    def do_GET(self) -> None:
        self.__class__.hits[self.path] = self.__class__.hits.get(self.path, 0) + 1
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/ok")
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, _format: str, *_args: object) -> None:
        return


class _ThreadingHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True


def _local_server() -> tuple[_ThreadingHTTPServer, str]:
    _CountingHTTPHandler.hits = {}
    server = _ThreadingHTTPServer(("127.0.0.1", 0), _CountingHTTPHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    return server, f"http://{host}:{port}"


def main() -> int:
    valid_repo = _repo("job-seeker", 3)
    valid_receipt = _receipt(valid_repo, lines=3)
    normalized = _normalize_completion_evidence({"loop_proof": valid_receipt})
    valid = evidence_verification.verify_completion_evidence(normalized, producer="acceptance") or {}
    _check("loop_proof-only completion evidence is shape-valid", "loop_proof" in normalized and "production_observation" not in normalized, normalized)
    _check("valid canonical receipt verifies", valid.get("status") == "VERIFIED" and valid.get("source") == "careers-loop-proof", valid)

    worktree_only = _repo("job-seeker", 1)
    (worktree_only / STORE).write_text("label-0\nlabel-1\nlabel-2\n", encoding="utf-8")
    worktree_only_result = _verify(_receipt(worktree_only, lines=3))
    _check(
        "working-tree-only growth fails committed-store check",
        worktree_only_result.get("status") == "UNVERIFIED" and "committed_labels_grew" in worktree_only_result.get("reason", ""),
        worktree_only_result,
    )

    wrong_author = _repo("conductor-codex", 3)
    wrong_author_result = _verify(_receipt(wrong_author, lines=3, author="conductor-codex"))
    _check(
        "wrong canonical author fails single-writer check",
        wrong_author_result.get("status") == "UNVERIFIED" and "single_writer_author" in wrong_author_result.get("reason", ""),
        wrong_author_result,
    )

    injected = _receipt(
        valid_repo,
        lines=3,
        extra_checks=[
            {
                "name": "committed_labels_grew",
                "command": "git -C /tmp show HEAD:foundations/careers/jobseeker/scorer_eval/continuous_eval_labels.jsonl; cat /home/mira/.ssh/id_rsa",
                "expected": ">1",
                "observed": 3,
                "pass": True,
            }
        ],
    )
    injected_result = _verify(injected)
    _check(
        "non-allowlisted command shape fails closed",
        injected_result.get("status") == "UNVERIFIED" and "command must match" in injected_result.get("reason", ""),
        injected_result,
    )

    stray = _repo("job-seeker", 3)
    (stray / "foundations/careers/stray.txt").write_text("producer smell\n", encoding="utf-8")
    stray_result = _verify(_receipt(stray, lines=3))
    _check(
        "untracked canonical careers drift fails close",
        stray_result.get("status") == "UNVERIFIED" and "untracked drift" in stray_result.get("reason", ""),
        stray_result,
    )

    server, base_url = _local_server()
    try:
        ssrf = _receipt(
            valid_repo,
            lines=3,
            extra_checks=[
                {
                    "name": "http_200",
                    "command": f"GET {base_url}/ok",
                    "expected": "HTTP200",
                    "observed": 200,
                    "pass": True,
                }
            ],
        )
        ssrf_result = _verify(ssrf)
        _check(
            "loopback http_200 is blocked before request",
            ssrf_result.get("status") == "UNVERIFIED"
            and "blocked non-public" in ssrf_result.get("reason", "")
            and not _CountingHTTPHandler.hits,
            {"result": ssrf_result, "hits": dict(_CountingHTTPHandler.hits)},
        )

        redirect = _receipt(
            valid_repo,
            lines=3,
            extra_checks=[
                {
                    "name": "http_200",
                    "command": f"GET {base_url}/redirect",
                    "expected": "HTTP200",
                    "observed": 200,
                    "pass": True,
                }
            ],
        )
        with mock.patch.object(careers_loop_proof, "_public_http_url_error", return_value=""):
            redirect_result = _verify(redirect)
        _check(
            "http_200 does not follow redirects",
            redirect_result.get("status") == "UNVERIFIED"
            and _CountingHTTPHandler.hits.get("/redirect") == 1
            and _CountingHTTPHandler.hits.get("/ok", 0) == 0,
            {"result": redirect_result, "hits": dict(_CountingHTTPHandler.hits)},
        )
    finally:
        server.shutdown()
        server.server_close()

    source = inspect.getsource(careers_loop_proof)
    _check("verifier never enables shell execution", "shell=True" not in source, "shell=True found")

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} careers loop-proof assertion(s): {FAILURES}")
        return 1
    print("\nPASS - careers loop-proof receipts are independently re-verified and fail closed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
