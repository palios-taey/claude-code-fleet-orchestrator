#!/usr/bin/env python3
"""Acceptance: wake packets inject selector-mapped Knowledge Base nodes."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import tempfile
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _require_test_namespace() -> str:
    raw = (os.environ.get("ORCH_TEST_NAMESPACE") or "").strip()
    if not raw:
        raise SystemExit("ORCH_TEST_NAMESPACE is required")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{5,80}", raw):
        raise SystemExit("ORCH_TEST_NAMESPACE must be 6-80 chars of letters/digits/._:-")
    lowered = raw.lower()
    if not any(marker in lowered for marker in ("test", "ci", "acceptance")):
        raise SystemExit("ORCH_TEST_NAMESPACE must include test, ci, or acceptance")
    if lowered in {"prod", "production", "neo4j", "default"}:
        raise SystemExit("ORCH_TEST_NAMESPACE must not name a production/default namespace")
    return raw


PFX = f"{_require_test_namespace()}-kbpush-{uuid.uuid4().hex[:8]}"
os.environ["NOTIFY_KEY_PREFIX"] = PFX
from fleet_orchestrator.test_isolation import assert_acceptance_redis_isolated  # noqa: E402

assert_acceptance_redis_isolated()

import fleet_orchestrator.tasks_api as tasks_api  # noqa: E402
from fleet_orchestrator.config import OrchConfig, get_neo4j_driver  # noqa: E402
from fleet_orchestrator.orch_schema import create_phase, create_project, create_task, init_schema  # noqa: E402


CFG = OrchConfig()
CLIENT = TestClient(tasks_api.app)
SUPERVISOR = f"{PFX}-supervisor"
WORKER = f"{PFX}-job-seeker"
PROJECT = f"{PFX}-project"
PHASE = f"{PROJECT}::phase"
TASK = f"{PROJECT}::apply"
NONMATCH_TASK = f"{PROJECT}::generic"
FAILURES: list[str] = []
ENV_KEYS = ("ORCH_KB_NEO4J_URI", "ORCH_KB_MAP_PATH", "ORCH_REF_ALLOWED_ROOT", "ORCH_SESSION_IDS")


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


@contextmanager
def _preserved_env() -> Iterator[None]:
    original = {key: os.environ.get(key) for key in ENV_KEYS}
    try:
        for key in ENV_KEYS:
            os.environ.pop(key, None)
        yield
    finally:
        for key, value in original.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def _cleanup() -> None:
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        session.run(
            """
            MATCH (n)
            WHERE coalesce(n.id, '') STARTS WITH $prefix
               OR coalesce(n.stable_key, '') STARTS WITH $prefix
               OR coalesce(n.test_prefix, '') = $prefix
            DETACH DELETE n
            """,
            prefix=PFX,
        )


def _seed_tasks(ref_root: Path) -> None:
    source = ref_root / "plan.md"
    source.write_text("# kb push fixture\n", encoding="utf-8")
    (ref_root / "dedupe.md").write_text("DUPLICATE_REF_CONTENT\n", encoding="utf-8")
    create_project(PROJECT, "kb push project", supervisor=SUPERVISOR, priority=1, config=CFG)
    create_phase(PROJECT, PHASE, "phase", config=CFG)
    create_task(
        PHASE,
        TASK,
        "Careers application task that needs mapped KB rulings",
        owner=WORKER,
        priority=1,
        refs=[{"path": "dedupe.md", "l_start": 1, "l_end": 1}],
        source_path=str(source),
        capability_tags=["careers", "applications"],
        wake_owner_if_ready=False,
        config=CFG,
    )
    create_task(
        PHASE,
        NONMATCH_TASK,
        "Generic task that should not get careers KB",
        owner=WORKER,
        priority=2,
        capability_tags=["generic"],
        wake_owner_if_ready=False,
        config=CFG,
    )


def _seed_kb(nodes: dict[str, str]) -> None:
    with get_neo4j_driver(CFG).session(database=CFG.neo4j_db) as session:
        for idx, (stable_key, content) in enumerate(nodes.items(), start=1):
            session.run(
                """
                MERGE (entity:KnowledgeEntity {stable_key: $stable_key})
                SET entity.entity_type = 'policy',
                    entity.layer = 'operator-kb',
                    entity.active_status = 'active',
                    entity.test_prefix = $prefix
                MERGE (revision:KnowledgeRevision {stable_key: $stable_key, revision_no: $revision_no})
                SET revision.title = $title,
                    revision.summary = $summary,
                    revision.content = $content,
                    revision.truth_register = 'Observed',
                    revision.test_prefix = $prefix
                MERGE (entity)-[:CURRENT_REVISION]->(revision)
                """,
                stable_key=stable_key,
                revision_no=idx,
                title=f"title {idx}",
                summary=f"summary for {stable_key}",
                content=content,
                prefix=PFX,
            )


def _write_map(path: Path, data: dict[str, object]) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _wake(task_id: str) -> dict[str, object]:
    response = CLIENT.get(f"/api/sessions/{WORKER}/wake-packet", params={"cli": "codex", "task_id": task_id})
    _check("wake endpoint returns HTTP 200", response.status_code == 200, response.text)
    return response.json()


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def main() -> int:
    _cleanup()
    with _preserved_env(), tempfile.TemporaryDirectory(prefix="kb-push-") as raw:
        tmp = Path(raw)
        ref_root = tmp / "refs"
        ref_root.mkdir()
        map_path = tmp / "kb-map.json"
        try:
            init_schema(config=CFG)
            _seed_tasks(ref_root)

            os.environ["ORCH_REF_ALLOWED_ROOT"] = str(ref_root)
            os.environ["ORCH_SESSION_IDS"] = ""
            unconfigured = _wake(TASK)
            unconfigured_packet = str(unconfigured.get("packet") or "")
            _check("unconfigured KB layer leaves route successful", unconfigured.get("ok") is True, unconfigured)
            _check("unconfigured KB layer renders no Knowledge Base section", "## Knowledge Base" not in unconfigured_packet, unconfigured_packet)

            keys = {
                f"{PFX}::policy::stakes": "STAKES_POLICY_CONTENT\n",
                f"{PFX}::policy::no_tests": "NO_TESTS_POLICY_CONTENT\n",
                f"{PFX}::process::apply": "APPLY_RUNBOOK_CONTENT\n",
                f"{PFX}::policy::dedupe": "DUPLICATE_REF_CONTENT",
            }
            _seed_kb(keys)
            _write_map(
                map_path,
                {
                    "universal": [f"{PFX}::policy::stakes", f"{PFX}::policy::no_tests"],
                    "selectors": [
                        {
                            "match": {"tags_any": ["careers", "linkedin"]},
                            "keys": [f"{PFX}::process::apply", f"{PFX}::policy::dedupe"],
                        }
                    ],
                },
            )
            os.environ["ORCH_KB_NEO4J_URI"] = os.environ["ORCH_NEO4J_URI"]
            os.environ["ORCH_KB_MAP_PATH"] = str(map_path)

            configured = _wake(TASK)
            packet = str(configured.get("packet") or "")
            meta = configured.get("packet_meta") if isinstance(configured.get("packet_meta"), dict) else {}
            snapshot = meta.get("snapshot") if isinstance(meta.get("snapshot"), dict) else {}
            kb_snapshot = snapshot.get("kb_context") if isinstance(snapshot, dict) else []
            order = [packet.find(key) for key in keys]
            _check("configured KB layer leaves route successful", configured.get("ok") is True, configured)
            _check("configured packet contains Knowledge Base section", "## Knowledge Base" in packet, packet)
            _check("KB keys render universal first then selector keys", all(pos >= 0 for pos in order) and order == sorted(order), order)
            _check("KB node includes stable_key and revision metadata", "stable_key:" in packet and "revision_no:" in packet, packet)
            _check("KB node includes content sha256", _sha("APPLY_RUNBOOK_CONTENT\n") in packet, packet)
            _check("KB content is wrapped as untrusted data", "<<UNTRUSTED-DATA " in packet and f"source=\"kb:{PFX}::policy::stakes\"" in packet, packet)
            _check("deduped KB node does not repeat ref content", packet.count("DUPLICATE_REF_CONTENT") == 1 and "deduped: true" in packet, packet)
            _check("packet metadata records KB provenance", len(kb_snapshot or []) == 4 and all("content_sha256" in item for item in kb_snapshot), kb_snapshot)

            _write_map(
                map_path,
                {
                    "universal": [],
                    "selectors": [{"match": {"tags_any": ["careers"]}, "keys": [f"{PFX}::missing"]}],
                },
            )
            missing = _wake(TASK)
            _check("mapped missing KB key fails loud at route body", missing.get("ok") is False and f"{PFX}::missing" in str(missing.get("error")), missing)

            _write_map(
                map_path,
                {
                    "universal": [f"{PFX}::policy::stakes"],
                    "selectors": [{"match": {"tags_any": ["linkedin"]}, "keys": [f"{PFX}::process::apply"]}],
                },
            )
            nonmatching = _wake(NONMATCH_TASK)
            nonmatching_packet = str(nonmatching.get("packet") or "")
            _check("nonmatching task leaves route successful", nonmatching.get("ok") is True, nonmatching)
            _check("nonmatching task renders no Knowledge Base section", "## Knowledge Base" not in nonmatching_packet, nonmatching_packet)
        finally:
            _cleanup()

    if FAILURES:
        print(f"\nFAIL -- {len(FAILURES)} assertion(s): {FAILURES}")
        return 1
    print("\nPASS -- wake-packet KB push layer injects mapped current revisions and fails loud on missing keys.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
