#!/usr/bin/env python3
from __future__ import annotations

import os
import sys
import tempfile
import time
import uuid
from pathlib import Path

from fastapi.testclient import TestClient


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

PREFIX = f"refacc-{uuid.uuid4().hex[:8]}"
os.environ["NOTIFY_KEY_PREFIX"] = PREFIX
if "ORCH_DOTENV" not in os.environ:
    for candidate in (
        ROOT / ".env",
        Path.home() / "claude-code-fleet-orchestrator/.env",
    ):
        if candidate.is_file():
            os.environ["ORCH_DOTENV"] = str(candidate)
            break
os.environ.setdefault("ORCH_REDIS_HOST", "127.0.0.1")
os.environ.setdefault("ORCH_REDIS_PORT", "6379")

from fleet_orchestrator.config import OrchConfig, get_neo4j_driver, get_redis_sync  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    _REF_READ_BYTE_CAP,
    _read_ref_context,
    _stop_block_count_key,
    _stop_block_marker_key,
    complete_project,
    create_phase,
    create_project,
    create_task,
    get_project_summary,
    reset_project,
    resolve_ref_path,
)
from fleet_orchestrator.plan_loader import _META_BLOB_BYTE_CAP, _PLAN_LINE_BYTE_CAP, _parse_plan, _parse_ref  # noqa: E402
from fleet_orchestrator.tasks_api import app  # noqa: E402

CFG = OrchConfig()
CLIENT = TestClient(app)
FAILURES: list[str] = []


def _assert(label: str, condition: bool, detail) -> None:
    if condition:
        print(f"PASS {label}")
        return
    FAILURES.append(label)
    print(f"FAIL {label} {detail}")


def _cleanup(prefix: str) -> None:
    driver = get_neo4j_driver(CFG)
    with driver.session(database=CFG.neo4j_db) as session:
        session.run("MATCH (t:OrchTask) WHERE t.id STARTS WITH $prefix DETACH DELETE t", prefix=prefix)
        session.run("MATCH (ph:OrchPhase) WHERE ph.id STARTS WITH $prefix DETACH DELETE ph", prefix=prefix)
        session.run("MATCH (p:OrchProject) WHERE p.id STARTS WITH $prefix DETACH DELETE p", prefix=prefix)
    redis_client = get_redis_sync(CFG)
    cursor = 0
    while True:
        cursor, keys = redis_client.scan(cursor=cursor, match=f"{prefix}:*", count=100)
        if keys:
            redis_client.delete(*keys)
        if cursor == 0:
            break


def _project_fixture(name: str, *, owner: str = "worker-a") -> str:
    project_id = f"{PREFIX}-{name}-proj"
    phase_id = f"{PREFIX}-{name}-phase"
    task_id = f"{PREFIX}-{name}-task"
    create_project(project_id, f"{name} project", supervisor="conductor", priority=1, config=CFG)
    create_phase(project_id, phase_id, "Main", config=CFG)
    create_task(phase_id, task_id, "pending task", owner=owner, priority=5, wake_owner_if_ready=False, config=CFG)
    return project_id


def _benchmark_parse_ms(blocks: int) -> float:
    line = "# Project: proj - Name " + " ".join("[tag: x]" for _ in range(blocks))
    doc = f"{line}\n"
    timings = []
    for _ in range(3):
        start = time.perf_counter()
        parsed = _parse_plan(doc)
        timings.append((time.perf_counter() - start) * 1000.0)
        if parsed["project"] is None:
            raise AssertionError("project parse failed")
    return min(timings)


def _benchmark_malformed_parse_ms(blocks: int) -> tuple[float, dict]:
    line = "# Project: proj - Name " + (" [" * blocks) + "x"
    doc = f"{line}\n"
    timings = []
    last_parsed = None
    for _ in range(3):
        start = time.perf_counter()
        parsed = _parse_plan(doc)
        timings.append((time.perf_counter() - start) * 1000.0)
        last_parsed = parsed
    assert last_parsed is not None
    return min(timings), last_parsed


def main() -> int:
    _cleanup(PREFIX)
    try:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            os.environ["ORCH_REF_ALLOWED_ROOT"] = str(root)

            plan_dir = root / "plans"
            plan_dir.mkdir()
            plan_path = plan_dir / "plan.md"
            plan_path.write_text("# stub\n", encoding="utf-8")

            source_dir = plan_dir / "src"
            source_dir.mkdir()
            in_root = source_dir / "module.py"
            in_root.write_text("line1\nline2\nline3\n", encoding="utf-8")

            resolved, warning = resolve_ref_path("/etc/passwd", str(plan_path))
            _assert("absolute-path-rejected", resolved is None and warning == "ref outside allowed root: /etc/passwd", (resolved, warning))

            no_root = _read_ref_context(
                [{"path": "src/module.py", "l_start": 1, "l_end": 2}],
                source_path=None,
                line_cap=200,
            )
            no_root_first = no_root["refs"][0]
            _assert(
                "no-source-root-rejected",
                no_root_first.get("warning") == "ref has no plan-source root (sandbox undefined)" and "content" not in no_root_first,
                no_root_first,
            )

            _assert("null-byte-parse-rejected", _parse_ref("bad\x00path:1-2") is None, _parse_ref("bad\x00path:1-2"))
            null_ctx = _read_ref_context(
                [{"path": "bad\x00path", "l_start": 1, "l_end": 2}],
                source_path=str(plan_path),
                line_cap=200,
            )
            _assert(
                "null-byte-graceful",
                null_ctx["refs"][0].get("warning") == "ref unreadable: control characters in path",
                null_ctx["refs"][0],
            )

            resolved, warning = resolve_ref_path("../secrets.txt", str(plan_path))
            _assert("dotdot-escape-rejected", resolved is None and warning == "ref outside allowed root: ../secrets.txt", (resolved, warning))

            outside = root / "outside.txt"
            outside.write_text("outside\n", encoding="utf-8")
            symlink_path = plan_dir / "escape.txt"
            os.symlink(outside, symlink_path)
            resolved, warning = resolve_ref_path("escape.txt", str(plan_path))
            _assert("symlink-escape-rejected", resolved is None and warning == "ref outside allowed root: escape.txt", (resolved, warning))

            bad_source_resp = CLIENT.post(
                "/api/projects/load-md",
                json={
                    "md_text": "# Project: bad-root - Bad [ref: src/module.py:1-2]\n",
                    "source_path": "/fake.md",
                    "source_kind": "markdown",
                    "ingested_by": "tester",
                    "supervisor": "conductor",
                },
            )
            _assert("crafted-source-path-rejected", bad_source_resp.status_code == 422, bad_source_resp.text)

            saved_allowed_root = os.environ.pop("ORCH_REF_ALLOWED_ROOT")
            no_root_resp = CLIENT.post(
                "/api/projects/load-md",
                json={
                    "md_text": "# Project: missing-root - Bad [ref: src/module.py:1-2]\n",
                    "source_path": str(plan_path),
                    "source_kind": "markdown",
                    "ingested_by": "tester",
                    "supervisor": "conductor",
                },
            )
            _assert("allowed-root-required", no_root_resp.status_code == 422, no_root_resp.text)
            os.environ["ORCH_REF_ALLOWED_ROOT"] = saved_allowed_root

            ctx = _read_ref_context(
                [{"path": "src/module.py", "l_start": 2, "l_end": 3}],
                source_path=str(plan_path),
                line_cap=200,
            )
            first = ctx["refs"][0]
            ok_first = first.get("content") == "line2\nline3" and "resolved_path" not in first and not first.get("warning")
            in_root.write_text("line1\nupdated2\nupdated3\n", encoding="utf-8")
            ctx_updated = _read_ref_context(
                [{"path": "src/module.py", "l_start": 2, "l_end": 3}],
                source_path=str(plan_path),
                line_cap=200,
            )
            second = ctx_updated["refs"][0]
            ok_second = second.get("content") == "updated2\nupdated3" and "resolved_path" not in second and not second.get("warning")
            _assert("in-root-fresh-read", ok_first and ok_second, (first, second))

            fifo_path = plan_dir / "stream.pipe"
            os.mkfifo(fifo_path)
            fifo_ctx = _read_ref_context(
                [{"path": "stream.pipe", "l_start": 1, "l_end": 2}],
                source_path=str(plan_path),
                line_cap=200,
            )
            _assert(
                "fifo-refused",
                fifo_ctx["refs"][0].get("warning") == "ref unreadable: stream.pipe:1-2 (not a regular file)",
                fifo_ctx["refs"][0],
            )

            big_path = plan_dir / "too-big.txt"
            big_path.write_bytes(b"x" * (_REF_READ_BYTE_CAP + 1))
            oversize = _read_ref_context(
                [{"path": "too-big.txt", "l_start": 1, "l_end": 5}],
                source_path=str(plan_path),
                line_cap=200,
            )
            oversize_first = oversize["refs"][0]
            expected_warning = f"ref unreadable: too-big.txt:1-5 (file exceeds byte cap {_REF_READ_BYTE_CAP})"
            _assert(
                "oversize-file-refused",
                oversize_first.get("warning") == expected_warning and "content" not in oversize_first,
                oversize_first,
            )

            bench_sizes = [4, 8, 16, 32]
            bench_ms = [_benchmark_parse_ms(size) for size in bench_sizes]
            ratios = [bench_ms[idx + 1] / max(bench_ms[idx], 0.001) for idx in range(len(bench_ms) - 1)]
            linear_ok = all(ratio < 3.0 for ratio in ratios)
            _assert("linear-parse-benchmark", linear_ok, {"sizes": bench_sizes, "ms": bench_ms, "ratios": ratios})
            print(f"BENCH parse-ms blocks={bench_sizes} values={[round(v, 3) for v in bench_ms]}")

            malformed_sizes = [1000, 2000, 4000, 8000, 16000]
            malformed_results = [_benchmark_malformed_parse_ms(size) for size in malformed_sizes]
            malformed_ms = [item[0] for item in malformed_results]
            malformed_parsed = malformed_results[-1][1]
            malformed_ok = max(malformed_ms) < 250.0
            malformed_warning = f"line 1: skipped overlong line (> {_PLAN_LINE_BYTE_CAP} bytes)"
            _assert(
                "overlong-line-bounded",
                malformed_ok and malformed_warning in malformed_parsed.get("warnings", []),
                {"sizes": malformed_sizes, "ms": malformed_ms, "warnings": malformed_parsed.get("warnings")},
            )
            print(f"BENCH malformed-parse-ms blocks={malformed_sizes} values={[round(v, 3) for v in malformed_ms]}")

            dense_meta_line = "# Project: proj - Name " + ("[tag:x]" * 80)
            dense_meta = _parse_plan(f"{dense_meta_line}\n")
            dense_warning = f"line 1: meta blob exceeds {_META_BLOB_BYTE_CAP} bytes"
            _assert(
                "meta-blob-cap-bounded",
                dense_warning in dense_meta.get("warnings", []) and dense_meta.get("project") is None,
                dense_meta,
            )

            project_ref_id = f"{PREFIX}-project-ref"
            create_project(
                project_ref_id,
                "project ref",
                supervisor="conductor",
                priority=1,
                refs=[{"path": "src/module.py", "l_start": 1, "l_end": 2}],
                source_path=str(plan_path),
                config=CFG,
            )
            project_summary = get_project_summary(project_ref_id, config=CFG)
            project_ref = project_summary["project"]["ref_context"]["refs"][0] if project_summary else {}
            _assert("project-ref-context-works", project_ref.get("content") == "line1\nupdated2", project_ref)

            force_cases = [
                ("force-false-bool", {"force": False}, 409),
                ("force-false-string", {"force": "false"}, 422),
                ("force-zero-int", {"force": 0}, 422),
                ("force-zero-string", {"force": "0"}, 422),
                ("force-absent", {}, 409),
            ]
            for label, body, expected_status in force_cases:
                project_id = _project_fixture(label)
                response = CLIENT.post(f"/api/projects/{project_id}/complete", json=body)
                _assert(label, response.status_code == expected_status, response.text)

            non_object_project = _project_fixture("force-list-body")
            non_object_resp = CLIENT.request("POST", f"/api/projects/{non_object_project}/complete", json=["force", True])
            _assert("force-non-object-body", non_object_resp.status_code == 422, non_object_resp.text)

            force_true_project = _project_fixture("force-true")
            force_true = CLIENT.post(f"/api/projects/{force_true_project}/complete", json={"force": True})
            _assert("force-true-bypasses", force_true.status_code == 200 and force_true.json().get("force") is True, force_true.text)
            _assert("complete-project-direct-conflict", _complete_conflicts(), "complete_project should conflict on unfinished work")

            project_a = _project_fixture("reset-a", owner="shared-session")
            _project_fixture("reset-b", owner="shared-session")
            redis_client = get_redis_sync(CFG)
            marker_key = _stop_block_marker_key("shared-session")
            count_key = _stop_block_count_key("shared-session")
            redis_client.set(marker_key, "still-here")
            redis_client.set(count_key, "2")
            reset_project(project_a, reset_by="tester", config=CFG)
            _assert(
                "reset-keeps-session-global-convergence",
                redis_client.get(marker_key) == "still-here" and redis_client.get(count_key) == "2",
                {"marker": redis_client.get(marker_key), "count": redis_client.get(count_key)},
            )

            if os.environ.get("REF_ACCEPTANCE_INJECT_FAIL") == "1":
                _assert("injected-fail", False, "REF_ACCEPTANCE_INJECT_FAIL=1")
        return 1 if FAILURES else 0
    finally:
        _cleanup(PREFIX)


def _complete_conflicts() -> bool:
    project_id = _project_fixture("complete-conflict")
    try:
        complete_project(project_id, force=False, completed_by="tester", config=CFG)
    except Exception as exc:
        return exc.__class__.__name__ == "ReadyWorkConflictError"
    return False


if __name__ == "__main__":
    raise SystemExit(main())
