#!/usr/bin/env python3
"""Acceptance: stale task reconciliation removes dead ready/live work.

The stop engine can only reason about tracker state. If a worker records an
interrupted/error outcome but no supervisor terminalizes the task, the old task
can sit pending/in_progress forever and poison next-ready/stop decisions. The
watch reaper must terminalize those proven non-success outcomes, and it must
auto-close only PR follow-up/audit tasks whose referenced PR is merged with
required gates verified.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import tempfile
import textwrap
import uuid
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _require_test_namespace() -> str:
    raw = (os.environ.get("ORCH_TEST_NAMESPACE") or "").strip()
    if not raw:
        raise SystemExit("ORCH_TEST_NAMESPACE is required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{5,80}", raw):
        raise SystemExit("ORCH_TEST_NAMESPACE must be 6-80 chars of letters/digits/._:-")
    if not any(marker in raw.lower() for marker in ("test", "ci", "acceptance")):
        raise SystemExit("ORCH_TEST_NAMESPACE must include test, ci, or acceptance")
    return raw


PFX = f"{_require_test_namespace()}-reconcile-{uuid.uuid4().hex[:8]}"
os.environ["NOTIFY_KEY_PREFIX"] = PFX

from fleet_orchestrator.config import OrchConfig, get_neo4j_driver  # noqa: E402
from fleet_orchestrator.dispatch import _state_key  # noqa: E402
from fleet_orchestrator.notify_state import redis_connect as notify_redis_connect  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    create_phase,
    create_project,
    create_task,
    get_task,
    init_schema,
    update_task_status,
)


CFG = OrchConfig()
REPO = "palios-taey/claude-code-fleet-orchestrator"
GREEN_SHA = "a" * 40
RED_SHA = "b" * 40
SUP = f"{PFX}-sup"
WORKER = f"{SUP}-codex"
ERROR_WORKER = f"{SUP}-err-codex"
DONE_WORKER = f"{SUP}-done-codex"
MISMATCH_WORKER = f"{SUP}-mismatch-codex"
PROJECT = f"{PFX}-project"
PHASE = f"{PROJECT}::phase"
INTERRUPTED = f"{PROJECT}::interrupted"
ERROR_TASK = f"{PROJECT}::error"
DONE_TASK = f"{PROJECT}::done"
MISMATCH = f"{PROJECT}::mismatch"
PR_AUDIT = f"{PROJECT}::r5-audit-green"
PR_RED = f"{PROJECT}::r5-audit-red"
PR_PLAIN = f"{PROJECT}::plain-pr-mention"
FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _redis():
    return notify_redis_connect()


def _cleanup() -> None:
    r = _redis()
    for pattern in (f"{PFX}:*",):
        cursor = 0
        while True:
            cursor, keys = r.scan(cursor=cursor, match=pattern, count=100)
            if keys:
                r.delete(*keys)
            if cursor == 0:
                break
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=PFX)


def _bind(worker: str, task_id: str, outcome: str, details: str, *, include_task_id: bool = True) -> None:
    r = _redis()
    r.set(
        _state_key(worker, "current_task"),
        json.dumps({"task_id": task_id, "description": task_id, "supervisor": SUP, "started_at": 123.0}),
    )
    payload = {"outcome": outcome, "details": details}
    if include_task_id:
        payload["task_id"] = task_id
    r.set(_state_key(worker, "last_outcome"), json.dumps(payload))


def _current_task_id(worker: str) -> str:
    raw = _redis().get(_state_key(worker, "current_task"))
    if not raw:
        return ""
    return str(json.loads(raw).get("task_id") or "")


def _write_fake_gh(directory: Path) -> None:
    gh = directory / "gh"
    gh.write_text(
        textwrap.dedent(
            f"""\
            #!/usr/bin/env python3
            import json
            import sys

            if len(sys.argv) < 3 or sys.argv[1] != "api":
                print("unsupported fake gh invocation", file=sys.stderr)
                sys.exit(2)

            path = sys.argv[2]
            pulls = {{
                "repos/{REPO}/pulls/17": {{"merged": True, "merged_at": "2026-06-30T00:00:00Z", "head": {{"sha": "{GREEN_SHA}"}}, "merge_commit_sha": "{GREEN_SHA}"}},
                "repos/{REPO}/pulls/18": {{"merged": True, "merged_at": "2026-06-30T00:00:00Z", "head": {{"sha": "{RED_SHA}"}}, "merge_commit_sha": "{RED_SHA}"}},
                "repos/{REPO}/pulls/19": {{"merged": False, "merged_at": None, "head": {{"sha": "{GREEN_SHA}"}}, "merge_commit_sha": ""}},
            }}
            if path in pulls:
                print(json.dumps(pulls[path]))
                sys.exit(0)

            for sha, mode in (("{GREEN_SHA}", "green"), ("{RED_SHA}", "red")):
                if path == f"repos/{REPO}/commits/{{sha}}":
                    print(json.dumps({{"sha": sha}}))
                    sys.exit(0)
                if path == f"repos/{REPO}/commits/{{sha}}/check-runs?per_page=100":
                    conclusion = "success" if mode == "green" else "failure"
                    print(json.dumps({{
                        "check_runs": [
                            {{"name": "ship-gate-acceptance", "status": "completed", "conclusion": conclusion, "completed_at": "2026-06-30T00:00:01Z", "app": {{"slug": "github-actions"}}}}
                        ]
                    }}))
                    sys.exit(0)
                if path == f"repos/{REPO}/commits/{{sha}}/statuses?per_page=100":
                    print(json.dumps([
                        {{"context": "r5-audit-gate", "state": "success", "created_at": "2026-06-30T00:00:00Z", "creator": {{"login": "github-actions[bot]"}}}}
                    ]))
                    sys.exit(0)

            print(f"fake gh: not found {{path}}", file=sys.stderr)
            sys.exit(1)
            """
        )
    )
    gh.chmod(0o755)


def _setup() -> None:
    init_schema(config=CFG)
    create_project(project_id=PROJECT, name=PROJECT, supervisor=SUP, priority=1, config=CFG)
    create_phase(project_id=PROJECT, phase_id=PHASE, name="phase", config=CFG)
    create_task(phase_id=PHASE, task_id=INTERRUPTED, description="pending interrupted zombie", owner=WORKER, priority=10, wake_owner_if_ready=False, config=CFG)
    create_task(phase_id=PHASE, task_id=ERROR_TASK, description="in-progress error zombie", owner=ERROR_WORKER, priority=10, wake_owner_if_ready=False, config=CFG)
    create_task(phase_id=PHASE, task_id=DONE_TASK, description="done outcome awaits supervisor control", owner=DONE_WORKER, priority=10, wake_owner_if_ready=False, config=CFG)
    create_task(phase_id=PHASE, task_id=MISMATCH, description="mismatched terminal outcome is not proof", owner=MISMATCH_WORKER, priority=10, wake_owner_if_ready=False, config=CFG)
    create_task(phase_id=PHASE, task_id=PR_AUDIT, description="R5 audit for PR #17", owner=SUP, priority=10, wake_owner_if_ready=False, config=CFG)
    create_task(phase_id=PHASE, task_id=PR_RED, description="Gatekeeper review for PR #18", owner=SUP, priority=10, wake_owner_if_ready=False, config=CFG)
    create_task(phase_id=PHASE, task_id=PR_PLAIN, description="Implementation notes mention PR #17 in prose only", owner=SUP, priority=10, wake_owner_if_ready=False, config=CFG)
    update_task_status(ERROR_TASK, "in_progress", owner=ERROR_WORKER, config=CFG)
    _bind(WORKER, INTERRUPTED, "interrupted", f"interrupted by restart [task_id={INTERRUPTED}]")
    _bind(ERROR_WORKER, ERROR_TASK, "error", f"tool failed [task_id={ERROR_TASK}]")
    _bind(DONE_WORKER, DONE_TASK, "done", f"done [task_id={DONE_TASK}]")
    _bind(MISMATCH_WORKER, MISMATCH, "interrupted", "interrupted different task", include_task_id=False)


def main() -> int:
    _cleanup()
    tmp = Path(tempfile.mkdtemp(prefix=f"{PFX}-gh-"))
    env_keys = (
        "PATH",
        "ORCH_COMPLETION_GITHUB_REPO",
        "ORCH_COMPLETION_ALLOWED_REPOS",
        "ORCH_COMPLETION_REQUIRED_CHECKS",
        "ORCH_COMPLETION_TRUSTED_CHECK_RUN_APPS",
        "ORCH_COMPLETION_TRUSTED_STATUS_CREATORS",
    )
    previous_env = {key: os.environ.get(key) for key in env_keys}
    try:
        _write_fake_gh(tmp)
        os.environ["PATH"] = f"{tmp}{os.pathsep}{os.environ['PATH']}"
        os.environ["ORCH_COMPLETION_GITHUB_REPO"] = REPO
        os.environ["ORCH_COMPLETION_ALLOWED_REPOS"] = REPO
        os.environ["ORCH_COMPLETION_REQUIRED_CHECKS"] = "r5-audit-gate,ship-gate-acceptance"
        os.environ["ORCH_COMPLETION_TRUSTED_CHECK_RUN_APPS"] = "github-actions"
        os.environ["ORCH_COMPLETION_TRUSTED_STATUS_CREATORS"] = "github-actions[bot]"
        _setup()

        watch = __import__("fleet_orchestrator.cli_orch_watch", fromlist=["dummy"])
        sent: list[tuple[str, str]] = []

        def fake_send(_r, target, body, **_kwargs):
            sent.append((target, body))
            return True

        with mock.patch.object(watch, "_target_stop_decision_allows_stop", return_value=False), \
             mock.patch.object(watch, "_send_wake", side_effect=fake_send):
            wake_count = watch._process_task_reconciliations(
                _redis(),
                dedup_ttl_sec=1,
                task_id_prefix=PFX,
                project_id_prefix=PFX,
            )

        interrupted = get_task(INTERRUPTED, config=CFG)
        error_task = get_task(ERROR_TASK, config=CFG)
        done_task = get_task(DONE_TASK, config=CFG)
        mismatch = get_task(MISMATCH, config=CFG)
        pr_audit = get_task(PR_AUDIT, config=CFG)
        pr_red = get_task(PR_RED, config=CFG)
        pr_plain = get_task(PR_PLAIN, config=CFG)

        _check("watch reconciliation emitted wakes for changed tasks", wake_count == 3 and len(sent) == 3, sent)
        _check("pending interrupted zombie terminalized", interrupted.get("status") == "interrupted", interrupted)
        _check("interrupted worker current_task cleared", _current_task_id(WORKER) == "", _current_task_id(WORKER))
        _check("error zombie terminalized as failed", error_task.get("status") == "failed", error_task)
        _check("done outcome is not auto-completed by reconciliation", done_task.get("status") == "pending", done_task)
        _check("mismatched terminal outcome is not proof", mismatch.get("status") == "pending", mismatch)
        _check(
            "merged PR audit auto-closes only with verified gates",
            pr_audit.get("status") == "completed"
            and pr_audit.get("completion_evidence_verified") is True
            and pr_audit.get("completion_evidence", {}).get("commit_sha") == GREEN_SHA,
            pr_audit,
        )
        _check("merged PR with red gate stays open", pr_red.get("status") == "pending", pr_red)
        _check("plain PR mention stays open", pr_plain.get("status") == "pending", pr_plain)
    finally:
        for key, value in previous_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        shutil.rmtree(tmp, ignore_errors=True)
        _cleanup()

    if FAILURES:
        print(f"\nFAIL -- {len(FAILURES)}: {FAILURES}")
        return 1
    print("\nPASS -- stale task reconciliation terminalizes dead outcomes and verified merged PR audit tasks.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
