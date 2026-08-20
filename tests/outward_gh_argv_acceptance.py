#!/usr/bin/env python3
"""Acceptance: direct gh status/comment argv is gated at mutation time.

Proves task-7107c13f: wrapping gh (not only helper CLIs) so ``gh api -X POST``
status/comment and ``gh pr comment`` cannot bypass unbind. Isolated FakeRedis +
fake inner gh; no live Redis/Neo/GitHub.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fleet_orchestrator.notify_state import state_key  # noqa: E402
from fleet_orchestrator.outward_capability import (  # noqa: E402
    OutwardAuthorizationError,
    github_argv_requires_outward_capability,
    require_github_argv_capability,
)


FAILURES: list[str] = []
WRAPPER = ROOT / "scripts" / "gh-outward"


def _check(label: str, condition: bool, detail: object = "") -> None:
    print(("PASS " if condition else "FAIL ") + label + ("" if condition else f" -> {detail}"))
    if not condition:
        FAILURES.append(label)


class FakeRedis:
    def __init__(self) -> None:
        self.store: Dict[str, str] = {}

    def get(self, key: str) -> Optional[str]:
        return self.store.get(key)

    def set(self, key: str, value: str) -> None:
        self.store[key] = value

    def delete(self, *keys: str) -> int:
        deleted = 0
        for key in keys:
            if key in self.store:
                del self.store[key]
                deleted += 1
        return deleted


def main() -> int:
    session = "taey-ed-grok"
    supervisor = "conductor-codex"
    task_id = "task-7107c13f-gh-fixture"
    redis = FakeRedis()
    task = {
        "id": task_id,
        "status": "in_progress",
        "dispatched_to": session,
        "owner": supervisor,
    }

    def loader(tid: str, *, config: Any = None) -> Optional[Dict[str, Any]]:
        if tid != task_id:
            return None
        return dict(task)

    _check(
        "GET statuses does not require capability",
        not github_argv_requires_outward_capability(
            ["api", f"repos/palios-taey/x/commits/abc/statuses?per_page=100"]
        ),
    )
    _check(
        "POST statuses requires capability",
        github_argv_requires_outward_capability(
            ["api", "-X", "POST", "repos/palios-taey/x/statuses/abc"]
        ),
    )
    _check(
        "POST comments requires capability",
        github_argv_requires_outward_capability(
            ["api", "--method", "POST", "repos/palios-taey/x/issues/32/comments"]
        ),
    )
    _check(
        "pr comment requires capability",
        github_argv_requires_outward_capability(["pr", "comment", "32", "--body", "x"]),
    )
    _check(
        "pr view does not require capability",
        not github_argv_requires_outward_capability(["pr", "view", "32"]),
    )
    _check(
        "pr merge requires capability (fail-closed write)",
        github_argv_requires_outward_capability(["pr", "merge", "32"]),
    )
    _check(
        "pr close requires capability",
        github_argv_requires_outward_capability(["pr", "close", "32"]),
    )
    _check(
        "issue close requires capability",
        github_argv_requires_outward_capability(["issue", "close", "9"]),
    )
    _check(
        "release create requires capability",
        github_argv_requires_outward_capability(["release", "create", "v1"]),
    )
    _check(
        "repo delete requires capability",
        github_argv_requires_outward_capability(["repo", "delete", "org/x"]),
    )
    _check(
        "unknown argv requires capability",
        github_argv_requires_outward_capability(["mystery", "mutate"]),
    )
    _check(
        "api PATCH requires capability",
        github_argv_requires_outward_capability(["api", "-X", "PATCH", "repos/org/x"]),
    )

    redis.set(
        state_key(session, "current_task"),
        json.dumps(
            {
                "task_id": task_id,
                "description": "fixture",
                "supervisor": supervisor,
                "started_at": time.time(),
            }
        ),
    )
    with mock.patch("fleet_orchestrator.outward_capability.redis_connect", return_value=redis), \
         mock.patch("fleet_orchestrator.outward_capability._default_task_loader", side_effect=loader), \
         mock.patch("fleet_orchestrator.outward_capability.resolve_session_id", return_value=session):
        allowed = require_github_argv_capability(
            ["api", "-X", "POST", "repos/palios-taey/x/statuses/abc"],
            session_id=session,
            redis_client=redis,
            task_loader=loader,
        )
        _check("bound POST status argv authorized", allowed is not None and allowed.allowed, allowed)

        redis.delete(state_key(session, "current_task"))
        try:
            require_github_argv_capability(
                ["pr", "comment", "32", "--body", "stale"],
                session_id=session,
                redis_client=redis,
                task_loader=loader,
            )
            _check("unbound pr comment raises", False, "expected OutwardAuthorizationError")
        except OutwardAuthorizationError as exc:
            _check(
                "unbound pr comment raises",
                "no live current_task binding" in str(exc),
                exc,
            )

        passthrough = require_github_argv_capability(
            ["api", "repos/palios-taey/x/commits/abc/statuses?per_page=100"],
            session_id=session,
            redis_client=redis,
            task_loader=loader,
        )
        _check("unbound GET statuses is not a mutation", passthrough is None, passthrough)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        sink = tmp_path / "inner-gh-log.json"
        inner = tmp_path / "fake-gh"
        inner.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, sys\n"
            "from pathlib import Path\n"
            "Path(os.environ['GH_INNER_LOG']).write_text(json.dumps(sys.argv[1:]))\n"
            "print('{\"ok\":true}')\n",
            encoding="utf-8",
        )
        inner.chmod(inner.stat().st_mode | stat.S_IEXEC)
        env = os.environ.copy()
        env["GH_OUTWARD_INNER"] = str(inner)
        env["GH_INNER_LOG"] = str(sink)
        env["ORCH_OUTWARD_SESSION"] = session
        pythonpath = str(ROOT)
        if env.get("PYTHONPATH"):
            pythonpath = pythonpath + os.pathsep + env["PYTHONPATH"]
        env["PYTHONPATH"] = pythonpath

        sitecustomize = tmp_path / "sitecustomize.py"
        sitecustomize.write_text(
            "import json, sys, types\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "import fleet_orchestrator.outward_capability as oc\n"
            "class FakeRedis:\n"
            "    def __init__(self):\n"
            "        self.store = {}\n"
            "    def get(self, key):\n"
            "        return self.store.get(key)\n"
            "    def set(self, key, value):\n"
            "        self.store[key] = value\n"
            "redis = FakeRedis()\n"
            f"redis.set({state_key(session, 'current_task')!r}, json.dumps({{'task_id': {task_id!r}, 'description': 'fixture', 'supervisor': {supervisor!r}, 'started_at': 1.0}}))\n"
            "oc.redis_connect = lambda: redis\n"
            f"oc._default_task_loader = lambda tid, *, config=None: ({{'id': {task_id!r}, 'status': 'in_progress', 'dispatched_to': {session!r}, 'owner': {supervisor!r}}} if tid == {task_id!r} else None)\n",
            encoding="utf-8",
        )
        env["PYTHONPATH"] = str(tmp_path) + os.pathsep + pythonpath

        bound = subprocess.run(
            [
                sys.executable,
                str(WRAPPER),
                "api",
                "-X",
                "POST",
                "repos/palios-taey/x/statuses/abc",
                "-f",
                "state=success",
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        _check(
            "bound wrapper execs inner gh",
            bound.returncode == 0 and sink.exists(),
            (bound.returncode, bound.stdout, bound.stderr, sink.exists()),
        )

        sitecustomize.write_text(
            "import json, sys, types\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "import fleet_orchestrator.outward_capability as oc\n"
            "class FakeRedis:\n"
            "    def __init__(self):\n"
            "        self.store = {}\n"
            "    def get(self, key):\n"
            "        return self.store.get(key)\n"
            "redis = FakeRedis()\n"
            "oc.redis_connect = lambda: redis\n"
            f"oc._default_task_loader = lambda tid, *, config=None: ({{'id': {task_id!r}, 'status': 'in_progress', 'dispatched_to': {session!r}, 'owner': {supervisor!r}}} if tid == {task_id!r} else None)\n",
            encoding="utf-8",
        )
        if sink.exists():
            sink.unlink()
        unbound = subprocess.run(
            [
                sys.executable,
                str(WRAPPER),
                "pr",
                "comment",
                "32",
                "--body",
                "stale after unbind",
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        _check(
            "unbound wrapper denies pr comment",
            unbound.returncode == 1 and "SAFETY DENY" in unbound.stderr,
            (unbound.returncode, unbound.stdout, unbound.stderr),
        )
        _check("unbound wrapper did not exec inner gh", not sink.exists(), sink.exists())

        get_status = subprocess.run(
            [
                sys.executable,
                str(WRAPPER),
                "api",
                "repos/palios-taey/x/commits/abc/statuses?per_page=100",
            ],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        _check(
            "unbound GET statuses still reaches inner gh",
            get_status.returncode == 0 and sink.exists(),
            (get_status.returncode, get_status.stdout, get_status.stderr),
        )

    if FAILURES:
        print(f"FAIL: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("PASS outward_gh_argv_acceptance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
