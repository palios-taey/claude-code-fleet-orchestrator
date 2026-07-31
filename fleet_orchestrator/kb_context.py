from __future__ import annotations

import hashlib
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from neo4j import GraphDatabase


LOG = logging.getLogger(__name__)
_KB_DISABLED_LOGGED = False


class KnowledgeBaseContextError(RuntimeError):
    """Raised when a selector-matched KB context cannot be assembled."""


def select_kb_context(session: str, work: Dict[str, Any], refs: Dict[str, List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
    config = _kb_config()
    if config is None:
        return None

    mapping = _load_kb_map(config["map_path"])
    keys = _select_keys(mapping, session=session, work=work)
    if not keys:
        return None

    ref_shas = _ref_content_shas(refs)
    nodes = _fetch_kb_nodes(config["neo4j_uri"], keys)
    for node in nodes:
        content_sha = _sha256_text(str(node.get("content") or ""))
        node["content_sha256"] = content_sha
        if content_sha in ref_shas:
            node["deduped"] = True
            node["content"] = ""
        else:
            node["deduped"] = False
    return nodes


def _kb_config() -> Optional[Dict[str, str]]:
    global _KB_DISABLED_LOGGED
    uri = (os.environ.get("ORCH_KB_NEO4J_URI") or "").strip()
    map_path = (os.environ.get("ORCH_KB_MAP_PATH") or "").strip()
    if not uri and not map_path:
        if not _KB_DISABLED_LOGGED:
            LOG.info("knowledge-base context injection disabled: ORCH_KB_NEO4J_URI and ORCH_KB_MAP_PATH unset")
            _KB_DISABLED_LOGGED = True
        return None
    if not uri or not map_path:
        raise KnowledgeBaseContextError("ORCH_KB_NEO4J_URI and ORCH_KB_MAP_PATH must both be set for KB context injection")
    return {"neo4j_uri": uri, "map_path": map_path}


def _load_kb_map(raw_path: str) -> Dict[str, Any]:
    path = Path(raw_path).expanduser().resolve(strict=False)
    if not path.is_file():
        raise KnowledgeBaseContextError(f"ORCH_KB_MAP_PATH is not readable: {raw_path}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise KnowledgeBaseContextError(f"ORCH_KB_MAP_PATH must be readable JSON: {raw_path}") from exc
    if not isinstance(data, dict):
        raise KnowledgeBaseContextError("ORCH_KB_MAP_PATH must contain a JSON object")
    universal = data.get("universal", [])
    selectors = data.get("selectors", [])
    if not isinstance(universal, list) or not all(isinstance(item, str) for item in universal):
        raise KnowledgeBaseContextError("ORCH_KB_MAP_PATH universal must be a list of stable_key strings")
    if not isinstance(selectors, list):
        raise KnowledgeBaseContextError("ORCH_KB_MAP_PATH selectors must be a list")
    for selector in selectors:
        if not isinstance(selector, dict) or not isinstance(selector.get("match"), dict):
            raise KnowledgeBaseContextError("ORCH_KB_MAP_PATH selectors entries must contain a match object")
        keys = selector.get("keys", [])
        if not isinstance(keys, list) or not all(isinstance(item, str) for item in keys):
            raise KnowledgeBaseContextError("ORCH_KB_MAP_PATH selector keys must be stable_key strings")
    return data


def _select_keys(mapping: Dict[str, Any], *, session: str, work: Dict[str, Any]) -> List[str]:
    selected: List[str] = []
    matched = False
    for selector in mapping.get("selectors") or []:
        if _selector_matches(selector.get("match") or {}, session=session, work=work):
            matched = True
            selected.extend(str(key) for key in selector.get("keys") or [])
    if not matched:
        return []
    ordered = [str(key) for key in mapping.get("universal") or []] + selected
    return _dedupe_preserving_order(key.strip() for key in ordered if key and key.strip())


def _selector_matches(match: Dict[str, Any], *, session: str, work: Dict[str, Any]) -> bool:
    owner_prefix = str(match.get("owner_prefix") or "").strip()
    tags_any = _as_list(match.get("tags_any"))
    tags_none = _as_list(match.get("tags_none"))
    if owner_prefix:
        candidates = [
            str(work.get("owner") or ""),
            str(work.get("dispatched_to") or ""),
            str(session or ""),
        ]
        if not any(candidate.startswith(owner_prefix) for candidate in candidates if candidate):
            return False
    if tags_any or tags_none:
        tags = set(_as_list(work.get("capability_tags")))
        if tags_any and not tags.intersection(tags_any):
            return False
        if tags_none and tags.intersection(tags_none):
            return False
    return bool(owner_prefix or tags_any)


def _fetch_kb_nodes(uri: str, stable_keys: List[str]) -> List[Dict[str, Any]]:
    rows: Dict[str, Dict[str, Any]] = {}
    try:
        driver = GraphDatabase.driver(uri, auth=None)
        try:
            with driver.session() as session:
                result = session.run(
                    """
                    UNWIND $stable_keys AS stable_key
                    MATCH (entity:KnowledgeEntity {stable_key: stable_key})-[:CURRENT_REVISION]->(revision:KnowledgeRevision)
                    RETURN entity.stable_key AS stable_key,
                           entity.entity_type AS entity_type,
                           entity.layer AS layer,
                           entity.active_status AS active_status,
                           revision.title AS title,
                           revision.summary AS summary,
                           revision.content AS content,
                           revision.truth_register AS truth_register,
                           revision.revision_no AS revision_no
                    """,
                    stable_keys=stable_keys,
                )
                for record in result:
                    row = dict(record)
                    rows[str(row.get("stable_key") or "")] = row
        finally:
            driver.close()
    except Exception as exc:
        raise KnowledgeBaseContextError(f"configured KB Neo4j is unreachable or invalid: {exc}") from exc

    missing = [key for key in stable_keys if key not in rows]
    if missing:
        raise KnowledgeBaseContextError(f"mapped KB stable_key missing from KnowledgeEntity CURRENT_REVISION: {', '.join(missing)}")
    return [rows[key] for key in stable_keys]


def _ref_content_shas(refs: Dict[str, List[Dict[str, Any]]]) -> set[str]:
    shas: set[str] = set()
    for items in refs.values():
        for ref in items or []:
            content = ref.get("content")
            if content:
                shas.add(_sha256_text(str(content)))
            for section in ref.get("sections") or []:
                section_content = section.get("content")
                if section_content:
                    shas.add(_sha256_text(str(section_content)))
    return shas


def _as_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value).replace(";", ",").split(",") if item.strip()]


def _dedupe_preserving_order(values: Iterable[str]) -> List[str]:
    seen: set[str] = set()
    result: List[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()
