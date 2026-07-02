"""Acceptance: project triggers respawn stopped sessions before dispatching wakes.

The respawn call is intentionally unmocked. The test installs throwaway
executables for tmux, peer-respawn.sh, and taey-notify, then verifies the
project-trigger path invokes those real processes.
"""
from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import uuid
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _require_test_namespace() -> str:
    raw = (os.environ.get("ORCH_TEST_NAMESPACE") or "").strip()
    if not raw:
        raise SystemExit("ORCH_TEST_NAMESPACE is required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{5,80}", raw):
        raise SystemExit("ORCH_TEST_NAMESPACE must be 6-80 chars of letters/digits/._:-")
    if not any(marker in raw.lower() for marker in ("test", "ci", "acceptance")):
        raise SystemExit("ORCH_TEST_NAMESPACE must include test, ci, or acceptance")
    return raw


from fleet_orchestrator.test_isolation import assert_acceptance_redis_isolated  # noqa: E402

assert_acceptance_redis_isolated()

_PFX = f"{_require_test_namespace()}-cron-liveness-{uuid.uuid4().hex[:8]}"
os.environ["NOTIFY_KEY_PREFIX"] = _PFX
os.environ["ORCH_SESSION_IDS"] = f"{_PFX}-conductor"

from fleet_orchestrator.cli_orch_cron import (  # noqa: E402
    _fire_project_trigger,
    _starvation_state_key,
    orch_key,
)
from fleet_orchestrator.config import OrchConfig, get_redis_sync  # noqa: E402
from fleet_orchestrator.notify_state import state_key as notify_state_key  # noqa: E402
from fleet_orchestrator.orch_schema import create_phase, create_project, create_task, get_neo4j_driver, init_schema  # noqa: E402

CFG = OrchConfig()
_FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: str = "") -> None:
    print(("  PASS " if cond else "  FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        _FAILURES.append(label)


def _write_executable(path: Path, body: str) -> None:
    path.write_text(body, encoding="utf-8")
    path.chmod(0o755)


def _write_codex_hooks(path: Path) -> None:
    required = {
        "SessionStart": "codex_session_start.py",
        "PreToolUse": "codex_pre_tool.py",
        "PostToolUse": "codex_post_tool.py",
        "Stop": "codex_stop.py",
        "UserPromptSubmit": "codex_user_prompt.py",
    }
    payload = {
        "hooks": {
            event: [{"hooks": [{"command": f"python /tmp/{script}"}]}]
            for event, script in required.items()
        }
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


def _install_process_stubs(tmp: Path) -> dict[str, Path]:
    sessions_file = tmp / "tmux-sessions.txt"
    respawn_log = tmp / "peer-respawn.log"
    notify_log = tmp / "taey-notify.jsonl"

    tmux = tmp / "tmux"
    _write_executable(
        tmux,
        """#!/usr/bin/env bash
if [ "${1:-}" = "list-sessions" ]; then
  if [ -f "${TMUX_SESSIONS_FILE}" ]; then
    cat "${TMUX_SESSIONS_FILE}"
  fi
  exit 0
fi
exit 1
""",
    )

    respawn = tmp / "peer-respawn.sh"
    _write_executable(
        respawn,
        """#!/usr/bin/env bash
set -u
session="${1:-}"
printf '%s\n' "${session}" >> "${RESPAWN_LOG}"
rc="${RESPAWN_RC:-0}"
if [ "${rc}" != "0" ]; then
  exit "${rc}"
fi
touch "${TMUX_SESSIONS_FILE}"
if [ -n "${session}" ] && ! grep -Fxq "${session}" "${TMUX_SESSIONS_FILE}" 2>/dev/null; then
  printf '%s\n' "${session}" >> "${TMUX_SESSIONS_FILE}"
fi
exit 0
""",
    )

    notify = tmp / "taey-notify"
    _write_executable(
        notify,
        """#!/usr/bin/env python3
import json
import os
import sys

with open(os.environ["NOTIFY_LOG"], "a", encoding="utf-8") as fh:
    fh.write(json.dumps(sys.argv[1:]) + "\\n")
raise SystemExit(int(os.environ.get("NOTIFY_RC", "0")))
""",
    )

    hooks = tmp / "codex-hooks.json"
    _write_codex_hooks(hooks)

    return {
        "sessions_file": sessions_file,
        "respawn_log": respawn_log,
        "notify_log": notify_log,
        "tmux": tmux,
        "respawn": respawn,
        "notify": notify,
        "hooks": hooks,
    }


def _read_lines(path: Path) -> list[str]:
    if not path.exists():
        return []
    return [line.rstrip("\n") for line in path.read_text(encoding="utf-8").splitlines()]


def _read_notify(path: Path) -> list[list[str]]:
    rows: list[list[str]] = []
    for line in _read_lines(path):
        rows.append(json.loads(line))
    return rows


def _cleanup(r=None) -> None:
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as s:
        s.run("MATCH (n) WHERE n.id STARTS WITH $p DETACH DELETE n", p=_PFX)
    if r is not None:
        for raw_key in list(r.scan_iter(f"*{_PFX}*")):
            r.delete(raw_key)


def _seed_project(case: str, owner: str) -> tuple[str, str, dict]:
    project_id = f"{_PFX}-{case}"
    phase_id = f"{project_id}::phase"
    task_id = f"{project_id}::t1"
    create_project(project_id=project_id, name=project_id, supervisor="conductor", config=CFG)
    create_phase(phase_id=phase_id, project_id=project_id, name="P1", order=1, config=CFG)
    create_task(
        task_id=task_id,
        phase_id=phase_id,
        description=f"Task {case}",
        owner=owner,
        wake_owner_if_ready=False,
        config=CFG,
    )
    trig = {
        "id": f"{project_id}-trig",
        "project": project_id,
        "session": owner,
        "supervisor": "conductor",
        "mode": "advance",
    }
    return project_id, task_id, trig


def _run_fire(r, trig: dict, *, minute: int) -> str:
    return _fire_project_trigger(r, trig.copy(), datetime(2026, 1, 1, 12, minute))


def _fire_id(trig: dict, *, minute: int) -> str:
    return f"{trig['id']}-20260101-12{minute:02d}"


def main() -> int:
    init_schema(config=CFG)
    r = get_redis_sync(CFG)
    _cleanup(r)

    env_names = [
        "PATH",
        "TMUX_SESSIONS_FILE",
        "RESPAWN_LOG",
        "RESPAWN_RC",
        "NOTIFY_LOG",
        "NOTIFY_RC",
        "ORCH_NOTIFY_CLI",
        "ORCH_PEER_RESPAWN_SCRIPT",
        "CODEX_HOOKS_PATH",
    ]
    old_env = {name: os.environ.get(name) for name in env_names}

    try:
        with tempfile.TemporaryDirectory(prefix=f"{_PFX}-") as tmp_raw:
            tmp = Path(tmp_raw)
            paths = _install_process_stubs(tmp)
            os.environ["PATH"] = f"{tmp}{os.pathsep}{old_env.get('PATH') or ''}"
            os.environ["TMUX_SESSIONS_FILE"] = str(paths["sessions_file"])
            os.environ["RESPAWN_LOG"] = str(paths["respawn_log"])
            os.environ["NOTIFY_LOG"] = str(paths["notify_log"])
            os.environ["ORCH_NOTIFY_CLI"] = str(paths["notify"])
            os.environ["ORCH_PEER_RESPAWN_SCRIPT"] = str(paths["respawn"])
            os.environ["CODEX_HOOKS_PATH"] = str(paths["hooks"])

            stopped = f"{_PFX}-stopped-codex"
            project_id, _task_id, trig = _seed_project("stopped-success", stopped)
            result = _run_fire(r, trig, minute=1)
            respawn_lines = _read_lines(paths["respawn_log"])
            sessions = _read_lines(paths["sessions_file"])
            notify_rows = _read_notify(paths["notify_log"])
            _check("stopped session respawn succeeds", result == "dispatched", f"got {result}")
            _check("respawn executable actually ran", respawn_lines == [stopped], repr(respawn_lines))
            _check("respawn made session visible before dispatch", stopped in sessions, repr(sessions))
            _check(
                "wake delivered after respawn",
                any(row and row[0] == stopped and "--handoff" in row for row in notify_rows),
                repr(notify_rows),
            )
            _check(
                "starvation state clear on successful dispatch",
                not r.exists(_starvation_state_key(str(trig["id"]), project_id)),
            )

            paths["respawn_log"].write_text("", encoding="utf-8")
            paths["notify_log"].write_text("", encoding="utf-8")
            healthy = f"{_PFX}-healthy-codex"
            paths["sessions_file"].write_text(f"{healthy}\n", encoding="utf-8")
            r.set(notify_state_key(healthy, "idle"), "1")
            project_id, _task_id, trig = _seed_project("healthy", healthy)
            result = _run_fire(r, trig, minute=2)
            _check("healthy in-tmux idle session dispatches", result == "dispatched", f"got {result}")
            _check("healthy session skips respawn", _read_lines(paths["respawn_log"]) == [])
            _check(
                "healthy session receives wake",
                any(row and row[0] == healthy and "--handoff" in row for row in _read_notify(paths["notify_log"])),
                repr(_read_notify(paths["notify_log"])),
            )

            paths["respawn_log"].write_text("", encoding="utf-8")
            paths["notify_log"].write_text("", encoding="utf-8")
            paths["sessions_file"].write_text("", encoding="utf-8")
            os.environ["RESPAWN_RC"] = "7"
            failing = f"{_PFX}-respawn-fails-codex"
            project_id, _task_id, trig = _seed_project("respawn-fails", failing)
            r.set(_starvation_state_key(str(trig["id"]), project_id), "stale")
            result = _run_fire(r, trig, minute=3)
            _check("respawn nonzero is structured failure", result == "failed:session_dead_respawn_failed", result)
            _check("nonzero respawn executable actually ran", _read_lines(paths["respawn_log"]) == [failing])
            _check("nonzero failure clears starvation state", not r.exists(_starvation_state_key(str(trig["id"]), project_id)))
            _check("nonzero failure clears dedup", not r.exists(orch_key("orch-cron-fired", _fire_id(trig, minute=3))))
            rows = _read_notify(paths["notify_log"])
            _check(
                "nonzero failure alerts conductor",
                any(row and row[0] == "conductor" and "PROJECT_TRIGGER_RESPAWN_FAILED" in " ".join(row) for row in rows),
                repr(rows),
            )
            _check("nonzero failure does not dispatch wake", not any(row and row[0] == failing for row in rows), repr(rows))
            os.environ.pop("RESPAWN_RC", None)

            paths["respawn_log"].write_text("", encoding="utf-8")
            paths["notify_log"].write_text("", encoding="utf-8")
            paths["sessions_file"].write_text("", encoding="utf-8")
            os.environ["ORCH_PEER_RESPAWN_SCRIPT"] = str(tmp / "missing-peer-respawn.sh")
            missing = f"{_PFX}-respawn-missing-codex"
            project_id, _task_id, trig = _seed_project("respawn-missing", missing)
            r.set(_starvation_state_key(str(trig["id"]), project_id), "stale")
            result = _run_fire(r, trig, minute=4)
            _check("missing respawn is structured failure", result == "failed:session_dead_respawn_failed", result)
            _check("missing respawn clears starvation state", not r.exists(_starvation_state_key(str(trig["id"]), project_id)))
            _check("missing respawn clears dedup", not r.exists(orch_key("orch-cron-fired", _fire_id(trig, minute=4))))
            rows = _read_notify(paths["notify_log"])
            _check(
                "missing respawn alerts conductor",
                any(row and row[0] == "conductor" and "respawn script not found" in " ".join(row) for row in rows),
                repr(rows),
            )
            _check("missing respawn does not dispatch wake", not any(row and row[0] == missing for row in rows), repr(rows))

            paths["respawn_log"].write_text("", encoding="utf-8")
            paths["notify_log"].write_text("", encoding="utf-8")
            paths["sessions_file"].write_text("", encoding="utf-8")
            os.environ.pop("ORCH_PEER_RESPAWN_SCRIPT", None)
            unset = f"{_PFX}-respawn-unset-codex"
            project_id, _task_id, trig = _seed_project("respawn-unset", unset)
            r.set(_starvation_state_key(str(trig["id"]), project_id), "stale")
            result = _run_fire(r, trig, minute=5)
            _check("unset respawn config is structured failure", result == "failed:session_dead_respawn_failed", result)
            _check("unset respawn config does not run executable", _read_lines(paths["respawn_log"]) == [])
            _check("unset respawn config clears starvation state", not r.exists(_starvation_state_key(str(trig["id"]), project_id)))
            _check("unset respawn config clears dedup", not r.exists(orch_key("orch-cron-fired", _fire_id(trig, minute=5))))
            rows = _read_notify(paths["notify_log"])
            _check(
                "unset respawn config alerts conductor",
                any(row and row[0] == "conductor" and "ORCH_PEER_RESPAWN_SCRIPT is required" in " ".join(row) for row in rows),
                repr(rows),
            )
            _check("unset respawn config does not dispatch wake", not any(row and row[0] == unset for row in rows), repr(rows))
    finally:
        for name, value in old_env.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        _cleanup(r)

    if _FAILURES:
        print(f"FAIL: {len(_FAILURES)} assertion(s) failed: {_FAILURES}")
        return 1

    print("PASS: _fire_project_trigger uses real respawn process gating before wake dispatch.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
