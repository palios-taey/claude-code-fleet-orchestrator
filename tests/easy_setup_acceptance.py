#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import importlib
import importlib.util
import importlib.machinery
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

for candidate in (ROOT / ".env", Path.home() / "claude-code-fleet-orchestrator/.env"):
    if "ORCH_DOTENV" not in os.environ and candidate.is_file():
        os.environ["ORCH_DOTENV"] = str(candidate)
        break
os.environ.setdefault("ORCH_REDIS_HOST", "127.0.0.1")
os.environ.setdefault("ORCH_REDIS_PORT", "6379")
os.environ.setdefault("ORCH_NEO4J_URI", "bolt://127.0.0.1:7687")
os.environ.setdefault("ORCH_NEO4J_DB", "neo4j")
os.environ.setdefault("ORCH_DASHBOARD_URL", "http://127.0.0.1:5002")

from fastapi.testclient import TestClient  # noqa: E402

easy_setup = importlib.import_module("fleet_orchestrator.easy_setup")  # noqa: E402
from fleet_orchestrator.easy_setup import (  # noqa: E402
    MANAGED_DENIES,
    apply_claude_permission_guard,
    atomic_write_json,
    atomic_write_text,
    compose_scope,
    package_version,
    reconcile_pending_hook_transaction,
    remove_claude_permission_guard,
    restore_claude_settings_backup,
    snapshot_claude_settings,
)
from fleet_orchestrator.tasks_api import app  # noqa: E402

FAILURES: list[str] = []
# Derived from the single source of truth (fleet_orchestrator/version.py) — never
# hardcode the release literal here. A hardcoded value silently locks in a stale
# version: this once asserted "1.6.0" while the package had moved to 1.8.x, so the
# version-identity check "passed" against the wrong number instead of catching the
# drift it exists to catch.
from fleet_orchestrator.version import __version__ as EXPECTED_RELEASE  # noqa: E402


def _assert(label: str, condition: bool, detail: object) -> None:
    if condition:
        print(f"PASS {label}")
    else:
        FAILURES.append(label)
        print(f"FAIL {label} {detail}")


def _temp_settings(tmp: Path, deny: list[str] | None = None) -> Path:
    tmp.mkdir(parents=True, exist_ok=True)
    settings_path = tmp / "settings.json"
    settings_path.write_text(json.dumps({"permissions": {"deny": deny or []}}, indent=2) + "\n", encoding="utf-8")
    return settings_path


def main() -> int:
    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)

        settings_path = _temp_settings(tmp, ["ExistingDeny"])
        first = apply_claude_permission_guard(settings_path, apply=True)
        second = apply_claude_permission_guard(settings_path, apply=True)
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        deny = settings["permissions"]["deny"]
        marker = settings["_managedBy"]["claude-code-fleet-orchestrator"]["permissions.deny_added"]
        _assert(
            "deny-exactly-once",
            all(deny.count(entry) == 1 for entry in MANAGED_DENIES) and first["changed"] and not second["changed"] and set(marker) == set(MANAGED_DENIES),
            settings,
        )

        preowned_path = _temp_settings(tmp / "preowned", ["AskUserQuestion", "ExistingDeny"])
        preowned_path.parent.mkdir(parents=True, exist_ok=True)
        preowned_path.write_text(json.dumps({"permissions": {"deny": ["AskUserQuestion", "ExistingDeny"]}}, indent=2) + "\n", encoding="utf-8")
        apply_claude_permission_guard(preowned_path, apply=True)
        remove_claude_permission_guard(preowned_path, apply=True)
        preowned = json.loads(preowned_path.read_text(encoding="utf-8"))
        _assert(
            "ownership-roundtrip-preexisting-deny",
            "AskUserQuestion" in preowned["permissions"]["deny"] and "AskUserQuestion(*)" not in preowned["permissions"]["deny"],
            preowned,
        )

        before_text = settings_path.read_text(encoding="utf-8")
        with mock.patch("fleet_orchestrator.easy_setup.os.replace", side_effect=RuntimeError("replace failed")):
            try:
                atomic_write_text(settings_path, "mutated\n")
            except RuntimeError:
                pass
        after_text = settings_path.read_text(encoding="utf-8")
        _assert("crash-mid-write-recovery", before_text == after_text, {"before": before_text, "after": after_text})

        state_path = tmp / "state.json"
        atomic_write_json(state_path, {"a": 1})
        original_state = state_path.read_text(encoding="utf-8")
        with mock.patch("fleet_orchestrator.easy_setup.os.replace", side_effect=RuntimeError("replace failed")):
            try:
                atomic_write_json(state_path, {"a": 2})
            except RuntimeError:
                pass
        _assert("state-atomic-write-recovery", state_path.read_text(encoding="utf-8") == original_state, state_path.read_text(encoding="utf-8"))

        double_path = _temp_settings(tmp / "double", ["UserOwned"])
        double_path.parent.mkdir(parents=True, exist_ok=True)
        double_path.write_text(json.dumps({"permissions": {"deny": ["UserOwned"]}}, indent=2) + "\n", encoding="utf-8")
        baseline = double_path.read_text(encoding="utf-8")
        with mock.patch.object(easy_setup, "STATE_DIR", tmp / "state-dir"), \
             mock.patch.object(easy_setup, "SETUP_STATE_PATH", tmp / "state-dir" / "easy_setup_state.json"):
            backup1 = snapshot_claude_settings(double_path, original_text=baseline)
            backup2 = snapshot_claude_settings(double_path, original_text=double_path.read_text(encoding="utf-8"))
            hook_added = {"Stop": ["python3 /tmp/notify/hooks/stop_idle.py"]}
            apply_claude_permission_guard(double_path, apply=True, hook_commands_added=hook_added)
            apply_claude_permission_guard(double_path, apply=True, hook_commands_added={})
            removed = remove_claude_permission_guard(double_path, apply=True)
            final_text = double_path.read_text(encoding="utf-8")
        _assert(
            "double-install-uninstall-baseline-clean",
            backup1 == backup2 and final_text == baseline and removed["changed"],
            {"backup1": str(backup1), "backup2": str(backup2), "final_text": final_text},
        )

        hooks_path = _temp_settings(tmp / "hooks", [])
        hooks_doc = {
            "hooks": {
                "Stop": [
                    {"hooks": [{"type": "command", "command": "python3 /user/alt/stop_idle.py", "timeout": 5000}]},
                    {"hooks": [{"type": "command", "command": "python3 /notify/hooks/stop_idle.py", "timeout": 5000}]},
                ]
            },
            "permissions": {"deny": []},
        }
        hooks_path.write_text(json.dumps(hooks_doc, indent=2) + "\n", encoding="utf-8")
        with mock.patch("fleet_orchestrator.easy_setup.resolve_notify_root", return_value=Path("/notify")):
            hook_guard = apply_claude_permission_guard(hooks_path, apply=True, hook_commands_added={"Stop": ["python3 /notify/hooks/stop_idle.py"]})
        hook_doc = json.loads(hooks_path.read_text(encoding="utf-8"))
        stop_commands = []
        for group in hook_doc["hooks"]["Stop"]:
            for hook in group["hooks"]:
                stop_commands.append(hook["command"])
        _assert(
            "basename-collision-user-hook-preserved",
            "python3 /user/alt/stop_idle.py" in stop_commands and "python3 /notify/hooks/stop_idle.py" in stop_commands and len(stop_commands) == 2 and hook_guard["recorded_hook_commands"]["Stop"] == ["python3 /notify/hooks/stop_idle.py"],
            stop_commands,
        )

        expected_notify_root = tmp / "expected-notify"
        (expected_notify_root / "hooks").mkdir(parents=True)
        (expected_notify_root / "hooks" / "session_start.py").write_text("# ok\n", encoding="utf-8")
        expected_hooks = easy_setup._expected_hook_scripts(expected_notify_root)
        missing_session_start_hooks = easy_setup._expected_hook_scripts(tmp / "old-notify")
        _assert(
            "expected-hooks-include-session-start",
            expected_hooks.get("SessionStart") == (expected_notify_root / "hooks" / "session_start.py").resolve()
            and "SessionStart" not in missing_session_start_hooks,
            expected_hooks,
        )

        legacy_compact_path = _temp_settings(tmp / "legacy-compact", [])
        legacy_compact_doc = {
            "permissions": {"deny": []},
            "pre_compact": "python3 /user/hooks/pre_compact.py",
            "post_compact": "python3 /notify/hooks/post_compact.py",
            "hooks": {
                "pre_compact": [{"hooks": [{"type": "command", "command": "python3 /notify/hooks/pre_compact.py"}]}],
                "PreCompact": [{"hooks": [{"type": "command", "command": "python3 /user/hooks/pre_compact.py"}]}],
                "PostCompact": [{"hooks": [{"type": "command", "command": "python3 /notify/hooks/post_compact.py"}]}],
                "Stop": [{"hooks": [{"type": "command", "command": "python3 /notify/hooks/stop_idle.py", "timeout": 5000}]}],
            },
        }
        legacy_compact_path.write_text(json.dumps(legacy_compact_doc, indent=2) + "\n", encoding="utf-8")
        with mock.patch("fleet_orchestrator.easy_setup.resolve_notify_root", return_value=Path("/notify")):
            legacy_result = apply_claude_permission_guard(legacy_compact_path, apply=True)
            legacy_second = apply_claude_permission_guard(legacy_compact_path, apply=True)
        legacy_settings = json.loads(legacy_compact_path.read_text(encoding="utf-8"))
        _assert(
            "legacy-compact-hooks-removed",
            legacy_settings.get("pre_compact") == "python3 /user/hooks/pre_compact.py"
            and "post_compact" not in legacy_settings
            and "pre_compact" not in legacy_settings.get("hooks", {})
            and "PreCompact" in legacy_settings.get("hooks", {})
            and "PostCompact" not in legacy_settings.get("hooks", {})
            and "Stop" in legacy_settings.get("hooks", {})
            and set(legacy_result["legacy_compact_hooks_removed"]) == {"post_compact", "hooks.pre_compact", "hooks.PostCompact"}
            and legacy_second["legacy_compact_hooks_removed"] == [],
            legacy_settings,
        )

        with mock.patch.object(easy_setup, "STATE_DIR", tmp / "drift-state"), \
             mock.patch.object(easy_setup, "SETUP_STATE_PATH", tmp / "drift-state" / "easy_setup_state.json"), \
             mock.patch.object(easy_setup, "CLAUDE_SETTINGS_PATH", tmp / "drift-b" / "settings.json"):
            drift_original = _temp_settings(tmp / "drift-a", ["Original"]).read_text(encoding="utf-8")
            recorded_path = tmp / "drift-a" / "settings.json"
            snapshot_claude_settings(recorded_path, original_text=drift_original)
            refused = False
            try:
                restore_claude_settings_backup()
            except RuntimeError:
                refused = True
            restored = restore_claude_settings_backup(allow_path_drift=True)
        _assert("path-drift-restore-refuse", refused and restored is not None, {"refused": refused, "restored": str(restored) if restored else None})

        pending_path = _temp_settings(tmp / "pending", [])
        pending_doc = {
            "permissions": {"deny": []},
            "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": "python3 /notify/hooks/stop_idle.py", "timeout": 5000}]}]
            },
        }
        pending_path.write_text(json.dumps(pending_doc, indent=2) + "\n", encoding="utf-8")
        with mock.patch.object(easy_setup, "STATE_DIR", tmp / "pending-state"), \
             mock.patch.object(easy_setup, "SETUP_STATE_PATH", tmp / "pending-state" / "easy_setup_state.json"), \
             mock.patch("fleet_orchestrator.easy_setup.resolve_notify_root", return_value=Path("/notify")):
            easy_setup.save_setup_state(
                {
                    "pending_hook_transaction": {
                        "notify_root": "/notify",
                        "before_hooks": {"Stop": []},
                        "created_at": 1.0,
                    }
                }
            )
            pending_result = reconcile_pending_hook_transaction(pending_path)
            pending_settings = json.loads(pending_path.read_text(encoding="utf-8"))
        _assert(
            "pending-txn-crash-reconcile",
            pending_result["reconciled"] and pending_result["hook_commands_added"]["Stop"] == ["python3 /notify/hooks/stop_idle.py"] and pending_settings["_managedBy"]["claude-code-fleet-orchestrator"]["hooks"]["commands_added"]["Stop"] == ["python3 /notify/hooks/stop_idle.py"],
            pending_settings,
        )

        pending_lazy_path = _temp_settings(tmp / "pending-lazy", [])
        pending_lazy_doc = {
            "permissions": {"deny": []},
            "hooks": {
                "Stop": [{"hooks": [{"type": "command", "command": "python3 /notify/hooks/stop_idle.py", "timeout": 5000}]}]
            },
        }
        pending_lazy_path.write_text(json.dumps(pending_lazy_doc, indent=2) + "\n", encoding="utf-8")
        with mock.patch.object(easy_setup, "STATE_DIR", tmp / "pending-lazy-state"), \
             mock.patch.object(easy_setup, "SETUP_STATE_PATH", tmp / "pending-lazy-state" / "easy_setup_state.json"), \
             mock.patch("fleet_orchestrator.easy_setup.resolve_notify_root", side_effect=RuntimeError("should not resolve notify root")):
            easy_setup.save_setup_state(
                {
                    "pending_hook_transaction": {
                        "notify_root": "/notify",
                        "before_hooks": {"Stop": []},
                        "created_at": 2.0,
                    }
                }
            )
            pending_lazy_result = reconcile_pending_hook_transaction(pending_lazy_path)
            pending_lazy_state = easy_setup.load_setup_state()
            pending_lazy_settings = json.loads(pending_lazy_path.read_text(encoding="utf-8"))
        _assert(
            "pending-txn-recorded-root-adopts-without-resolve",
            pending_lazy_result["reconciled"]
            and pending_lazy_result["hook_commands_added"]["Stop"] == ["python3 /notify/hooks/stop_idle.py"]
            and "pending_hook_transaction" not in pending_lazy_state
            and pending_lazy_settings["_managedBy"]["claude-code-fleet-orchestrator"]["hooks"]["commands_added"]["Stop"] == ["python3 /notify/hooks/stop_idle.py"],
            pending_lazy_settings,
        )

        unmanaged_path = _temp_settings(tmp / "unmanaged", ["UserOnly"])
        unmanaged_before = unmanaged_path.read_text(encoding="utf-8")
        unmanaged_result = remove_claude_permission_guard(unmanaged_path, apply=True)
        unmanaged_after = unmanaged_path.read_text(encoding="utf-8")
        _assert(
            "unmanaged-uninstall-noop",
            not unmanaged_result["changed"] and unmanaged_before == unmanaged_after and "_managedBy" not in unmanaged_after,
            unmanaged_after,
        )

        with mock.patch("fleet_orchestrator.easy_setup.detect_local_infra_ports", return_value={"redis": True, "neo4j": False}), \
             mock.patch("fleet_orchestrator.easy_setup.set_compose_managed") as set_compose_managed, \
             mock.patch("fleet_orchestrator.easy_setup.require_command") as require_command, \
             mock.patch("fleet_orchestrator.easy_setup.resolve_notify_root") as resolve_notify_root, \
             mock.patch("subprocess.run") as subrun:
            require_command.side_effect = lambda name: f"/usr/bin/{name}"
            resolve_notify_root.return_value = Path("/tmp/notify-root")
            subrun.return_value.returncode = 0
            loader = importlib.machinery.SourceFileLoader("orch_install_test", str(ROOT / "scripts" / "install"))
            spec = importlib.util.spec_from_loader("orch_install_test", loader)
            assert spec is not None
            module = importlib.util.module_from_spec(spec)
            loader.exec_module(module)
            install_main = module.main
            with mock.patch.object(sys, "argv", ["install", "--dry-run"]):
                rc = install_main()
        called_commands = [" ".join(call.args[0]) for call in subrun.call_args_list if call.args]
        _assert("byo-no-docker", rc == 0 and not set_compose_managed.call_args_list and not any(command.startswith("/usr/bin/docker") or command.startswith("docker ") for command in called_commands), called_commands)

        dry_run_settings = tmp / "dry-run" / "settings.json"
        dry_run_state = tmp / "dry-run-state"
        dry_run_notify_root = tmp / "dry-run-notify"
        (dry_run_notify_root / "scripts").mkdir(parents=True)
        (dry_run_notify_root / "identity.py").write_text("# dry-run fixture\n", encoding="utf-8")
        install_hooks = dry_run_notify_root / "scripts" / "install-hooks.sh"
        install_hooks.write_text("#!/usr/bin/env sh\nexit 0\n", encoding="utf-8")
        install_hooks.chmod(0o755)
        dry_run_env = os.environ.copy()
        dry_run_env.update(
            {
                "CLAUDE_SETTINGS_PATH": str(dry_run_settings),
                "ORCH_STATE_DIR": str(dry_run_state),
                "ORCH_NOTIFY_LIB_ROOT": str(dry_run_notify_root),
            }
        )
        dry_run = subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "install"), "--dry-run", "--skip-compose"],
            cwd=str(ROOT),
            env=dry_run_env,
            capture_output=True,
            text=True,
            check=False,
        )
        _assert(
            "dry-run-writes-nothing",
            dry_run.returncode == 0 and not dry_run_settings.exists() and not dry_run_state.exists(),
            {
                "returncode": dry_run.returncode,
                "stdout": dry_run.stdout,
                "stderr": dry_run.stderr,
                "settings_exists": dry_run_settings.exists(),
                "state_exists": dry_run_state.exists(),
            },
        )
        notify_no_deps_root = tmp / "notify-no-deps"
        notify_no_deps_root.mkdir()
        (notify_no_deps_root / "identity.py").write_text("# ok\n", encoding="utf-8")
        no_config_import = subprocess.run(
            [
                sys.executable,
                "-c",
                (
                    "import builtins, os\n"
                    "real_import = builtins.__import__\n"
                    "def guarded_import(name, *args, **kwargs):\n"
                    "    if name == 'fleet_orchestrator.config':\n"
                    "        raise AssertionError('config imported before install deps')\n"
                    "    return real_import(name, *args, **kwargs)\n"
                    "builtins.__import__ = guarded_import\n"
                    "from fleet_orchestrator.easy_setup import resolve_notify_root\n"
                    "print(resolve_notify_root())\n"
                ),
            ],
            cwd=str(ROOT),
            env={**os.environ, "PYTHONPATH": str(ROOT), "ORCH_NOTIFY_LIB_ROOT": str(notify_no_deps_root)},
            capture_output=True,
            text=True,
            check=False,
        )
        _assert(
            "install-notify-resolution-does-not-import-config",
            no_config_import.returncode == 0 and str(notify_no_deps_root) in no_config_import.stdout,
            {"returncode": no_config_import.returncode, "stdout": no_config_import.stdout, "stderr": no_config_import.stderr},
        )

        with mock.patch("fleet_orchestrator.easy_setup.ensure_claude_integration", return_value={"guard": {"changed": False}}), \
             mock.patch("fleet_orchestrator.easy_setup.managed_python", return_value="/tmp/fake-venv-python"), \
             mock.patch("fleet_orchestrator.easy_setup._spawn_background", side_effect=[1234, 5678]), \
             mock.patch("fleet_orchestrator.easy_setup.write_pid_record"), \
             mock.patch("fleet_orchestrator.easy_setup.port_open", return_value=False), \
             mock.patch("fleet_orchestrator.easy_setup.read_pid_record", return_value=None):
            messages = easy_setup.enable_services()
        _assert("venv-interpreter", any("started pid=1234" in line for line in messages) and any("started pid=5678" in line for line in messages), messages)

        fake_cfg = mock.Mock(redis_host="10.1.2.3", redis_port=6399, neo4j_uri="bolt://10.9.9.9:7777", neo4j_db="neo4j")
        with mock.patch("fleet_orchestrator.easy_setup._load_config_module") as loader, \
             mock.patch("fleet_orchestrator.easy_setup._redis_ping") as redis_ping, \
             mock.patch("fleet_orchestrator.easy_setup._neo4j_probe") as neo4j_probe:
            OrchConfig = mock.Mock(return_value=fake_cfg)
            loader.return_value = (OrchConfig, mock.Mock(), mock.Mock())
            result = easy_setup._doctor_infra()
        _assert(
            "doctor-real-probe-configured-endpoints",
            result.ok and redis_ping.call_args.args[0] is fake_cfg and neo4j_probe.call_args.args[0] is fake_cfg,
            result,
        )
        with mock.patch("fleet_orchestrator.easy_setup.resolve_notify_root", return_value=tmp / "notify-root"):
            (tmp / "notify-root").mkdir()
            (tmp / "notify-root" / "identity.py").write_text("# ok\n", encoding="utf-8")
            notify_root_check = easy_setup._doctor_notify_root()
        _assert("doctor-checks-notify-root-resolution", notify_root_check.ok, notify_root_check)

        with mock.patch("fleet_orchestrator.tasks_api.get_ready_tasks", return_value=[]):
            client = TestClient(app)
            health = client.get("/health")
            payload = health.json()
        _assert("release-version-identity", package_version() == EXPECTED_RELEASE, package_version())
        _assert(
            "health-version-identity",
            health.status_code == 200 and payload.get("version") == package_version(),
            payload,
        )
        _assert("health-version-release", payload.get("version") == EXPECTED_RELEASE, payload)

        scope = compose_scope()
        _assert(
            "doctor-scope-shape",
            "127.0.0.1:7687" in scope["ports"] and "orch_neo4j_data" in scope["volumes"] and "SessionStart" in scope["hooks"] and any(path.endswith("settings.json") for path in scope["files"]),
            scope,
        )

        if os.environ.get("EASY_SETUP_ACCEPTANCE_INJECT_FAIL") == "1":
            _assert("injected-fail", False, "EASY_SETUP_ACCEPTANCE_INJECT_FAIL=1")

    return 1 if FAILURES else 0


if __name__ == "__main__":
    raise SystemExit(main())
