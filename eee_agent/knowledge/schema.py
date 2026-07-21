"""SQLite schema (version 1) for the Houdini knowledge cache.

Pure DDL: ``create_schema`` builds the entities/aliases/facets/edges/documents/
sections tables, the ``entities_fts`` FTS5 table and the structural indexes from
design §8.1. ``verify_fts5`` confirms the FTS5 extension is compiled in. No data
is written here and no queries are answered here.
"""

from __future__ import annotations

import sqlite3

__all__ = ["Fts5Unavailable", "KB_SCHEMA_VERSION", "create_schema", "verify_fts5"]

KB_SCHEMA_VERSION = 1


class Fts5Unavailable(RuntimeError):
    """The SQLite build does not have FTS5 compiled in."""


def verify_fts5(conn: sqlite3.Connection) -> None:
    """Raise :class:`Fts5Unavailable` unless FTS5 is compiled into SQLite."""
    rows = conn.execute("PRAGMA compile_options").fetchall()
    if not any("ENABLE_FTS5" in str(row[0]) for row in rows):
        raise Fts5Unavailable("SQLite was not compiled with ENABLE_FTS5")


def create_schema(conn: sqlite3.Connection) -> None:
    """Create all version-1 tables, constraints and indexes on ``conn``.

    Idempotent for a fresh connection; the builder always starts from an empty
    temp database. Foreign keys are enabled and a single explicit transaction
    holds every DDL statement.
    """
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(
        """
        CREATE TABLE kb_metadata (
            key        TEXT PRIMARY KEY,
            value_json TEXT NOT NULL
        );

        CREATE TABLE entities (
            entity_id       TEXT PRIMARY KEY,
            kind            TEXT NOT NULL,
            subtype         TEXT NOT NULL,
            canonical_name  TEXT NOT NULL,
            title           TEXT NOT NULL,
            summary         TEXT NOT NULL,
            authority       TEXT NOT NULL,
            source_path     TEXT NOT NULL,
            source_anchor   TEXT,
            is_current      INTEGER NOT NULL,
            attributes_json TEXT NOT NULL
        );

        CREATE TABLE aliases (
            alias      TEXT NOT NULL,
            entity_id  TEXT NOT NULL,
            alias_type TEXT NOT NULL,
            priority   INTEGER NOT NULL,
            UNIQUE (alias, entity_id, alias_type),
            FOREIGN KEY (entity_id) REFERENCES entities(entity_id)
        );

        CREATE TABLE facets (
            entity_id   TEXT NOT NULL,
            facet_key   TEXT NOT NULL,
            facet_value TEXT NOT NULL,
            UNIQUE (entity_id, facet_key, facet_value),
            FOREIGN KEY (entity_id) REFERENCES entities(entity_id)
        );

        CREATE TABLE edges (
            edge_id         TEXT PRIMARY KEY,
            source_id       TEXT NOT NULL,
            predicate       TEXT NOT NULL,
            target_id       TEXT,
            target_raw      TEXT,
            target_anchor   TEXT,
            resolved        INTEGER NOT NULL,
            source_location TEXT NOT NULL,
            FOREIGN KEY (source_id) REFERENCES entities(entity_id),
            FOREIGN KEY (target_id) REFERENCES entities(entity_id)
        );

        CREATE TABLE documents (
            entity_id TEXT PRIMARY KEY,
            body      TEXT NOT NULL,
            FOREIGN KEY (entity_id) REFERENCES entities(entity_id)
        );

        CREATE TABLE sections (
            entity_id   TEXT NOT NULL,
            section_key TEXT NOT NULL,
            heading     TEXT NOT NULL,
            body        TEXT NOT NULL,
            ordinal     INTEGER NOT NULL,
            UNIQUE (entity_id, section_key, ordinal),
            FOREIGN KEY (entity_id) REFERENCES entities(entity_id)
        );

        CREATE VIRTUAL TABLE entities_fts USING fts5(
            entity_id UNINDEXED,
            canonical_name,
            title,
            summary,
            tags,
            body
        );
        """
    )
    conn.executescript(
        """
        CREATE INDEX idx_aliases_alias      ON aliases(alias);
        CREATE INDEX idx_aliases_type       ON aliases(alias_type);
        CREATE INDEX idx_aliases_alias_type ON aliases(alias, alias_type);
        CREATE INDEX idx_facets_key_value   ON facets(facet_key, facet_value);
        CREATE INDEX idx_facets_entity      ON facets(entity_id);
        CREATE INDEX idx_entities_kind_sub_cur ON entities(kind, subtype, is_current);
        CREATE INDEX idx_edges_src_pred     ON edges(source_id, predicate);
        CREATE INDEX idx_edges_tgt_pred     ON edges(target_id, predicate);
        CREATE INDEX idx_sections_entity_key ON sections(entity_id, section_key);
        """
    )
