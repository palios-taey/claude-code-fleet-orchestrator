"""Migrate persisted supervisor state to configured Codex control principals."""
from __future__ import annotations

import argparse
import json
from typing import Any, Iterable

from .config import OrchConfig, get_neo4j_driver


def codex_supervisor_mappings(session_ids: Iterable[str]) -> tuple[dict[str, str], ...]:
    mappings = []
    for raw in session_ids:
        control = str(raw or "").strip()
        if not control.lower().endswith("-codex"):
            continue
        family = control[: -len("-codex")]
        if family:
            mappings.append({"from": family, "to": control})
    return tuple(mappings)


def _measure(tx, old: str) -> dict[str, int]:
    projects = tx.run(
        "MATCH (p:OrchProject {supervisor: $old}) RETURN count(p) AS count",
        old=old,
    ).single()["count"]
    tasks = tx.run(
        "MATCH (t:OrchTask {owner: $old}) RETURN count(t) AS count",
        old=old,
    ).single()["count"]
    refs = tx.run(
        "MATCH (s:OrchSupervisor {session: $old}) RETURN count(s) AS count",
        old=old,
    ).single()["count"]
    return {"projects": int(projects), "tasks": int(tasks), "supervisor_refs": int(refs)}


def _ref_state(tx, old: str, new: str) -> dict[str, Any]:
    record = tx.run(
        """
        OPTIONAL MATCH (old:OrchSupervisor {session: $old})
        OPTIONAL MATCH (new:OrchSupervisor {session: $new})
        RETURN old.refs AS old_refs, new.refs AS new_refs
        """,
        old=old,
        new=new,
    ).single()
    return dict(record) if record else {"old_refs": None, "new_refs": None}


def migrate_control_principals(config: OrchConfig, *, apply: bool = False) -> dict[str, Any]:
    mappings = codex_supervisor_mappings(config.session_ids)
    if not mappings:
        raise ValueError("ORCH_SESSION_IDS contains no *-codex control principals to migrate")

    driver = get_neo4j_driver(config)
    with driver.session(database=config.neo4j_db) as session:
        tx = session.begin_transaction()
        try:
            observations = []
            for mapping in mappings:
                old = mapping["from"]
                new = mapping["to"]
                before = _measure(tx, old)
                refs = _ref_state(tx, old, new)
                old_refs = refs.get("old_refs")
                new_refs = refs.get("new_refs")
                if (
                    old_refs not in (None, "", "[]")
                    and new_refs not in (None, "", "[]")
                    and old_refs != new_refs
                ):
                    raise ValueError(
                        f"ref conflict for {old!r} -> {new!r}; reconcile both OrchSupervisor refs before migration"
                    )
                if apply:
                    tx.run(
                        "MATCH (p:OrchProject {supervisor: $old}) "
                        "SET p.supervisor = $new, p.updated_at = datetime()",
                        old=old,
                        new=new,
                    ).consume()
                    tx.run(
                        "MATCH (t:OrchTask {owner: $old}) "
                        "SET t.owner = $new, t.updated_at = datetime()",
                        old=old,
                        new=new,
                    ).consume()
                    tx.run(
                        """
                        OPTIONAL MATCH (old:OrchSupervisor {session: $old})
                        MERGE (new:OrchSupervisor {session: $new})
                        ON CREATE SET new.created_at = datetime()
                        SET new.refs = CASE
                            WHEN old IS NOT NULL AND (new.refs IS NULL OR new.refs = '[]') THEN old.refs
                            ELSE new.refs
                        END,
                        new.updated_at = datetime()
                        WITH old, new
                        FOREACH (_ IN CASE WHEN old IS NOT NULL THEN [1] ELSE [] END | DETACH DELETE old)
                        RETURN new.session AS session
                        """,
                        old=old,
                        new=new,
                    ).consume()
                observations.append({**mapping, "before": before})
            if apply:
                tx.commit()
            else:
                tx.rollback()
        except Exception:
            tx.rollback()
            raise

    result: dict[str, Any] = {
        "ok": True,
        "mode": "apply" if apply else "dry_run",
        "mappings": observations,
    }
    if apply:
        with driver.session(database=config.neo4j_db) as session:
            result["remaining_old_records"] = {
                mapping["from"]: _measure(session, mapping["from"])
                for mapping in mappings
            }
    return result


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Migrate bare-supervisor graph state to configured *-codex controls."
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="commit the transaction; without this flag the command only reports counts",
    )
    args = parser.parse_args()
    print(json.dumps(migrate_control_principals(OrchConfig(), apply=args.apply), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
