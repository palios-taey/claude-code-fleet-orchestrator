#!/usr/bin/env python3
"""Acceptance: GitHub mutations go through a credential broker, not worker gh.

Proves CONTROL rework for task-7107c13f in an isolated prefix:

- classified reads pass without capability
- unknown/write argv is fail-closed
- worker env has no GH_TOKEN after install
- simulated /usr/bin/gh without the broker token cannot write the sink
- bound broker may write; after unbind the same still-running actor is denied
- live prefixes are refused (no deploy)

No live Redis/Neo/GitHub.
"""
from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
from pathlib import Path



ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fleet_orchestrator.github_broker import (  # noqa: E402
    GitHubBrokerInstallError,
    install_github_broker,
    prefix_is_live,
)
from fleet_orchestrator.notify_state import state_key  # noqa: E402


FAILURES: list[str] = []
TOKEN = "broker-secret-token"


def _check(label: str, condition: bool, detail: object = "") -> None:
    print(("PASS " if condition else "FAIL ") + label + ("" if condition else f" -> {detail}"))
    if not condition:
        FAILURES.append(label)


def _write_fake_inner(path: Path, sink: Path) -> None:
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        "from pathlib import Path\n"
        f"SINK = Path({str(sink)!r})\n"
        "WRITE_HINTS = ('-X', 'POST', 'PATCH', 'PUT', 'DELETE', 'merge', 'close', 'create', 'comment', 'delete')\n"
        "args = sys.argv[1:]\n"
        "is_write = any(token in WRITE_HINTS or token.upper() in WRITE_HINTS for token in args)\n"
        "if is_write and not os.environ.get('GH_TOKEN'):\n"
        "    print('NO_TOKEN', file=sys.stderr)\n"
        "    raise SystemExit(1)\n"
        "if is_write:\n"
        "    events = json.loads(SINK.read_text()) if SINK.exists() else []\n"
        "    events.append({'args': args, 'token': os.environ.get('GH_TOKEN', '')})\n"
        "    SINK.write_text(json.dumps(events))\n"
        "print('{\"ok\":true}')\n",
        encoding="utf-8",
    )
    path.chmod(path.stat().st_mode | stat.S_IEXEC)


def _sink_events(sink: Path) -> list:
    if not sink.exists() or not sink.read_text().strip():
        return []
    return json.loads(sink.read_text())


def main() -> int:
    session = "taey-ed-grok"
    supervisor = "conductor-codex"
    task_id = "task-7107c13f-broker-fixture"
    _check("live /usr prefix refused by classifier", prefix_is_live(Path("/usr/bin")))
    try:
        install_github_broker(
            Path("/usr/local"),
            inner_gh=Path("/bin/true"),
            broker_script=ROOT / "scripts" / "gh-outward",
            token=TOKEN,
        )
        _check("live prefix install refused", False, "expected GitHubBrokerInstallError")
    except GitHubBrokerInstallError as exc:
        _check("live prefix install refused", "refusing live prefix" in str(exc), exc)
    except Exception as exc:  # noqa: BLE001
        _check("live prefix install refused", "refusing live prefix" in str(exc), exc)

    with tempfile.TemporaryDirectory() as tmp:
        prefix = Path(tmp) / "prefix"
        sink = Path(tmp) / "sink.json"
        inner = Path(tmp) / "inner-gh"
        _write_fake_inner(inner, sink)
        installed = install_github_broker(
            prefix,
            inner_gh=inner,
            broker_script=ROOT / "scripts" / "gh-outward",
            token=TOKEN,
            python_executable=sys.executable,
        )
        worker_env_file = Path(installed["worker_env"])
        worker_unset = worker_env_file.read_text()
        _check("worker env unsets GH_TOKEN", "unset GH_TOKEN" in worker_unset, worker_unset)
        creds = Path(installed["credentials"])
        _check("broker creds mode 0600", oct(creds.stat().st_mode & 0o777) == "0o600", oct(creds.stat().st_mode))

        sitecustomize = Path(tmp) / "sitecustomize.py"
        sitecustomize.write_text(
            "import json, os, sys\n"
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
            f"KEY = {state_key(session, 'current_task')!r}\n"
            "if os.environ.get('BROKER_BOUND', '1') == '1':\n"
            f"    redis.set(KEY, json.dumps({{'task_id': {task_id!r}, 'description': 'fixture', 'supervisor': {supervisor!r}, 'started_at': 1.0}}))\n"
            "oc.redis_connect = lambda: redis\n"
            f"oc._default_task_loader = lambda tid, *, config=None: ({{'id': {task_id!r}, 'status': 'in_progress', 'dispatched_to': {session!r}, 'owner': {supervisor!r}}} if tid == {task_id!r} else None)\n",
            encoding="utf-8",
        )

        def run_gh(argv, *, bound: bool, path_kind: str) -> subprocess.CompletedProcess:
            env = os.environ.copy()
            env.pop("GH_TOKEN", None)
            env.pop("GITHUB_TOKEN", None)
            env["BROKER_BOUND"] = "1" if bound else "0"
            env["ORCH_OUTWARD_SESSION"] = session
            env["PYTHONPATH"] = str(Path(tmp)) + os.pathsep + str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
            if path_kind == "broker":
                env["PATH"] = str(prefix / "bin") + os.pathsep + env.get("PATH", "")
                binary = str(prefix / "bin" / "gh")
            else:
                env["PATH"] = str(prefix / "usr" / "bin") + os.pathsep + env.get("PATH", "")
                binary = str(prefix / "usr" / "bin" / "gh")
            return subprocess.run(
                [binary, *argv],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )

        bound_write = run_gh(
            ["api", "-X", "POST", "repos/palios-taey/x/statuses/abc", "-f", "state=success"],
            bound=True,
            path_kind="broker",
        )
        _check(
            "bound broker write reaches inner gh with token",
            bound_write.returncode == 0 and any(e.get("token") == TOKEN for e in _sink_events(sink)),
            (bound_write.returncode, bound_write.stderr, _sink_events(sink)),
        )
        before = list(_sink_events(sink))

        unbound_merge = run_gh(["pr", "merge", "32"], bound=False, path_kind="broker")
        _check(
            "unbound broker pr merge denied",
            unbound_merge.returncode == 1 and "SAFETY DENY" in unbound_merge.stderr,
            (unbound_merge.returncode, unbound_merge.stderr),
        )
        _check("unbound broker pr merge did not mutate sink", _sink_events(sink) == before, _sink_events(sink))

        unknown = run_gh(["mystery", "mutate"], bound=False, path_kind="broker")
        _check(
            "unbound unknown argv fail-closed",
            unknown.returncode == 1 and "SAFETY DENY" in unknown.stderr,
            (unknown.returncode, unknown.stderr),
        )
        _check("unknown argv did not mutate sink", _sink_events(sink) == before, _sink_events(sink))

        system_write = run_gh(
            ["api", "-X", "POST", "repos/palios-taey/x/statuses/abc"],
            bound=False,
            path_kind="system",
        )
        _check(
            "system gh without token cannot write",
            system_write.returncode != 0 and "NO_TOKEN" in system_write.stderr,
            (system_write.returncode, system_write.stderr),
        )
        _check("system gh did not mutate sink", _sink_events(sink) == before, _sink_events(sink))

        unbound_read = run_gh(
            ["api", "repos/palios-taey/x/commits/abc/statuses?per_page=100"],
            bound=False,
            path_kind="broker",
        )
        _check(
            "classified GET still works after unbind",
            unbound_read.returncode == 0,
            (unbound_read.returncode, unbound_read.stdout, unbound_read.stderr),
        )

    if FAILURES:
        print(f"FAIL: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("PASS outward_github_broker_acceptance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
