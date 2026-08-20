#!/usr/bin/env python3
"""Acceptance: GitHub mutations require a separate-principal socket broker.

CONTROL rework for task-7107c13f:

- token lives only in the broker process env, never in the worker tree
- worker client shim contains the socket path only
- inner gh is broker-private, not on worker PATH
- simulated /usr/bin/gh without token cannot write
- unknown/write argv fail closed after unbind
- live prefixes refused (no deploy)

No live Redis/Neo/GitHub.
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

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fleet_orchestrator.github_broker import (  # noqa: E402
    GitHubBrokerInstallError,
    install_github_broker,
    prefix_is_live,
)
from fleet_orchestrator.notify_state import state_key  # noqa: E402


FAILURES: list[str] = []
TOKEN = "broker-secret-token-not-for-workers"


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
        "WRITE_HINTS = ('POST', 'PATCH', 'PUT', 'DELETE', 'merge', 'close', 'create', 'comment', 'delete')\n"
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


def _tree_contains(root: Path, needle: str) -> bool:
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if needle in text:
            return True
    return False


def main() -> int:
    session = "taey-ed-grok"
    supervisor = "conductor-codex"
    task_id = "task-7107c13f-broker-fixture"
    _check("live /usr prefix refused by classifier", prefix_is_live(Path("/usr/bin")))
    try:
        install_github_broker(
            Path("/usr/local"),
            inner_gh=Path("/bin/true"),
            client_script=ROOT / "scripts" / "gh-outward",
            token="",
        )
        _check("live prefix install refused", False, "expected GitHubBrokerInstallError")
    except GitHubBrokerInstallError as exc:
        _check("live prefix install refused", "refusing live prefix" in str(exc), exc)

    with tempfile.TemporaryDirectory() as tmp:
        prefix = Path(tmp) / "prefix"
        sink = Path(tmp) / "sink.json"
        inner_src = Path(tmp) / "inner-gh"
        _write_fake_inner(inner_src, sink)
        installed = install_github_broker(
            prefix,
            inner_gh=inner_src,
            client_script=ROOT / "scripts" / "gh-outward",
            token="",
            python_executable=sys.executable,
        )
        worker_root = Path(installed["worker_root"])
        broker_dir = Path(installed["broker_dir"])
        socket_path = Path(installed["socket"])
        _check("worker tree has no token", not _tree_contains(worker_root, TOKEN), worker_root)
        _check(
            "worker client does not name inner gh",
            "gh-real" not in Path(installed["worker_gh"]).read_text(),
            Path(installed["worker_gh"]).read_text(),
        )
        _check("inner gh is outside worker tree", not str(installed["inner_gh"]).startswith(str(worker_root)))

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

        broker_env = os.environ.copy()
        broker_env["GH_TOKEN"] = TOKEN
        broker_env["ORCH_GITHUB_BROKER_SOCKET"] = str(socket_path)
        broker_env["ORCH_GITHUB_BROKER_INNER"] = installed["inner_gh"]
        broker_env["PYTHONPATH"] = str(Path(tmp)) + os.pathsep + str(ROOT)
        broker_env["BROKER_BOUND"] = "1"
        broker_proc = subprocess.Popen(
            [sys.executable, str(ROOT / "scripts" / "github-brokerd")],
            env=broker_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            for _attempt in range(50):
                if socket_path.exists():
                    break
                time.sleep(0.05)
            _check("broker socket exists", socket_path.exists(), socket_path)

            def run_worker(argv, *, bound: bool, path_kind: str) -> subprocess.CompletedProcess:
                env = os.environ.copy()
                env.pop("GH_TOKEN", None)
                env.pop("GITHUB_TOKEN", None)
                env["ORCH_OUTWARD_SESSION"] = session
                env["ORCH_GITHUB_BROKER_SOCKET"] = str(socket_path)
                env["PYTHONPATH"] = str(ROOT)
                if path_kind == "broker":
                    env["PATH"] = str(worker_root / "bin") + os.pathsep + env.get("PATH", "")
                    binary = str(worker_root / "bin" / "gh")
                else:
                    env["PATH"] = str(worker_root / "usr" / "bin") + os.pathsep + env.get("PATH", "")
                    binary = str(worker_root / "usr" / "bin" / "gh")
                # Rebind broker's FakeRedis by restarting is heavy; BROKER_BOUND is
                # read at broker import. Toggle by env on a new connection is not
                # enough. Send session and rely on broker process env BROKER_BOUND.
                return subprocess.run(
                    [binary, *argv],
                    capture_output=True,
                    text=True,
                    env=env,
                    check=False,
                )

            bound_write = run_worker(
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

            system_write = run_worker(
                ["api", "-X", "POST", "repos/palios-taey/x/statuses/abc"],
                bound=True,
                path_kind="system",
            )
            _check(
                "system gh without token cannot write",
                system_write.returncode != 0 and "NO_TOKEN" in system_write.stderr,
                (system_write.returncode, system_write.stderr),
            )
            _check("system gh did not mutate sink", _sink_events(sink) == before, _sink_events(sink))
        finally:
            broker_proc.terminate()
            try:
                broker_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                broker_proc.kill()

        # Restart broker unbound (no current_task) to prove revocation.
        broker_env["BROKER_BOUND"] = "0"
        broker_proc = subprocess.Popen(
            [sys.executable, str(ROOT / "scripts" / "github-brokerd")],
            env=broker_env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        try:
            if socket_path.exists():
                socket_path.unlink()
            for _attempt in range(50):
                if socket_path.exists():
                    break
                time.sleep(0.05)
            env = os.environ.copy()
            env.pop("GH_TOKEN", None)
            env["ORCH_OUTWARD_SESSION"] = session
            env["ORCH_GITHUB_BROKER_SOCKET"] = str(socket_path)
            env["PYTHONPATH"] = str(ROOT)
            env["PATH"] = str(worker_root / "bin") + os.pathsep + env.get("PATH", "")
            before = list(_sink_events(sink))
            unbound_merge = subprocess.run(
                [str(worker_root / "bin" / "gh"), "pr", "merge", "32"],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            _check(
                "unbound broker pr merge denied",
                unbound_merge.returncode == 1 and "SAFETY DENY" in unbound_merge.stderr,
                (unbound_merge.returncode, unbound_merge.stderr),
            )
            _check("unbound broker pr merge did not mutate sink", _sink_events(sink) == before, _sink_events(sink))
            unknown = subprocess.run(
                [str(worker_root / "bin" / "gh"), "mystery", "mutate"],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            _check(
                "unbound unknown argv fail-closed",
                unknown.returncode == 1 and "SAFETY DENY" in unknown.stderr,
                (unknown.returncode, unknown.stderr),
            )
            get_status = subprocess.run(
                [str(worker_root / "bin" / "gh"), "api", "repos/palios-taey/x/commits/abc/statuses?per_page=100"],
                capture_output=True,
                text=True,
                env=env,
                check=False,
            )
            _check(
                "classified GET still works after unbind",
                get_status.returncode == 0,
                (get_status.returncode, get_status.stdout, get_status.stderr),
            )
        finally:
            broker_proc.terminate()
            try:
                broker_proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                broker_proc.kill()

    if FAILURES:
        print(f"FAIL: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("PASS outward_github_broker_acceptance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
