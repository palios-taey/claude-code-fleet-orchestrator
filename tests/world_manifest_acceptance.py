"""Acceptance: World Manifest v0 is content-rooted and causally published."""
from __future__ import annotations

import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet_orchestrator.causal_ledger import verify_chain  # noqa: E402
from fleet_orchestrator.world_manifest import (  # noqa: E402
    KNOWLEDGE_INDEX_ENV,
    SYSTEM_MAP_ENV,
    WORLD_MANIFEST_ENV,
    build_world_manifest_v0,
    canonical_json,
    publish_world_manifest_v0,
)


FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rows(path: Path) -> list[dict]:
    rows: list[dict] = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        if raw.startswith("#"):
            continue
        rows.append(json.loads(raw))
    return rows


def _write_index(path: Path) -> None:
    path.write_text(
        json.dumps(
            {
                "index_id": "taey-knowledge-index",
                "generated_at_commit": "070ee49128fc31ee9686f1c6bdc4680a197d8b37",
                "live_url": "https://github.com/palios-taey/taey-presence/raw/main/serving/knowledge_index/index.json",
                "sections": {
                    "presence": {
                        "capabilities": [
                            {
                                "id": "presence-serve",
                                "kind": "serve",
                                "status": "production",
                                "repo": {
                                    "name": "palios-taey/taey-presence",
                                    "pinned_sha": "070ee49128fc31ee9686f1c6bdc4680a197d8b37",
                                },
                                "artifact_commit_sha": "34823a643428b5a0c93086ecd2c9231b4b1eac28",
                                "artifact_manifest": {
                                    "path": "serving/manifests/presence-serve.artifacts.json",
                                    "sha256": "8acbd68c973a601d4e9657554ce99ac5e6081807b85e27e0367bf3c3dda579d4",
                                },
                                "receipts": {
                                    "liveness": "serving/receipts/presence-serve.liveness.json",
                                    "liveness_sha256": "af7f5696d4e5c7a0417a1917c58beac989341ae7b06a61dd7cbbd9e39d65a78a",
                                },
                            },
                            {
                                "id": "presence-draft",
                                "kind": "serve",
                                "status": "staging",
                            },
                        ]
                    }
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def main() -> int:
    saved = {
        name: os.environ.get(name)
        for name in (SYSTEM_MAP_ENV, KNOWLEDGE_INDEX_ENV, WORLD_MANIFEST_ENV, "ORCH_CAUSAL_LEDGER_PATH")
    }
    with tempfile.TemporaryDirectory(prefix="world-manifest-") as raw:
        root = Path(raw)
        system_map = root / "TAEY_SYSTEM_CONNECTION_MAP.md"
        index = root / "index.json"
        manifest_path = root / "world-manifest-v0.json"
        ledger_path = root / "causal.jsonl"
        system_map.write_text("# system map\nObserved edge.\n", encoding="utf-8")
        _write_index(index)
        os.environ[SYSTEM_MAP_ENV] = str(system_map)
        os.environ[KNOWLEDGE_INDEX_ENV] = str(index)
        os.environ[WORLD_MANIFEST_ENV] = str(manifest_path)
        os.environ["ORCH_CAUSAL_LEDGER_PATH"] = str(ledger_path)
        try:
            manifest = build_world_manifest_v0(as_of="2026-08-01T00:00:00Z")
            roots = manifest["roots"]
            _check("world id is content oid", str(manifest.get("world_id", "")).startswith("world:"), manifest)
            _check("system map sha is measured", roots["system_connection_map"]["sha256"] == _sha256(system_map), roots)
            _check("knowledge index sha is measured", roots["taey_presence_index"]["sha256"] == _sha256(index), roots)
            _check(
                "only production capabilities are included",
                [item["id"] for item in roots["production_capabilities"]] == ["presence-serve"],
                roots["production_capabilities"],
            )
            later_manifest = build_world_manifest_v0(as_of="2026-08-01T00:01:00Z")
            _check("world id ignores as_of churn", later_manifest["world_id"] == manifest["world_id"], later_manifest)
            publication = publish_world_manifest_v0(
                subject={"task_id": "world::manifest"},
                parents=["event:parent"],
                as_of="2026-08-01T00:02:00Z",
            )
            _check(
                "publisher writes canonical manifest",
                manifest_path.read_text(encoding="utf-8") == canonical_json(publication["manifest"]) + "\n",
            )
            rows = _rows(ledger_path)
            event = rows[-1]["event"]
            _check("publisher appends causal event", event["event_type"] == "world_manifest_published", event)
            _check("publication event carries world id", event["payload"]["world_id"] == publication["world_id"], event)
            _check(
                "ledger verifies after publication",
                verify_chain(str(ledger_path)) == {"ok": True, "rows": 1},
                verify_chain(str(ledger_path)),
            )
            os.environ[KNOWLEDGE_INDEX_ENV] = str(root / "missing-index.json")
            missing = build_world_manifest_v0(as_of="2026-08-01T00:03:00Z")
            _check(
                "missing knowledge index becomes Unknown root",
                missing["roots"]["taey_presence_index"]["register"] == "Unknown"
                and missing["roots"]["production_capabilities"][0]["register"] == "Unknown"
                and str(missing["world_id"]).startswith("world:"),
                missing["roots"],
            )
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
    if FAILURES:
        print("FAILURES:", ", ".join(FAILURES))
        return 1
    print("world_manifest_acceptance: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
