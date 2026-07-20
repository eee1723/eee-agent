"""Tests for the SQLite schema, deterministic writer and cache validation.

A small synthetic corpus is parsed with the Stage 2 parsers, assembled into a
GraphBundle, materialized with a BuildManifest, and inspected directly through
sqlite3. No real Houdini installation or HFS is required.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from pathlib import Path

import pytest

from eee_agent.knowledge.graph import assemble_graph
from eee_agent.knowledge.manifest import (
    build_manifest,
    fingerprint_bytes,
    hash_inventory,
)
from eee_agent.knowledge.models import (
    AliasDraft,
    Authority,
    EntityKind,
    GraphBundle,
)
from eee_agent.knowledge.parse_hom import parse_hom_document
from eee_agent.knowledge.parse_node import parse_node_document
from eee_agent.knowledge.parse_vex import parse_vex_document
from eee_agent.knowledge.schema import KB_SCHEMA_VERSION, create_schema, verify_fts5
from eee_agent.knowledge.writer import (
    CacheIntegrityError,
    validate_cache,
    write_cache,
)

NODE_FIXTURE = (
    "#type: node\n#context: sop\n#tags: model, polygons\n#internal: boolean\n"
    "= Boolean =\n\"\"\"Boolean op.\"\"\"\n"
    "== Overview == (overview)\nSee [Vex:intersect].\n"
)
VEX_FIXTURE = (
    "#type: vex\n#context: sop\n#group: geometry\n#tags: intersect, ray\n"
    "= intersect =\n\"\"\"Intersect a ray.\"\"\"\n"
    ":usage: intersect(geo) -> int\n@related intersect\n"
)
HOM_FIXTURE = (
    "= hou.Node =\n#type: homclass\n#superclass: hou.NodeReferenceCounted\n"
    "\"\"\"Node class.\"\"\"\n"
    "::`createNode(self, type_name)` -> [Hom:hou.Node]:\n"
    "    #cppname: HOM_Node::createNode\n    Creates a node.\n"
)


def _bundle() -> GraphBundle:
    docs = (
        parse_node_document("sop/boolean.txt", NODE_FIXTURE),
        parse_vex_document("functions/intersect.txt", VEX_FIXTURE),
        parse_hom_document("hou/Node.txt", HOM_FIXTURE),
    )
    return assemble_graph(docs, inventory=frozenset({"boolean"}))


def _manifest(bundle: GraphBundle):
    entity_counts = Counter(e.kind.value for e in bundle.entities)
    edge_counts = Counter(e.predicate for e in bundle.edges)
    unresolved = sum(1 for e in bundle.edges if not e.resolved)
    return build_manifest(
        kb_schema_version=KB_SCHEMA_VERSION,
        builder_version="b1",
        parser_version="p1",
        created_at_utc="2026-07-15T00:00:00Z",
        houdini_version="21.0.440",
        houdini_build="21.0.440",
        source_archives=(fingerprint_bytes("nodes.zip", b"node-bytes"),),
        skill_sources=(fingerprint_bytes("skills/vex-patterns/SKILL.md", b"skill"),),
        node_inventory_sha256=hash_inventory(frozenset({"boolean"})),
        entity_count_by_kind=entity_counts,
        edge_count_by_predicate=edge_counts,
        unresolved_reference_count=unresolved,
        ambiguous_alias_count=0,
    )


def write_fixture_cache(tmp_path: Path) -> Path:
    bundle = _bundle()
    path = tmp_path / "knowledge.sqlite3"
    write_cache(path, bundle, _manifest(bundle))
    return path


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(path)


# --- schema ---------------------------------------------------------------

def test_schema_version_is_one() -> None:
    assert KB_SCHEMA_VERSION == 1


def test_schema_contains_graph_facets_and_fts(tmp_path: Path) -> None:
    path = write_fixture_cache(tmp_path)
    with _connect(path) as conn:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type IN ('table','index')"
            )
        }
    assert {
        "entities", "aliases", "facets", "edges", "documents",
        "sections", "entities_fts",
    } <= names


def test_schema_has_required_indexes(tmp_path: Path) -> None:
    path = write_fixture_cache(tmp_path)
    with _connect(path) as conn:
        names = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index'"
            )
        }
    # alias exact + type, facet key/value, entity kind/subtype/current,
    # edge source/predicate + target/predicate, section entity/key.
    assert any("alias" in n.lower() for n in names)
    assert any("facet" in n.lower() for n in names)
    assert any("entit" in n.lower() for n in names)
    assert any("edge" in n.lower() for n in names)
    assert any("section" in n.lower() for n in names)


def test_foreign_keys_enforced(tmp_path: Path) -> None:
    path = write_fixture_cache(tmp_path)
    with _connect(path) as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO aliases(alias, entity_id, alias_type, priority) "
                "VALUES ('x', 'no::such::entity', 't', 1)"
            )


def test_fts5_available(tmp_path: Path) -> None:
    path = write_fixture_cache(tmp_path)
    with _connect(path) as conn:
        assert verify_fts5(conn) is None


# --- writer rows ----------------------------------------------------------

def test_writer_persists_all_entity_rows(tmp_path: Path) -> None:
    bundle = _bundle()
    path = write_fixture_cache(tmp_path)
    with _connect(path) as conn:
        count = conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0]
    assert count == len(bundle.entities)


def test_writer_persists_aliases_facets_edges_documents_sections(tmp_path: Path) -> None:
    bundle = _bundle()
    path = write_fixture_cache(tmp_path)
    with _connect(path) as conn:
        aliases = conn.execute("SELECT COUNT(*) FROM aliases").fetchone()[0]
        facets = conn.execute("SELECT COUNT(*) FROM facets").fetchone()[0]
        edges = conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0]
        docs = conn.execute("SELECT COUNT(*) FROM documents").fetchone()[0]
        sections = conn.execute("SELECT COUNT(*) FROM sections").fetchone()[0]
    assert aliases == len(bundle.aliases)
    assert edges == len(bundle.edges)
    assert docs == len(bundle.entities)
    assert facets > 0  # context/group/tag/superclass/operator_status extracted
    assert sections > 0


def test_writer_extracts_facets_from_attributes(tmp_path: Path) -> None:
    path = write_fixture_cache(tmp_path)
    with _connect(path) as conn:
        facet_pairs = {
            (row[0], row[1])
            for row in conn.execute(
                "SELECT facet_key, facet_value FROM facets "
                "WHERE entity_id='node_document:sop/boolean.txt@current'"
            )
        }
    assert ("context", "sop") in facet_pairs
    assert ("tag", "model") in facet_pairs
    assert ("operator_status", "verified_at_build") in facet_pairs


def test_writer_stores_complete_attributes_json(tmp_path: Path) -> None:
    path = write_fixture_cache(tmp_path)
    with _connect(path) as conn:
        row = conn.execute(
            "SELECT attributes_json FROM entities "
            "WHERE entity_id='node_document:sop/boolean.txt@current'"
        ).fetchone()
    attrs = json.loads(row[0])
    # operator_type_status enum serializes to its string value.
    assert attrs["operator_type_status"] == "verified_at_build"
    assert attrs["operator_type"] == "boolean"
    assert attrs["operator_type_candidates"] == ["boolean"]
    assert attrs["tags"] == ["model", "polygons"]


def test_writer_persists_body_and_sections(tmp_path: Path) -> None:
    path = write_fixture_cache(tmp_path)
    with _connect(path) as conn:
        body = conn.execute(
            "SELECT body FROM documents WHERE entity_id='node_document:sop/boolean.txt@current'"
        ).fetchone()[0]
        secs = conn.execute(
            "SELECT section_key, heading, ordinal FROM sections "
            "WHERE entity_id='node_document:sop/boolean.txt@current'"
        ).fetchall()
    assert "Boolean op." in body
    assert ("overview", "Overview", 0) in secs


def test_writer_populates_fts(tmp_path: Path) -> None:
    path = write_fixture_cache(tmp_path)
    with _connect(path) as conn:
        hits = {
            row[0]
            for row in conn.execute(
                "SELECT entity_id FROM entities_fts WHERE entities_fts MATCH 'boolean'"
            )
        }
    assert "node_document:sop/boolean.txt@current" in hits


def test_edge_id_is_deterministic_sha256(tmp_path: Path) -> None:
    bundle = _bundle()
    p1 = tmp_path / "a.sqlite3"
    p2 = tmp_path / "b.sqlite3"
    write_cache(p1, bundle, _manifest(bundle))
    write_cache(p2, bundle, _manifest(bundle))
    with _connect(p1) as c1, _connect(p2) as c2:
        ids1 = {r[0] for r in c1.execute("SELECT edge_id FROM edges")}
        ids2 = {r[0] for r in c2.execute("SELECT edge_id FROM edges")}
    assert ids1 == ids2
    assert all(len(eid) == 64 for eid in ids1)
    assert all(all(ch in "0123456789abcdef" for ch in eid) for eid in ids1)


def test_writer_uses_parameterized_sql_safe_for_quotes(tmp_path: Path) -> None:
    # A quote in data must be stored verbatim (no string interpolation in SQL).
    from eee_agent.knowledge.models import EntityDraft
    e = EntityDraft(
        entity_id="vex_function:quoted", kind=EntityKind.VEX_FUNCTION, subtype="",
        canonical_name="it's a 'name'", title="It's", summary="",
        authority=Authority.OFFICIAL_HOUDINI_DOCS, source_path="functions/q.txt",
        source_anchor=None, is_current=True, attributes={"context": "sop"},
        body="body with 'quotes'", sections=(),
    )
    a = AliasDraft(alias="it's", entity_id="vex_function:quoted",
                   alias_type="qualified_name", priority=100)
    bundle = GraphBundle((e,), (a,), ())
    path = tmp_path / "q.sqlite3"
    write_cache(path, bundle, _manifest(bundle))
    with _connect(path) as conn:
        canon = conn.execute(
            "SELECT canonical_name FROM entities WHERE entity_id='vex_function:quoted'"
        ).fetchone()[0]
        alias = conn.execute(
            "SELECT alias FROM aliases WHERE alias_type='qualified_name'"
        ).fetchone()[0]
    assert canon == "it's a 'name'"
    assert alias == "it's"


def test_writer_metadata_records_manifest(tmp_path: Path) -> None:
    manifest = _manifest(_bundle())
    path = write_fixture_cache(tmp_path)
    with _connect(path) as conn:
        keys = {
            row[0]
            for row in conn.execute("SELECT key FROM kb_metadata")
        }
        sha = json.loads(
            conn.execute(
                "SELECT value_json FROM kb_metadata WHERE key='manifest_sha256'"
            ).fetchone()[0]
        )
        hv = json.loads(
            conn.execute(
                "SELECT value_json FROM kb_metadata WHERE key='houdini_build'"
            ).fetchone()[0]
        )
    assert {
        "kb_schema_version", "manifest_sha256", "houdini_version",
        "houdini_build", "node_inventory_sha256", "entity_count_by_kind",
        "source_archives", "skill_sources",
    } <= keys
    assert sha == manifest.manifest_sha256
    assert hv == "21.0.440"


# --- validation -----------------------------------------------------------

def test_validate_cache_passes_on_valid(tmp_path: Path) -> None:
    path = write_fixture_cache(tmp_path)
    validate_cache(path)  # must not raise


def test_valid_cache_integrity_check_ok(tmp_path: Path) -> None:
    path = write_fixture_cache(tmp_path)
    with _connect(path) as conn:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_validate_raises_on_empty_or_missing_schema(tmp_path: Path) -> None:
    path = tmp_path / "empty.sqlite3"
    sqlite3.connect(path).close()  # creates an empty db
    with pytest.raises(CacheIntegrityError):
        validate_cache(path)


def test_validate_raises_on_wrong_schema_version(tmp_path: Path) -> None:
    path = write_fixture_cache(tmp_path)
    with _connect(path) as conn:
        conn.execute(
            "UPDATE kb_metadata SET value_json='99' WHERE key='kb_schema_version'"
        )
        conn.commit()
    with pytest.raises(CacheIntegrityError):
        validate_cache(path)


def test_validate_raises_on_entity_count_mismatch(tmp_path: Path) -> None:
    path = write_fixture_cache(tmp_path)
    with _connect(path) as conn:
        conn.execute("DELETE FROM entities WHERE entity_id='vex_function:intersect'")
        conn.commit()
    with pytest.raises(CacheIntegrityError):
        validate_cache(path)


def test_validate_raises_on_dangling_resolved_edge(tmp_path: Path) -> None:
    path = write_fixture_cache(tmp_path)
    with _connect(path) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "INSERT INTO edges(edge_id, source_id, predicate, target_id, target_raw, "
            "target_anchor, resolved, source_location) "
            "VALUES ('deadbeef', 'vex_function:intersect', 'references', "
            "'no::such::target', 'Vex:nope', NULL, 1, 'functions/intersect.txt:9')"
        )
        conn.execute("PRAGMA foreign_keys = ON")
        conn.commit()
    with pytest.raises(CacheIntegrityError):
        validate_cache(path)


def test_validate_raises_on_foreign_key_violation(tmp_path: Path) -> None:
    path = write_fixture_cache(tmp_path)
    with _connect(path) as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        conn.execute(
            "INSERT INTO aliases(alias, entity_id, alias_type, priority) "
            "VALUES ('orphan', 'no::such::entity', 't', 1)"
        )
        conn.commit()
    with pytest.raises(CacheIntegrityError):
        validate_cache(path)


# --- hardened self-checks -------------------------------------------------

def _set_metadata(path: Path, key: str, value_json: str) -> None:
    with _connect(path) as conn:
        conn.execute(
            "UPDATE kb_metadata SET value_json=? WHERE key=?", (value_json, key)
        )
        conn.commit()


def _delete_metadata(path: Path, key: str) -> None:
    with _connect(path) as conn:
        conn.execute("DELETE FROM kb_metadata WHERE key=?", (key,))
        conn.commit()


def test_validate_raises_when_edge_count_metadata_missing(tmp_path: Path) -> None:
    path = write_fixture_cache(tmp_path)
    _delete_metadata(path, "edge_count_by_predicate")
    with pytest.raises(CacheIntegrityError):
        validate_cache(path)


def test_validate_raises_when_edge_count_empty(tmp_path: Path) -> None:
    path = write_fixture_cache(tmp_path)
    _set_metadata(path, "edge_count_by_predicate", "{}")
    with pytest.raises(CacheIntegrityError):
        validate_cache(path)


def test_validate_raises_when_edge_count_not_object(tmp_path: Path) -> None:
    path = write_fixture_cache(tmp_path)
    _set_metadata(path, "edge_count_by_predicate", "[]")
    with pytest.raises(CacheIntegrityError):
        validate_cache(path)


def test_validate_raises_when_edge_count_invalid_json(tmp_path: Path) -> None:
    path = write_fixture_cache(tmp_path)
    _set_metadata(path, "edge_count_by_predicate", "not json")
    with pytest.raises(CacheIntegrityError):
        validate_cache(path)


def test_validate_raises_when_entity_count_empty(tmp_path: Path) -> None:
    path = write_fixture_cache(tmp_path)
    _set_metadata(path, "entity_count_by_kind", "{}")
    with pytest.raises(CacheIntegrityError):
        validate_cache(path)


def test_validate_raises_on_resolved_edge_with_null_target(tmp_path: Path) -> None:
    path = write_fixture_cache(tmp_path)
    with _connect(path) as conn:
        conn.execute("UPDATE edges SET target_id=NULL WHERE resolved=1")
        conn.commit()
    with pytest.raises(CacheIntegrityError):
        validate_cache(path)


def test_validate_raises_on_unresolved_edge_with_target(tmp_path: Path) -> None:
    path = write_fixture_cache(tmp_path)
    with _connect(path) as conn:
        conn.execute(
            "UPDATE edges SET target_id='vex_function:intersect' WHERE resolved=0"
        )
        conn.commit()
    with pytest.raises(CacheIntegrityError):
        validate_cache(path)


def test_validate_raises_on_invalid_resolved_value(tmp_path: Path) -> None:
    path = write_fixture_cache(tmp_path)
    with _connect(path) as conn:
        conn.execute("UPDATE edges SET resolved=2 WHERE resolved=0")
        conn.commit()
    with pytest.raises(CacheIntegrityError):
        validate_cache(path)


def test_writer_rejects_invalid_graph_bundle(tmp_path: Path) -> None:
    from eee_agent.knowledge.graph import GraphError
    from eee_agent.knowledge.models import EdgeDraft, EntityDraft

    entity = EntityDraft(
        entity_id="vex_function:x", kind=EntityKind.VEX_FUNCTION, subtype="",
        canonical_name="x", title="X", summary="",
        authority=Authority.OFFICIAL_HOUDINI_DOCS, source_path="functions/x.txt",
        source_anchor=None, is_current=True, attributes={}, body="", sections=(),
    )
    # A resolved edge with no target violates a graph invariant.
    bad_edge = EdgeDraft(
        source_id="vex_function:x", predicate="references", target_id=None,
        target_raw="Vex:ghost", target_anchor=None, resolved=True,
        source_location="functions/x.txt:1",
    )
    bundle = GraphBundle((entity,), (), (bad_edge,))
    with pytest.raises(GraphError):
        write_cache(tmp_path / "bad.sqlite3", bundle, _manifest(bundle))
