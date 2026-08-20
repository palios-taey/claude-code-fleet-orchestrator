#!/usr/bin/env python3
"""Acceptance: GitHub mutations require a separate-principal socket broker.

CONTROL rework for task-7107c13f:

- token lives only in the broker process env, never in the worker tree
- worker exec socket rejects mint/revoke (control channel only)
- SO_PEERCRED uid must match the broker-owned control principal map
- bind_current_task mints; session_unbind revokes; handle never in Redis
- worker mint-as-victim and revoke-victim on the exec socket are denied
- live prefixes refused (no deploy)

No live Redis/Neo/GitHub.
"""
from __future__ import annotations

import inspect
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

from fleet_orchestrator.dispatch import _rollback_claim, bind_current_task  # noqa: E402
from fleet_orchestrator import github_broker as github_broker_mod  # noqa: E402
from fleet_orchestrator.github_broker import (  # noqa: E402
    GitHubBrokerInstallError,
    call_broker,
    install_github_broker,
    mint_and_deliver_outward_handle,
    peer_is_control_principal,
    peer_may_control,
    prefix_is_live,
    process_is_orch_api_controller,
    revoke_and_clear_outward_handle,
)
from fleet_orchestrator.notify_state import state_key  # noqa: E402


class FileRedis:
    def __init__(self, path: Path) -> None:
        self.path = path
        if not path.exists():
            path.write_text("{}", encoding="utf-8")

    def _load(self) -> dict:
        return json.loads(self.path.read_text(encoding="utf-8") or "{}")

    def _save(self, store: dict) -> None:
        self.path.write_text(json.dumps(store), encoding="utf-8")

    def get(self, key: str):
        return self._load().get(key)

    def set(self, key: str, value: str) -> None:
        store = self._load()
        store[key] = value
        self._save(store)

    def delete(self, *keys: str) -> int:
        store = self._load()
        deleted = 0
        for key in keys:
            if key in store:
                del store[key]
                deleted += 1
        self._save(store)
        return deleted

    def pipeline(self, transaction: bool = True):
        del transaction
        return _Pipe(self)


class _Pipe:
    def __init__(self, redis: FileRedis) -> None:
        self.redis = redis
        self.ops: list[tuple] = []

    def delete(self, *keys: str):
        self.ops.append(("delete", keys))
        return self

    def set(self, key: str, value: str):
        self.ops.append(("set", key, value))
        return self

    def execute(self):
        results = []
        for op in self.ops:
            if op[0] == "delete":
                results.append(self.redis.delete(*op[1]))
            else:
                results.append(self.redis.set(op[1], op[2]))
        self.ops.clear()
        return results


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


def _start_broker(env: dict, socket_path: Path) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, str(ROOT / "scripts" / "github-brokerd")],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    for _attempt in range(50):
        if socket_path.exists():
            break
        time.sleep(0.05)
    return proc


def _stop_broker(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        proc.kill()


def main() -> int:
    session = "taey-ed-grok-fixture"
    supervisor = "conductor-codex"
    task_id = "task-7107c13f-broker-fixture"
    victim = "victim-grok"
    victim_task = "task-victim-live"
    _check("live /usr prefix refused by classifier", prefix_is_live(Path("/usr/bin")))
    _check(
        "uid 1 is not a default control principal",
        not peer_may_control(1, {"control": {os.getuid()}, "worker": set()}),
    )
    orig_peer = github_broker_mod.session_from_peer_pid
    prior_controls = os.environ.get("ORCH_GITHUB_BROKER_CONTROL_SESSIONS")
    os.environ["ORCH_GITHUB_BROKER_CONTROL_SESSIONS"] = "conductor-codex"
    github_broker_mod.session_from_peer_pid = lambda pid: "taey-ed-grok"
    _check(
        "worker TTY cannot mint on control",
        not peer_is_control_principal(os.getpid()),
    )
    github_broker_mod.session_from_peer_pid = lambda pid: "conductor-codex"
    _check(
        "supervisor TTY can mint on control",
        peer_is_control_principal(os.getpid()),
    )
    github_broker_mod.session_from_peer_pid = lambda pid: ""
    _check(
        "empty TTY without API cmdline cannot mint",
        not peer_is_control_principal(os.getpid()),
    )
    github_broker_mod.session_from_peer_pid = orig_peer
    if prior_controls is None:
        os.environ.pop("ORCH_GITHUB_BROKER_CONTROL_SESSIONS", None)
    else:
        os.environ["ORCH_GITHUB_BROKER_CONTROL_SESSIONS"] = prior_controls
    api_unit = (ROOT / "deploy" / "systemd" / "fleet-orchestrator-api-gatea.service").read_text(
        encoding="utf-8"
    )
    broker_unit = (ROOT / "deploy" / "systemd" / "github-broker.service").read_text(encoding="utf-8")
    _check(
        "API unit is the github-control supplementary group",
        "SupplementaryGroups=github-control" in api_unit,
        api_unit,
    )
    _check(
        "broker unit forbids usermod mira into github-control",
        "Do NOT usermod -aG github-control mira" in broker_unit,
        broker_unit,
    )
    _check(
        "this test process is not the API controller cmdline",
        not process_is_orch_api_controller(os.getpid()),
    )
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

    bind_src = inspect.getsource(bind_current_task)
    unbind_src = (ROOT / "fleet_orchestrator" / "tasks_api.py").read_text(encoding="utf-8")
    _check("bind_current_task calls control mint", "mint_and_deliver_outward_handle" in bind_src)
    _check("bind_current_task revokes prior handles", "revoke_and_clear_outward_handle" in bind_src)
    _check(
        "session_unbind_current_task calls control revoke",
        "revoke_and_clear_outward_handle" in unbind_src,
    )
    _check(
        "dispatch rollback revokes minted handles",
        "revoke_and_clear_outward_handle" in inspect.getsource(_rollback_claim),
    )
    _check(
        "mint does not write tmux session env",
        "set-environment" not in inspect.getsource(mint_and_deliver_outward_handle),
    )

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
        socket_path = Path(installed["socket"])
        control_socket = Path(installed["control_socket"])
        _check("worker tree has no token", not _tree_contains(worker_root, TOKEN), worker_root)
        _check(
            "worker client does not name inner gh",
            "gh-real" not in Path(installed["worker_gh"]).read_text(),
            Path(installed["worker_gh"]).read_text(),
        )
        _check(
            "worker client does not name control socket",
            "control" not in Path(installed["worker_gh"]).read_text(),
            Path(installed["worker_gh"]).read_text(),
        )
        _check("inner gh is outside worker tree", not str(installed["inner_gh"]).startswith(str(worker_root)))
        _check(
            "control socket is outside worker tree",
            not str(control_socket).startswith(str(worker_root)),
            control_socket,
        )

        redis_file = Path(tmp) / "broker-redis.json"
        redis_file.write_text("{}", encoding="utf-8")
        redis = FileRedis(redis_file)
        redis.set(
            state_key(session, "current_task"),
            json.dumps(
                {
                    "task_id": task_id,
                    "description": "fixture",
                    "supervisor": supervisor,
                    "started_at": 1.0,
                }
            ),
        )
        sitecustomize = Path(tmp) / "sitecustomize.py"
        sitecustomize.write_text(
            "import json, os, sys\n"
            "from pathlib import Path\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "import fleet_orchestrator.outward_capability as oc\n"
            "import fleet_orchestrator.current_task_binding as ctb\n"
            "class FileRedis:\n"
            "    def __init__(self, path):\n"
            "        self.path = Path(path)\n"
            "    def _load(self):\n"
            "        return json.loads(self.path.read_text() or '{}')\n"
            "    def _save(self, store):\n"
            "        self.path.write_text(json.dumps(store))\n"
            "    def get(self, key):\n"
            "        return self._load().get(key)\n"
            "    def set(self, key, value):\n"
            "        store = self._load(); store[key] = value; self._save(store)\n"
            "    def delete(self, *keys):\n"
            "        store = self._load()\n"
            "        for key in keys:\n"
            "            store.pop(key, None)\n"
            "        self._save(store)\n"
            "    def pipeline(self, transaction=True):\n"
            "        return _Pipe(self)\n"
            "class _Pipe:\n"
            "    def __init__(self, redis):\n"
            "        self.redis = redis\n"
            "        self.ops = []\n"
            "    def delete(self, *keys):\n"
            "        self.ops.append(('delete', keys))\n"
            "        return self\n"
            "    def set(self, key, value):\n"
            "        self.ops.append(('set', key, value))\n"
            "        return self\n"
            "    def execute(self):\n"
            "        results = []\n"
            "        for op in self.ops:\n"
            "            if op[0] == 'delete':\n"
            "                results.append(self.redis.delete(*op[1]))\n"
            "            else:\n"
            "                results.append(self.redis.set(op[1], op[2]))\n"
            "        self.ops.clear()\n"
            "        return results\n"
            f"redis = FileRedis({str(redis_file)!r})\n"
            "oc.redis_connect = lambda: redis\n"
            "ctb.redis_connect = lambda: redis\n"
            f"oc._default_task_loader = lambda tid, *, config=None: ({{'id': {task_id!r}, 'status': 'in_progress', 'dispatched_to': {session!r}, 'owner': {supervisor!r}}} if tid == {task_id!r} else None)\n"
            "import fleet_orchestrator.dispatch as dispatch_mod\n"
            "dispatch_mod._redis_connect = lambda: redis\n"
            "dispatch_mod._mark_in_progress_best_effort = lambda *a, **k: None\n"
            "dispatch_mod.register_worker_task_liveness = lambda **k: True\n"
            "dispatch_mod.worker_task_liveness_enabled = lambda: False\n"
            "import fleet_orchestrator.github_broker as gb\n"
            f"PEER_SESSION_FILE = Path({str(Path(tmp) / 'peer-session.txt')!r})\n"
            "_real_session = gb.session_from_peer_pid\n"
            "def _peer_session(pid):\n"
            "    try:\n"
            "        cmdline = Path(f'/proc/{pid}/cmdline').read_bytes()\n"
            "    except OSError:\n"
            "        cmdline = b''\n"
            "    if b'gh-outward' in cmdline:\n"
            "        return PEER_SESSION_FILE.read_text().strip()\n"
            "    return _real_session(pid)\n"
            "gb.session_from_peer_pid = _peer_session\n",
            encoding="utf-8",
        )
        peer_session_file = Path(tmp) / "peer-session.txt"
        peer_session_file.write_text(session, encoding="utf-8")

        base_env = os.environ.copy()
        base_env["GH_TOKEN"] = TOKEN
        base_env["ORCH_GITHUB_BROKER_SOCKET"] = str(socket_path)
        base_env["ORCH_GITHUB_BROKER_CONTROL_SOCKET"] = str(control_socket)
        base_env["ORCH_GITHUB_BROKER_INNER"] = installed["inner_gh"]
        base_env["PYTHONPATH"] = str(Path(tmp)) + os.pathsep + str(ROOT)
        base_env["ORCH_GITHUB_BROKER_WORKER_UIDS"] = str(os.getuid())
        base_env["ORCH_GITHUB_BROKER_CONTROL_SESSIONS"] = supervisor

        broker_env = dict(base_env)
        broker_proc = _start_broker(broker_env, socket_path)
        prior_control = os.environ.get("ORCH_GITHUB_BROKER_CONTROL_SOCKET")
        os.environ["ORCH_GITHUB_BROKER_CONTROL_SOCKET"] = str(control_socket)
        try:
            for _attempt in range(50):
                if control_socket.exists():
                    break
                time.sleep(0.05)
            _check("broker socket exists", socket_path.exists(), socket_path)
            _check("control socket exists", control_socket.exists(), control_socket)
            _check(
                "control socket mode 0660 not 0600",
                stat.S_IMODE(control_socket.stat().st_mode) == 0o660,
                oct(stat.S_IMODE(control_socket.stat().st_mode)),
            )
            _check(
                "exec socket mode 0660",
                stat.S_IMODE(socket_path.stat().st_mode) == 0o660,
                oct(stat.S_IMODE(socket_path.stat().st_mode)),
            )

            victim_mint = call_broker(
                str(socket_path),
                op="mint",
                session=victim,
                task_id=victim_task,
            )
            _check(
                "worker mint-as-victim on exec denied",
                int(victim_mint.get("rc") or 0) != 0
                and "authenticated control channel" in str(victim_mint.get("stderr") or ""),
                victim_mint,
            )
            victim_revoke = call_broker(str(socket_path), op="revoke", session=victim)
            _check(
                "worker revoke-victim on exec denied",
                int(victim_revoke.get("rc") or 0) != 0
                and "authenticated control channel" in str(victim_revoke.get("stderr") or ""),
                victim_revoke,
            )
            probe = Path(tmp) / "worker-mint-probe.py"
            probe.write_text(
                "import json, sys\n"
                f"sys.path.insert(0, {str(ROOT)!r})\n"
                "from fleet_orchestrator.github_broker import call_broker\n"
                f"print(json.dumps(call_broker({str(control_socket)!r}, op='mint', session={victim!r}, task_id={victim_task!r})))\n",
                encoding="utf-8",
            )
            worker_mint = subprocess.run(
                [sys.executable, str(probe)],
                capture_output=True,
                text=True,
                env={**os.environ, "PYTHONPATH": str(ROOT)},
                check=False,
            )
            try:
                worker_mint_payload = json.loads(worker_mint.stdout or "{}")
            except json.JSONDecodeError:
                worker_mint_payload = {"stdout": worker_mint.stdout, "stderr": worker_mint.stderr}
            _check(
                "worker control mint-as-victim denied",
                int(worker_mint_payload.get("rc") or 0) != 0
                and "control principal not mapped" in str(worker_mint_payload.get("stderr") or ""),
                (worker_mint.returncode, worker_mint_payload),
            )
            direct_mint = call_broker(
                str(control_socket),
                op="mint",
                session=victim,
                task_id=victim_task,
            )
            _check(
                "non-API caller cannot mint (no uvicorn tasks_api cmdline)",
                int(direct_mint.get("rc") or 0) != 0
                and "control principal not mapped" in str(direct_mint.get("stderr") or ""),
                direct_mint,
            )

            api_script = Path(tmp) / "no_tty_uvicorn_tasks_api.py"
            api_script.write_text(
                "import json, os, sys\n"
                f"sys.path.insert(0, {str(ROOT)!r})\n"
                "from fleet_orchestrator.dispatch import bind_current_task\n"
                "from fleet_orchestrator.github_broker import mint_and_deliver_outward_handle, revoke_and_clear_outward_handle\n"
                "op = sys.argv[1]\n"
                "out = {}\n"
                "tty = os.isatty(0)\n"
                "if op == 'mint':\n"
                f"    handle = mint_and_deliver_outward_handle({session!r}, {task_id!r}, 1.0, outward_handle_out=out)\n"
                "    print(json.dumps({'handle': handle, 'tty': tty}))\n"
                "elif op == 'bind':\n"
                f"    bind_current_task({session!r}, {task_id!r}, 'fixture', supervisor={supervisor!r}, outward_handle_out=out)\n"
                "    print(json.dumps({'handle': out.get('handle', ''), 'tty': tty}))\n"
                "elif op == 'boom':\n"
                "    import fleet_orchestrator.dispatch as dispatch_mod\n"
                "    class Boom:\n"
                "        def pipeline(self, transaction=True):\n"
                "            raise RuntimeError('isolated redis bind failure')\n"
                "    dispatch_mod._redis_connect = lambda: Boom()\n"
                "    try:\n"
                f"        bind_current_task('boom-worker', {task_id!r}, 'fixture', supervisor={supervisor!r}, outward_handle_out=out)\n"
                "        print(json.dumps({'error': 'expected RuntimeError', 'handle': out.get('handle', ''), 'tty': tty}))\n"
                "    except RuntimeError as exc:\n"
                "        print(json.dumps({'error': str(exc), 'handle': out.get('handle', ''), 'tty': tty}))\n"
                "elif op == 'revoke':\n"
                f"    removed = revoke_and_clear_outward_handle({session!r}, {task_id!r})\n"
                "    print(json.dumps({'removed': removed, 'tty': tty}))\n"
                "else:\n"
                "    raise SystemExit('unknown op')\n",
                encoding="utf-8",
            )

            def run_api_controller(op: str) -> dict:
                result = subprocess.run(
                    [
                        sys.executable,
                        str(api_script),
                        op,
                        "uvicorn",
                        "fleet_orchestrator.tasks_api:app",
                    ],
                    capture_output=True,
                    text=True,
                    stdin=subprocess.DEVNULL,
                    start_new_session=True,
                    env={
                        **os.environ,
                        "PYTHONPATH": str(Path(tmp)) + os.pathsep + str(ROOT),
                        "ORCH_GITHUB_BROKER_CONTROL_SOCKET": str(control_socket),
                        "ORCH_GITHUB_BROKER_CONTROL_SESSIONS": supervisor,
                    },
                    check=False,
                )
                try:
                    payload = json.loads((result.stdout or "").strip().splitlines()[-1])
                except (json.JSONDecodeError, IndexError):
                    payload = {
                        "error": "unparsed",
                        "stdout": result.stdout,
                        "stderr": result.stderr,
                        "rc": result.returncode,
                    }
                payload["_rc"] = result.returncode
                payload["_stderr"] = result.stderr
                return payload

            no_tty_mint = run_api_controller("mint")
            _check(
                "no-TTY API cmdline can mint",
                bool(no_tty_mint.get("handle")) and no_tty_mint.get("tty") is False,
                no_tty_mint,
            )

            handle_out = run_api_controller("bind")
            handle = str(handle_out.get("handle") or "")
            _check(
                "no-TTY API bind_current_task minted handle via control",
                bool(handle) and handle_out.get("tty") is False,
                handle_out,
            )
            dumped = json.loads(redis.get(state_key(session, "current_task")) or "{}")
            _check("current_task does not contain handle", "outward_handle" not in dumped, dumped)
            stolen = str(dumped.get("outward_handle") or "")
            _check("redis dump has no victim handle to replay", stolen == "", dumped)

            def run_worker(argv, *, path_kind: str = "broker"):
                env = os.environ.copy()
                env.pop("GH_TOKEN", None)
                env.pop("GITHUB_TOKEN", None)
                env.pop("ORCH_OUTWARD_HANDLE", None)
                env.pop("ORCH_GITHUB_BROKER_CONTROL_SOCKET", None)
                env["ORCH_GITHUB_BROKER_SOCKET"] = str(socket_path)
                env["PYTHONPATH"] = str(ROOT)
                if path_kind == "broker":
                    env["PATH"] = str(worker_root / "bin") + os.pathsep + env.get("PATH", "")
                    binary = str(worker_root / "bin" / "gh")
                else:
                    env["PATH"] = str(worker_root / "usr" / "bin") + os.pathsep + env.get("PATH", "")
                    binary = str(worker_root / "usr" / "bin" / "gh")
                return subprocess.run(
                    [binary, *argv],
                    capture_output=True,
                    text=True,
                    env=env,
                    check=False,
                )

            bound_write = run_worker(
                ["api", "-X", "POST", "repos/palios-taey/x/statuses/abc", "-f", "state=success"],
            )
            _check(
                "existing-process post-bind write succeeds without env handle",
                bound_write.returncode == 0 and any(e.get("token") == TOKEN for e in _sink_events(sink)),
                (bound_write.returncode, bound_write.stderr, _sink_events(sink)),
            )
            before = list(_sink_events(sink))

            peer_session_file.write_text(victim, encoding="utf-8")
            theft = run_worker(["pr", "merge", "32"])
            _check(
                "cross-seat peer cannot use another seat's live handle",
                theft.returncode == 1 and "possession handle" in theft.stderr,
                (theft.returncode, theft.stderr),
            )
            _check("cross-seat theft did not mutate sink", _sink_events(sink) == before, _sink_events(sink))
            peer_session_file.write_text(session, encoding="utf-8")

            implicit_bound = run_worker(
                ["api", "repos/palios-taey/x/issues", "-f", "title=x"],
            )
            _check(
                "bound implicit gh api -f POST reaches inner gh",
                implicit_bound.returncode == 0,
                (implicit_bound.returncode, implicit_bound.stderr),
            )
            before = list(_sink_events(sink))

            boom_out = run_api_controller("boom")
            _check(
                "bind redis failure raised",
                "isolated redis bind failure" in str(boom_out.get("error") or ""),
                boom_out,
            )
            leaked = str(boom_out.get("handle") or "")
            _check("failed bind produced a handle that must be revoked", bool(leaked), boom_out)
            peer_session_file.write_text("boom-worker", encoding="utf-8")
            failed_bind_write = run_worker(["pr", "merge", "32"])
            _check(
                "handle from failed bind is revoked",
                failed_bind_write.returncode == 1
                and "revoked outward possession handle" in failed_bind_write.stderr,
                (failed_bind_write.returncode, failed_bind_write.stderr),
            )
            peer_session_file.write_text(session, encoding="utf-8")

            system_write = run_worker(
                ["api", "-X", "POST", "repos/palios-taey/x/statuses/abc"],
                path_kind="system",
            )
            _check(
                "system gh without token cannot write",
                system_write.returncode != 0 and "NO_TOKEN" in system_write.stderr,
                (system_write.returncode, system_write.stderr),
            )
            _check("system gh did not mutate sink", _sink_events(sink) == before, _sink_events(sink))

            revoked = run_api_controller("revoke")
            removed = int(revoked.get("removed") or 0)
            _check("unbind helper revoked handle", removed >= 1, revoked)
            redis.delete(state_key(session, "current_task"))
            stolen_replay = run_worker(["pr", "merge", "32"])
            _check(
                "stale worker replaying redis current_task handle denied",
                stolen_replay.returncode == 1 and "SAFETY DENY" in stolen_replay.stderr,
                (stolen_replay.returncode, stolen_replay.stderr),
            )
            stale = run_worker(["pr", "merge", "32"])
            _check(
                "revoked handle denied after unbind",
                stale.returncode == 1 and "revoked outward possession handle" in stale.stderr,
                (stale.returncode, stale.stderr),
            )
            _check("revoked handle did not mutate sink", _sink_events(sink) == before, _sink_events(sink))
            unknown = run_worker(["mystery", "mutate"])
            _check(
                "unbound unknown argv fail-closed",
                unknown.returncode == 1 and "SAFETY DENY" in unknown.stderr,
                (unknown.returncode, unknown.stderr),
            )
            get_status = run_worker(
                ["api", "repos/palios-taey/x/commits/abc/statuses?per_page=100"],
            )
            _check(
                "classified GET still works after unbind",
                get_status.returncode == 0,
                (get_status.returncode, get_status.stdout, get_status.stderr),
            )

            lifecycle_out: dict = {}
            redis.set(
                state_key(session, "current_task"),
                json.dumps(
                    {
                        "task_id": task_id,
                        "description": "fixture",
                        "supervisor": supervisor,
                        "started_at": 1.0,
                    }
                ),
            )
            lifecycle_out = run_api_controller("mint")
            _check(
                "production mint helper returns a handle from no-TTY API",
                bool(lifecycle_out.get("handle")) and lifecycle_out.get("tty") is False,
                lifecycle_out,
            )
        finally:
            if prior_control is None:
                os.environ.pop("ORCH_GITHUB_BROKER_CONTROL_SOCKET", None)
            else:
                os.environ["ORCH_GITHUB_BROKER_CONTROL_SOCKET"] = prior_control
            _stop_broker(broker_proc)

    if FAILURES:
        print(f"FAIL: {len(FAILURES)} check(s): {FAILURES}")
        return 1
    print("PASS outward_github_broker_acceptance")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
