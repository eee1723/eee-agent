"""Deterministic materialization and validation of the knowledge cache.

``write_cache`` writes a :class:`GraphBundle` and :class:`BuildManifest` into a
fresh SQLite database using one explicit transaction and fully parameterized
SQL. ``validate_cache`` reopens the database and checks the build self-checks
from design §9.2 (integrity, foreign keys, schema version, counts and resolved
edges). Neither function opens the database read-only or runs migrations.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from eee_agent.knowledge.manifest import BuildManifest
from eee_agent.knowledge.models import EdgeDraft, EntityDraft, GraphBundle
from eee_agent.knowledge.schema import KB_SCHEMA_VERSION, create_schema, verify_fts5

__all__ = ["CacheIntegrityError", "validate_cache", "write_cache"]

# (attribute key, facet key) pairs projected into the facets table. The facet
# key/value index backs tag/context/group/superclass and operator-status filters
# (design §8.1).
_FACET_SCALARS = (
    ("context", "context"),
    ("group", "group"),
    ("superclass", "superclass"),
    ("operator_type_status", "operator_status"),
)


class CacheIntegrityError(RuntimeError):
    """A cache self-check (integrity, schema, counts or edges) failed."""


def _entity_facets(entity: EntityDraft) -> list[tuple[str, str]]:
    facets: list[tuple[str, str]] = []
    attrs = entity.attributes
    for attr_key, facet_key in _FACET_SCALARS:
        if attr_key in attrs:
            value = str(attrs[attr_key])
            if value:
                facets.append((facet_key, value))
    tags = attrs.get("tags")
    if tags:
        items = (tags,) if isinstance(tags, str) else tags
        for tag in items:
            text = str(tag)
            if text:
                facets.append(("tag", text))
    # Deduplicate within the entity while preserving order (UNIQUE constraint).
    seen: set[tuple[str, str]] = set()
    unique: list[tuple[str, str]] = []
    for facet in facets:
        if facet not in seen:
            seen.add(facet)
            unique.append(facet)
    return unique


def _edge_id(edge: EdgeDraft) -> str:
    payload = json.dumps(
        {
            "source_id": edge.source_id,
            "predicate": edge.predicate,
            "target_id": edge.target_id,
            "target_raw": edge.target_raw,
            "target_anchor": edge.target_anchor,
            "source_location": edge.source_location,
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _tags_text(entity: EntityDraft) -> str:
    tags = entity.attributes.get("tags")
    if not tags:
        return ""
    if isinstance(tags, str):
        return tags
    return " ".join(str(tag) for tag in tags)


def _metadata_value(value: object) -> str:
    return json.dumps(value, sort_keys=True, ensure_ascii=False)


def write_cache(path: Path, bundle: GraphBundle, manifest: BuildManifest) -> None:
    """Materialize ``bundle`` and ``manifest`` into a fresh database at ``path``.

    The database is created from scratch, FTS5 is verified, and every insert runs
    inside a single explicit transaction (committed atomically or rolled back).
    All SQL is parameterized.
    """
    path = Path(path)
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        verify_fts5(conn)
        create_schema(conn)
        with conn:  # explicit transaction: commit on success, rollback on error
            for key, value in manifest.to_metadata().items():
                conn.execute(
                    "INSERT INTO kb_metadata(key, value_json) VALUES (?, ?)",
                    (key, _metadata_value(value)),
                )
            for entity in bundle.entities:
                conn.execute(
                    "INSERT INTO entities(entity_id, kind, subtype, canonical_name, "
                    "title, summary, authority, source_path, source_anchor, "
                    "is_current, attributes_json) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        entity.entity_id,
                        entity.kind.value,
                        entity.subtype,
                        entity.canonical_name,
                        entity.title,
                        entity.summary,
                        entity.authority.value,
                        entity.source_path,
                        entity.source_anchor,
                        1 if entity.is_current else 0,
                        json.dumps(
                            dict(entity.attributes),
                            sort_keys=True,
                            ensure_ascii=False,
                        ),
                    ),
                )
            for alias in bundle.aliases:
                conn.execute(
                    "INSERT INTO aliases(alias, entity_id, alias_type, priority) "
                    "VALUES (?,?,?,?)",
                    (alias.alias, alias.entity_id, alias.alias_type, alias.priority),
                )
            for entity in bundle.entities:
                for facet_key, facet_value in _entity_facets(entity):
                    conn.execute(
                        "INSERT INTO facets(entity_id, facet_key, facet_value) "
                        "VALUES (?,?,?)",
                        (entity.entity_id, facet_key, facet_value),
                    )
            for edge in bundle.edges:
                conn.execute(
                    "INSERT INTO edges(edge_id, source_id, predicate, target_id, "
                    "target_raw, target_anchor, resolved, source_location) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (
                        _edge_id(edge),
                        edge.source_id,
                        edge.predicate,
                        edge.target_id,
                        edge.target_raw,
                        edge.target_anchor,
                        1 if edge.resolved else 0,
                        edge.source_location,
                    ),
                )
            for entity in bundle.entities:
                conn.execute(
                    "INSERT INTO documents(entity_id, body) VALUES (?,?)",
                    (entity.entity_id, entity.body),
                )
            for entity in bundle.entities:
                for section in entity.sections:
                    conn.execute(
                        "INSERT INTO sections(entity_id, section_key, heading, body, "
                        "ordinal) VALUES (?,?,?,?,?)",
                        (
                            entity.entity_id,
                            section.key,
                            section.heading,
                            section.body,
                            section.ordinal,
                        ),
                    )
            for entity in bundle.entities:
                conn.execute(
                    "INSERT INTO entities_fts(entity_id, canonical_name, title, "
                    "summary, tags, body) VALUES (?,?,?,?,?,?)",
                    (
                        entity.entity_id,
                        entity.canonical_name,
                        entity.title,
                        entity.summary,
                        _tags_text(entity),
                        entity.body,
                    ),
                )
    finally:
        conn.close()


def _metadata(conn: sqlite3.Connection, key: str) -> object | None:
    row = conn.execute(
        "SELECT value_json FROM kb_metadata WHERE key=?", (key,)
    ).fetchone()
    return json.loads(row[0]) if row is not None else None


def validate_cache(path: Path) -> None:
    """Run the build self-checks against the cache at ``path``.

    Raises :class:`CacheIntegrityError` if the schema version is wrong, the
    integrity or foreign-key checks fail, the recorded entity/edge counts do not
    match the rows, or any resolved edge dangles.
    """
    path = Path(path)
    if not path.is_file():
        raise CacheIntegrityError("cache file not found")
    conn = sqlite3.connect(str(path))
    try:
        conn.execute("PRAGMA foreign_keys = ON")

        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )
        }
        if not {
            "kb_metadata", "entities", "aliases", "facets", "edges",
            "documents", "sections", "entities_fts",
        } <= tables:
            raise CacheIntegrityError("cache is missing required tables")

        version = _metadata(conn, "kb_schema_version")
        if version is None:
            raise CacheIntegrityError("missing kb_schema_version metadata")
        try:
            version_int = int(version)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise CacheIntegrityError("invalid kb_schema_version metadata") from exc
        if version_int != KB_SCHEMA_VERSION:
            raise CacheIntegrityError(f"schema version mismatch: {version_int}")

        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            raise CacheIntegrityError("integrity_check failed")
        if conn.execute("PRAGMA foreign_key_check").fetchall():
            raise CacheIntegrityError("foreign_key_check reported violations")

        entity_counts = _metadata(conn, "entity_count_by_kind")
        if not isinstance(entity_counts, dict):
            raise CacheIntegrityError("missing or invalid entity_count_by_kind")
        for kind, count in entity_counts.items():
            actual = conn.execute(
                "SELECT COUNT(*) FROM entities WHERE kind=?", (str(kind),)
            ).fetchone()[0]
            if actual != int(count):
                raise CacheIntegrityError(
                    f"entity count mismatch for {kind}: {actual} != {count}"
                )

        edge_counts = _metadata(conn, "edge_count_by_predicate")
        if isinstance(edge_counts, dict):
            for predicate, count in edge_counts.items():
                actual = conn.execute(
                    "SELECT COUNT(*) FROM edges WHERE predicate=?",
                    (str(predicate),),
                ).fetchone()[0]
                if actual != int(count):
                    raise CacheIntegrityError(
                        f"edge count mismatch for {predicate}: {actual} != {count}"
                    )

        dangling = conn.execute(
            "SELECT edge_id FROM edges WHERE resolved=1 AND target_id IS NOT NULL "
            "AND target_id NOT IN (SELECT entity_id FROM entities)"
        ).fetchall()
        if dangling:
            raise CacheIntegrityError(
                f"{len(dangling)} resolved edge(s) point to an unknown entity"
            )
    finally:
        conn.close()
