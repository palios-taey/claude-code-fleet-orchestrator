"""Acceptance: checkpointing, witness anchoring, verifier fail-closed behavior."""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fleet_orchestrator.causal_ledger import append_event, checkpoint_ledger, latest_checkpoint_root, verify_chain  # noqa: E402
from fleet_orchestrator.provenance_verifier import (  # noqa: E402
    reverify_recorded_world_manifests,
    verify_provenance_kernel_closure,
)
from fleet_orchestrator.provenance_witness import (  # noqa: E402
    WITNESS_ADAPTER_ENV,
    WITNESS_ENABLED_ENV,
    WITNESS_PATH_ENV,
    WITNESS_PRINCIPAL_ENV,
    WitnessConfigError,
    publish_external_witness_anchor,
)
from fleet_orchestrator.world_manifest import (  # noqa: E402
    KNOWLEDGE_INDEX_ENV,
    SYSTEM_MAP_ENV,
    build_world_manifest_v0,
    publish_world_manifest_v0,
    reverify_world_manifest,
)


FAILURES: list[str] = []


def _check(label: str, cond: bool, extra: object = "") -> None:
    print(("PASS " if cond else "FAIL ") + label + ("" if cond else f" -> {extra}"))
    if not cond:
        FAILURES.append(label)


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
                "live_url": "https://example.invalid/index.json",
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
                            }
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
        for name in (
            "ORCH_CAUSAL_LEDGER_PATH",
            WITNESS_ENABLED_ENV,
            WITNESS_PRINCIPAL_ENV,
            WITNESS_PATH_ENV,
            WITNESS_ADAPTER_ENV,
            SYSTEM_MAP_ENV,
            KNOWLEDGE_INDEX_ENV,
        )
    }
    try:
        with tempfile.TemporaryDirectory(prefix="provenance-kernel-") as raw:
            root = Path(raw)
            ledger_path = root / "causal.jsonl"
            witness_path = root / "witness.jsonl"
            system_map = root / "TAEY_SYSTEM_CONNECTION_MAP.md"
            index = root / "index.json"
            system_map.write_text("# system map\nObserved edge.\n", encoding="utf-8")
            _write_index(index)
            os.environ["ORCH_CAUSAL_LEDGER_PATH"] = str(ledger_path)
            os.environ[SYSTEM_MAP_ENV] = str(system_map)
            os.environ[KNOWLEDGE_INDEX_ENV] = str(index)
            for name in (WITNESS_ENABLED_ENV, WITNESS_PRINCIPAL_ENV, WITNESS_PATH_ENV, WITNESS_ADAPTER_ENV):
                os.environ.pop(name, None)

            attestation_id = "attestation:operator-bound"
            packet_id = "packet-1"
            packet_hash = "sha256:packet-rendered"
            dispatch_row = append_event(
                "dispatch_claimed",
                subject={"task_id": "provenance::closure"},
                payload={"claim": "created"},
                path=str(ledger_path),
            )
            wake_row = append_event(
                "wake_packet_assembled",
                subject={"task_id": "provenance::closure"},
                parents=[dispatch_row["event"]["event_id"]],
                actor_attestation_id=attestation_id,
                packet_id=packet_id,
                packet_provenance_hash=packet_hash,
                payload={"packet_id": packet_id},
                path=str(ledger_path),
            )

            checkpoint = checkpoint_ledger(path=str(ledger_path))
            checkpoint_payload = checkpoint["payload"]
            _check("checkpoint appends ledger_checkpoint event", checkpoint["row"]["event"]["event_type"] == "ledger_checkpoint", checkpoint)
            _check("checkpoint records Merkle batch root", str(checkpoint_payload["batch_root"]).startswith("sha256:"), checkpoint_payload)
            _check("checkpoint records chained ledger root", str(checkpoint_payload["ledger_root"]).startswith("sha256:"), checkpoint_payload)
            _check("latest checkpoint is manifest-observable", latest_checkpoint_root(str(ledger_path))["ledger_root"] == checkpoint["ledger_root"])

            try:
                publish_external_witness_anchor(checkpoint["row"], ledger_path=str(ledger_path))
                _check("unconfigured witness anchoring fails loud", False)
            except WitnessConfigError:
                _check("unconfigured witness anchoring fails loud", True)

            no_witness = verify_provenance_kernel_closure(
                event_ids=[wake_row["event"]["event_id"]],
                actor_attestation_id=attestation_id,
                packet_id=packet_id,
                packet_provenance_hash=packet_hash,
                ledger_path=str(ledger_path),
            )
            _check(
                "closure without witness roots is rejected",
                not no_witness["ok"] and any("missing_witness_roots" in item for item in no_witness["errors"]),
                no_witness,
            )

            os.environ[WITNESS_ENABLED_ENV] = "1"
            os.environ[WITNESS_PRINCIPAL_ENV] = "palios-ledger-anchor-v1"
            os.environ[WITNESS_PATH_ENV] = str(witness_path)
            witness = publish_external_witness_anchor(checkpoint["row"], ledger_path=str(ledger_path))
            witness_body = witness_path.read_text(encoding="utf-8")
            _check("witness file is compact roots/counts only", "packet_id" not in witness_body and '"payload"' not in witness_body, witness_body)
            _check("witness appends causal anchor event", witness["row"]["event"]["event_type"] == "external_witness_anchor", witness)

            good = verify_provenance_kernel_closure(
                event_ids=[wake_row["event"]["event_id"]],
                actor_attestation_id=attestation_id,
                packet_id=packet_id,
                packet_provenance_hash=packet_hash,
                ledger_path=str(ledger_path),
            )
            _check("witnessed closure verifies", good["ok"], good)

            missing_event = verify_provenance_kernel_closure(
                event_ids=["event:not-present"],
                actor_attestation_id=attestation_id,
                packet_id=packet_id,
                packet_provenance_hash=packet_hash,
                ledger_path=str(ledger_path),
            )
            _check(
                "missing event IDs fail closed",
                not missing_event["ok"] and "missing_event_id:event:not-present" in missing_event["errors"],
                missing_event,
            )

            missing_attestation = verify_provenance_kernel_closure(
                event_ids=[wake_row["event"]["event_id"]],
                actor_attestation_id="",
                packet_id=packet_id,
                packet_provenance_hash=packet_hash,
                ledger_path=str(ledger_path),
            )
            _check(
                "missing actor attestations fail closed",
                not missing_attestation["ok"] and "missing_actor_attestation" in missing_attestation["errors"],
                missing_attestation,
            )

            bad_packet_hash = verify_provenance_kernel_closure(
                event_ids=[wake_row["event"]["event_id"]],
                actor_attestation_id=attestation_id,
                packet_id=packet_id,
                packet_provenance_hash="sha256:forged",
                ledger_path=str(ledger_path),
            )
            _check(
                "packet hash mismatches fail closed",
                not bad_packet_hash["ok"] and any("packet_hash_mismatch" in item for item in bad_packet_hash["errors"]),
                bad_packet_hash,
            )

            manifest = build_world_manifest_v0(
                as_of="2026-08-01T00:00:00Z",
                system_map_path=str(system_map),
                knowledge_index_path=str(index),
                causal_ledger_path=str(ledger_path),
            )
            _check(
                "world manifest records checkpoint root",
                manifest["roots"]["causal_ledger"]["ledger_root"] == checkpoint["ledger_root"],
                manifest["roots"]["causal_ledger"],
            )
            fresh_check = reverify_world_manifest(
                manifest=manifest,
                max_age_days=None,
                causal_ledger_path=str(ledger_path),
            )
            _check("fresh world manifest reverify passes", fresh_check["ok"], fresh_check)
            publication = publish_world_manifest_v0(
                subject={"task_id": "provenance::closure"},
                as_of="2026-08-01T00:00:00Z",
                manifest_path=str(root / "world-manifest-v0.json"),
                ledger_path=str(ledger_path),
                system_map_path=str(system_map),
                knowledge_index_path=str(index),
            )
            recorded = reverify_recorded_world_manifests(ledger_path=str(ledger_path), max_age_days=None)
            _check("recorded manifests reverify", recorded["ok"], recorded)
            system_map.write_text("# system map\nObserved drift.\n", encoding="utf-8")
            stale = reverify_world_manifest(
                manifest=publication["manifest"],
                max_age_days=None,
                causal_ledger_path=str(ledger_path),
            )
            _check(
                "stale manifest roots surface on reverify",
                not stale["ok"] and "root_drift:system_connection_map" in stale["errors"],
                stale,
            )

            _check("ledger verifies after checkpoint and witness", verify_chain(str(ledger_path))["ok"], verify_chain(str(ledger_path)))
            _check("expected four-plus causal rows written", len(_rows(ledger_path)) >= 4, _rows(ledger_path))
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    if FAILURES:
        print("FAILURES:", ", ".join(FAILURES))
        return 1
    print("provenance_kernel_acceptance: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
