"""Acceptance: taey-task dispatch loads fleet .env before the real claim path.

This covers the post-#269 failure mode: product lookup became optional, but
``taey-task dispatch`` still reached ``_claim_ready_orch_task`` with no
``ORCH_REDIS_HOST`` exported and crashed when that helper constructed
``OrchConfig``. The regression subprocess starts without ORCH_REDIS_HOST and
must still claim a real OrchTask through the dispatch primitive by loading the
fleet .env from the CLI bootstrap path.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fleet_orchestrator.config import OrchConfig  # noqa: E402
from fleet_orchestrator.notify_state import redis_connect as notify_redis_connect  # noqa: E402
from fleet_orchestrator.orch_schema import (  # noqa: E402
    _state_key,
    create_phase,
    create_project,
    create_task,
    get_neo4j_driver,
    get_task,
    init_schema,
)
from fleet_orchestrator.test_isolation import assert_agent_test_store_isolated  # noqa: E402

FAILURES: list[str] = []
REQUIRED_DOTENV_KEYS = (
    "ORCH_NEO4J_URI",
    "ORCH_NEO4J_DB",
    "ORCH_REDIS_HOST",
    "ORCH_REDIS_PORT",
    "REDIS_HOST",
    "REDIS_PORT",
    "NOTIFY_KEY_PREFIX",
)


def _require_test_namespace() -> str:
    raw = (os.environ.get("ORCH_TEST_NAMESPACE") or "").strip()
    if not raw:
        raise SystemExit("ORCH_TEST_NAMESPACE is required; run via scripts/orch-acceptance-isolated.")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{5,80}", raw):
        raise SystemExit("ORCH_TEST_NAMESPACE must be 6-80 chars of letters/digits/._:-")
    if "acceptance" not in raw.lower():
        raise SystemExit("ORCH_TEST_NAMESPACE must include acceptance")
    return raw


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _write_home_fleet_dotenv(home: Path, *, dashboard_url: str) -> Path:
    missing = [key for key in REQUIRED_DOTENV_KEYS if not os.environ.get(key)]
    if missing:
        raise SystemExit(f"isolated runner did not provide required env keys: {missing}")
    env_dir = home / "claude-code-fleet-orchestrator"
    env_dir.mkdir(parents=True, exist_ok=True)
    env_path = env_dir / ".env"
    values = {
        key: os.environ[key]
        for key in REQUIRED_DOTENV_KEYS
    }
    values.update(
        {
            "ORCH_DASHBOARD_URL": dashboard_url,
            "ORCH_NOTIFY_CLI": os.environ.get("ORCH_NOTIFY_CLI", "taey-notify"),
            "ORCH_AGENT_TEST_INFRA": os.environ.get("ORCH_AGENT_TEST_INFRA", "throwaway"),
        }
    )
    env_path.write_text(
        "\n".join(f"{key}='{value}'" for key, value in sorted(values.items())) + "\n",
        encoding="utf-8",
    )
    return env_path


def _cleanup(cfg: OrchConfig, redis_client, prefix: str, peer: str) -> None:
    redis_client.delete(*[_state_key(peer, suffix) for suffix in ("current_task", "idle", "last_tool_activity", "last_outcome", "parent")])
    notify_prefix = os.environ.get("NOTIFY_KEY_PREFIX", "taey")
    for pattern in (
        f"{notify_prefix}:worker-task-liveness:{prefix}*",
        f"{notify_prefix}:worker-task-liveness-escalated:{prefix}*",
    ):
        cursor = 0
        while True:
            cursor, keys = redis_client.scan(cursor=cursor, match=pattern, count=100)
            if keys:
                redis_client.delete(*keys)
            if cursor == 0:
                break
    with get_neo4j_driver(cfg).session(database=cfg.neo4j_db) as session:
        session.run("MATCH (n) WHERE n.id STARTS WITH $prefix DETACH DELETE n", prefix=prefix)


def _run_dispatch_subprocess(*, home: Path, cwd: Path, task_id: str, peer: str, supervisor: str, description: str) -> subprocess.CompletedProcess[str]:
    code = r"""
import contextlib
import io
import os
import sys
from types import SimpleNamespace
from unittest import mock

if os.environ.get("ORCH_REDIS_HOST"):
    raise SystemExit("ORCH_REDIS_HOST was unexpectedly exported before CLI bootstrap")

from fleet_orchestrator import script_entrypoints

sys.argv = ["taey-task", "--help"]
help_out = io.StringIO()
with contextlib.redirect_stdout(help_out):
    try:
        help_code = script_entrypoints.taey_task_main()
    except SystemExit as exc:
        help_code = exc.code
if help_code not in (None, 0):
    raise SystemExit(f"taey_task_main --help failed during CLI bootstrap: {help_code}")

import fleet_orchestrator.cli_taey_task as cli
import fleet_orchestrator.dispatch as dispatch_module

if cli.DASHBOARD_URL != os.environ["ORCH_DASHBOARD_URL"]:
    raise SystemExit(f"CLI module imported before dotenv bootstrap: {cli.DASHBOARD_URL!r}")

task_id = os.environ["TEST_TASK_ID"]
peer = os.environ["TEST_PEER"]
supervisor = os.environ["TEST_SUPERVISOR"]
description = os.environ["TEST_DESCRIPTION"]

def fake_api(method, endpoint, data=None):
    if method == "GET" and endpoint == f"/api/tasks/{task_id}":
        return {"id": task_id, "description": description}
    raise AssertionError((method, endpoint, data))

ok = SimpleNamespace(returncode=0, stdout="OK", stderr="")
args = SimpleNamespace(task_id=task_id, peer=peer, priority="normal", force=False)
with mock.patch.object(cli, "api_call", side_effect=fake_api), \
     mock.patch.object(cli, "detect_from_node", return_value=supervisor), \
     mock.patch.object(cli, "conflicting_binding", return_value=None), \
     mock.patch.object(dispatch_module, "hook_installation_status", return_value=SimpleNamespace(ok=True, detail="hooked")), \
     mock.patch.object(dispatch_module.subprocess, "run", return_value=ok), \
     mock.patch.object(dispatch_module, "maybe_emit_decision_receipt"):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        cli.cmd_dispatch(args)
print(out.getvalue().strip())
print("DISPATCH_OK")
"""
    env = os.environ.copy()
    for key in (
        "ORCH_DOTENV",
        "ORCH_REDIS_HOST",
        "ORCH_REDIS_PORT",
        "ORCH_NEO4J_URI",
        "ORCH_NEO4J_DB",
        "REDIS_HOST",
        "REDIS_PORT",
        "NOTIFY_KEY_PREFIX",
        "ORCH_DASHBOARD_URL",
        "ORCH_NOTIFY_CLI",
    ):
        env.pop(key, None)
    import_paths = [str(ROOT)]
    import_paths.extend(path for path in sys.path if path and Path(path).exists())
    existing_pythonpath = env.get("PYTHONPATH")
    if existing_pythonpath:
        import_paths.extend(path for path in existing_pythonpath.split(os.pathsep) if path)
    env.update(
        {
            "HOME": str(home),
            "PYTHONPATH": os.pathsep.join(dict.fromkeys(import_paths)),
            "TEST_TASK_ID": task_id,
            "TEST_PEER": peer,
            "TEST_SUPERVISOR": supervisor,
            "TEST_DESCRIPTION": description,
        }
    )
    return subprocess.run(
        [sys.executable, "-c", code],
        cwd=str(cwd),
        env=env,
        text=True,
        capture_output=True,
        check=False,
        timeout=30,
    )


def main() -> int:
    assert_agent_test_store_isolated()
    namespace = _require_test_namespace()
    cfg = OrchConfig()
    redis_client = notify_redis_connect()
    prefix = f"{namespace}-dispatch-dotenv-{uuid.uuid4().hex[:8]}"
    supervisor = f"{prefix}-sup"
    peer = f"{supervisor}-codex"
    task_id = f"{prefix}::peer-work"
    description = "dispatch dotenv full claim acceptance"

    _cleanup(cfg, redis_client, prefix, peer)
    tmp = Path(tempfile.mkdtemp(prefix="dispatch-cli-dotenv-"))
    home = tmp / "home"
    cwd = tmp / "cwd"
    cwd.mkdir(parents=True, exist_ok=True)
    dashboard_url = "http://127.0.0.1:59999"
    env_path = _write_home_fleet_dotenv(home, dashboard_url=dashboard_url)

    try:
        init_schema(config=cfg)
        create_project(project_id=prefix, name="dispatch dotenv acceptance", supervisor=supervisor, config=cfg)
        create_phase(project_id=prefix, phase_id=f"{prefix}::phase", name="dispatch", config=cfg)
        create_task(
            phase_id=f"{prefix}::phase",
            task_id=task_id,
            description=description,
            owner=peer,
            wake_owner_if_ready=False,
            config=cfg,
        )

        result = _run_dispatch_subprocess(
            home=home,
            cwd=cwd,
            task_id=task_id,
            peer=peer,
            supervisor=supervisor,
            description=description,
        )
        combined = f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
        _check("dispatch subprocess starts without exported ORCH_REDIS_HOST", "unexpectedly exported" not in combined, combined)
        _check("dispatch subprocess exits zero", result.returncode == 0, combined)
        _check("dispatch subprocess reports OK", "DISPATCH_OK" in result.stdout, combined)
        _check("home fleet dotenv was the only config source", str(env_path) and env_path.is_file(), env_path)

        task = get_task(task_id, config=cfg) or {}
        raw_current = redis_client.get(_state_key(peer, "current_task"))
        current = json.loads(raw_current) if raw_current else {}
        _check("dispatch claimed real OrchTask", task.get("status") == "in_progress", task)
        _check("dispatch records dispatched_to peer", task.get("dispatched_to") == peer, task)
        _check("dispatch binds current_task", current.get("task_id") == task_id, current)
        _check("dispatch current_task records supervisor", current.get("supervisor") == supervisor, current)
    finally:
        _cleanup(cfg, redis_client, prefix, peer)

    if FAILURES:
        print(f"\nFAIL - {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS - taey-task dispatch loads fleet dotenv before full claim path.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
