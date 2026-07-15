"""Read-only SQLite primitives over the knowledge cache.

``KnowledgeStore.open`` attaches the cache with a ``mode=ro`` URI and
``query_only=ON`` so the database cannot be mutated through the store. Every
query is parameterized; free-text queries are built only from quoted tokens
matching ``[A-Za-z0-9_:.]+`` and joined with ``OR``, so user input never reaches
SQL syntax or identifiers.
"""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

__all__ = [
    "AliasCandidate",
    "EntityRow",
    "KnowledgeStore",
    "NeighborRow",
    "SectionRow",
    "build_fts_match",
]

# A safe FTS token: alphanumerics plus ``_``, ``:`` and ``.``. Everything else
# is a separator. Quoted tokens can never contain a double quote, so wrapping
# each token in double quotes is injection-safe for the FTS MATCH expression.
_FTS_TOKEN_RE = re.compile(r"[A-Za-z0-9_:.]+")


def build_fts_match(query: str) -> str | None:
    """Build a safe FTS5 MATCH expression from ``query``.

    Tokens matching ``[A-Za-z0-9_:.]+`` are extracted, double-quoted and
    ``OR``-joined. Returns ``None`` when there are no usable tokens. The result
    is intended to be passed as a bound parameter to ``MATCH ?``.
    """
    tokens = _FTS_TOKEN_RE.findall(query or "")
    if not tokens:
        return None
    return " OR ".join(f'"{token}"' for token in tokens)


@dataclass(frozen=True)
class AliasCandidate:
    entity_id: str
    alias_type: str
    priority: int


@dataclass(frozen=True)
class EntityRow:
    entity_id: str
    kind: str
    subtype: str
    canonical_name: str
    title: str
    summary: str
    authority: str
    source_path: str
    source_anchor: str | None
    is_current: bool
    attributes: dict


@dataclass(frozen=True)
class SectionRow:
    entity_id: str
    section_key: str
    heading: str
    body: str
    ordinal: int


@dataclass(frozen=True)
class NeighborRow:
    predicate: str
    entity_id: str | None
    target_raw: str | None
    target_anchor: str | None
    resolved: bool
    source_location: str


class KnowledgeStore:
    """A read-only view over one knowledge cache."""

    def __init__(self, connection: sqlite3.Connection) -> None:
        self.connection = connection

    @classmethod
    def open(cls, path: Path | str) -> "KnowledgeStore":
        """Open the cache at ``path`` read-only (URI ``mode=ro``)."""
        uri = Path(path).resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only = ON")
        return cls(connection)

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "KnowledgeStore":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # --- metadata ----------------------------------------------------------

    def metadata_value(self, key: str):
        row = self.connection.execute(
            "SELECT value_json FROM kb_metadata WHERE key=?", (key,)
        ).fetchone()
        return json.loads(row[0]) if row is not None else None

    def metadata(self) -> dict:
        return {
            row["key"]: json.loads(row["value_json"])
            for row in self.connection.execute(
                "SELECT key, value_json FROM kb_metadata"
            )
        }

    # --- aliases -----------------------------------------------------------

    def alias_candidates(self, alias: str) -> list[AliasCandidate]:
        """Return the alias rows for ``alias`` at the maximum priority.

        Only rows whose priority equals the maximum priority for the alias are
        returned (lower-priority aliases are excluded), in stable order.
        """
        rows = self.connection.execute(
            "SELECT entity_id, alias_type, priority FROM aliases WHERE alias=? "
            "ORDER BY priority DESC, entity_id, alias_type",
            (alias,),
        ).fetchall()
        if not rows:
            return []
        max_priority = rows[0]["priority"]
        return [
            AliasCandidate(
                entity_id=row["entity_id"],
                alias_type=row["alias_type"],
                priority=row["priority"],
            )
            for row in rows
            if row["priority"] == max_priority
        ]

    # --- entities ----------------------------------------------------------

    def entity(self, entity_id: str) -> EntityRow | None:
        row = self.connection.execute(
            "SELECT entity_id, kind, subtype, canonical_name, title, summary, "
            "authority, source_path, source_anchor, is_current, attributes_json "
            "FROM entities WHERE entity_id=?",
            (entity_id,),
        ).fetchone()
        if row is None:
            return None
        return EntityRow(
            entity_id=row["entity_id"],
            kind=row["kind"],
            subtype=row["subtype"],
            canonical_name=row["canonical_name"],
            title=row["title"],
            summary=row["summary"],
            authority=row["authority"],
            source_path=row["source_path"],
            source_anchor=row["source_anchor"],
            is_current=bool(row["is_current"]),
            attributes=json.loads(row["attributes_json"]),
        )

    def entities(self, entity_ids: Iterable[str]) -> list[EntityRow]:
        ids = tuple(entity_ids)
        if not ids:
            return []
        placeholders = ",".join("?" for _ in ids)
        rows = self.connection.execute(
            "SELECT entity_id, kind, subtype, canonical_name, title, summary, "
            "authority, source_path, source_anchor, is_current, attributes_json "
            f"FROM entities WHERE entity_id IN ({placeholders})",
            ids,
        ).fetchall()
        return [
            EntityRow(
                entity_id=row["entity_id"],
                kind=row["kind"],
                subtype=row["subtype"],
                canonical_name=row["canonical_name"],
                title=row["title"],
                summary=row["summary"],
                authority=row["authority"],
                source_path=row["source_path"],
                source_anchor=row["source_anchor"],
                is_current=bool(row["is_current"]),
                attributes=json.loads(row["attributes_json"]),
            )
            for row in rows
        ]

    # --- facets ------------------------------------------------------------

    def entities_by_facet(self, facet_key: str, facet_value: str) -> list[str]:
        return [
            row["entity_id"]
            for row in self.connection.execute(
                "SELECT entity_id FROM facets WHERE facet_key=? AND facet_value=? "
                "ORDER BY entity_id",
                (facet_key, facet_value),
            )
        ]

    def facets_for_entity(self, entity_id: str) -> list[tuple[str, str]]:
        return [
            (row["facet_key"], row["facet_value"])
            for row in self.connection.execute(
                "SELECT facet_key, facet_value FROM facets WHERE entity_id=? "
                "ORDER BY facet_key, facet_value",
                (entity_id,),
            )
        ]

    # --- FTS ---------------------------------------------------------------

    def fts_candidates(self, query: str, limit: int) -> list[str]:
        match = build_fts_match(query)
        if match is None:
            return []
        rows = self.connection.execute(
            "SELECT entity_id FROM entities_fts WHERE entities_fts MATCH ? "
            "ORDER BY rank LIMIT ?",
            (match, limit),
        ).fetchall()
        return [row["entity_id"] for row in rows]

    # --- documents / sections ----------------------------------------------

    def document(self, entity_id: str) -> str | None:
        row = self.connection.execute(
            "SELECT body FROM documents WHERE entity_id=?", (entity_id,)
        ).fetchone()
        return row["body"] if row is not None else None

    def sections(self, entity_id: str) -> list[SectionRow]:
        rows = self.connection.execute(
            "SELECT section_key, heading, body, ordinal FROM sections "
            "WHERE entity_id=? ORDER BY ordinal",
            (entity_id,),
        ).fetchall()
        return [
            SectionRow(
                entity_id=entity_id,
                section_key=row["section_key"],
                heading=row["heading"],
                body=row["body"],
                ordinal=row["ordinal"],
            )
            for row in rows
        ]

    def section(self, entity_id: str, section_key: str) -> list[SectionRow]:
        rows = self.connection.execute(
            "SELECT section_key, heading, body, ordinal FROM sections "
            "WHERE entity_id=? AND section_key=? ORDER BY ordinal",
            (entity_id, section_key),
        ).fetchall()
        return [
            SectionRow(
                entity_id=entity_id,
                section_key=row["section_key"],
                heading=row["heading"],
                body=row["body"],
                ordinal=row["ordinal"],
            )
            for row in rows
        ]

    # --- neighbors ---------------------------------------------------------

    def neighbors_outgoing(
        self, entity_id: str, predicate: str | None, limit: int
    ) -> list[NeighborRow]:
        """Edges leaving ``entity_id`` (resolved target_id or unresolved raw)."""
        return self._neighbors(
            "SELECT predicate, target_id, target_raw, target_anchor, resolved, "
            "source_location FROM edges WHERE source_id=?",
            entity_id,
            predicate,
            limit,
        )

    def neighbors_incoming(
        self, entity_id: str, predicate: str | None, limit: int
    ) -> list[NeighborRow]:
        """Resolved edges pointing at ``entity_id`` (the source is the neighbor)."""
        return self._neighbors(
            "SELECT predicate, source_id, target_raw, target_anchor, resolved, "
            "source_location FROM edges WHERE target_id=?",
            entity_id,
            predicate,
            limit,
        )

    def _neighbors(
        self, sql: str, entity_id: str, predicate: str | None, limit: int
    ) -> list[NeighborRow]:
        params: list[object] = [entity_id]
        if predicate is not None:
            sql += " AND predicate=?"
            params.append(predicate)
        sql += " ORDER BY predicate, source_location LIMIT ?"
        params.append(limit)
        rows = self.connection.execute(sql, tuple(params)).fetchall()
        return [
            NeighborRow(
                predicate=row["predicate"],
                entity_id=row["target_id"] if "target_id" in row.keys() else row["source_id"],
                target_raw=row["target_raw"],
                target_anchor=row["target_anchor"],
                resolved=bool(row["resolved"]),
                source_location=row["source_location"],
            )
            for row in rows
        ]
