"""Tests for the read-only knowledge-cache store primitives.

A synthetic corpus is parsed, assembled and materialized with the Stage 4
writer, then queried through KnowledgeStore. No real Houdini installation or
HFS is required.
"""

from __future__ import annotations

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
    Authority,
    EntityDraft,
    EntityKind,
    ParsedDocument,
)
from eee_agent.knowledge.parse_hom import parse_hom_document
from eee_agent.knowledge.parse_node import parse_node_document
from eee_agent.knowledge.parse_vex import parse_vex_document
from eee_agent.knowledge.store import KnowledgeStore, build_fts_match

BOOLEAN_NODE = (
    "#type: node\n#context: sop\n#tags: model, polygons\n#internal: boolean\n"
    "= Boolean =\n\"\"\"Boolean operation between solids.\"\"\"\n"
    "== Overview == (overview)\nSee [Vex:intersect].\n"
)
INTERSECT_VEX = (
    "#type: vex\n#context: sop\n#group: geometry\n#tags: intersect, ray\n"
    "= intersect =\n\"\"\"Intersect a ray with geometry.\"\"\"\n"
    ":usage: intersect(geo) -> int\nSee [Node:sop/boolean].\n"
)
HOM_NODE = (
    "= hou.Node =\n#type: homclass\n#superclass: hou.NodeReferenceCounted\n"
    "\"\"\"Node class.\"\"\"\n"
    "::`createNode(self, type_name)` -> [Hom:hou.Node]:\n    Creates a node.\n"
)
DUP_A = "#type: vex\n#context: sop\n= dup =\n\"\"\"First dup.\"\"\"\n:usage: dup(a) -> int\n"
DUP_B = "#type: vex\n#context: sop\n= dup =\n\"\"\"Second dup.\"\"\"\n:usage: dup(b) -> int\n"
HISTORICAL_NODE = (
    "#type: node\n#context: sop\n#version: 2.0\n= Agent Look At =\n\"\"\"Historical.\"\"\"\n"
)
LONG_BODY = "\n\n".join(f"Paragraph {i}: " + "filler " * 30 for i in range(200))

BOOLEAN_ID = "node_document:sop/boolean.txt@current"
INTERSECT_ID = "vex_function:intersect"


def _long_document() -> ParsedDocument:
    entity = EntityDraft(
        entity_id="vex_function:longbody", kind=EntityKind.VEX_FUNCTION, subtype="sop",
        canonical_name="longbody", title="longbody", summary="long body function",
        authority=Authority.OFFICIAL_HOUDINI_DOCS, source_path="functions/longbody.txt",
        source_anchor=None, is_current=True,
        attributes={"context": "sop", "signatures": (), "returns": ""},
        body=LONG_BODY, sections=(),
    )
    return ParsedDocument((entity,), (), ())


def _manifest(bundle):
    return build_manifest(
        kb_schema_version=1,
        builder_version="b1",
        parser_version="p1",
        created_at_utc="2026-07-15T00:00:00Z",
        houdini_version="21.0.440",
        houdini_build="21.0.440",
        source_archives=(fingerprint_bytes("nodes.zip", b"node-bytes"),),
        skill_sources=(),
        node_inventory_sha256=hash_inventory(frozenset({"boolean"})),
        entity_count_by_kind=Counter(e.kind.value for e in bundle.entities),
        edge_count_by_predicate=Counter(e.predicate for e in bundle.edges),
        unresolved_reference_count=sum(1 for e in bundle.edges if not e.resolved),
        ambiguous_alias_count=0,
    )


def build_fixture_cache(tmp_path: Path) -> Path:
    docs = (
        parse_node_document("sop/boolean.txt", BOOLEAN_NODE),
        parse_vex_document("functions/intersect.txt", INTERSECT_VEX),
        parse_hom_document("hou/Node.txt", HOM_NODE),
        parse_vex_document("functions/a.txt", DUP_A),
        parse_vex_document("functions/b.txt", DUP_B),
        parse_node_document("sop/agentlookat-2.0.txt", HISTORICAL_NODE),
        _long_document(),
    )
    bundle = assemble_graph(docs, inventory=frozenset({"boolean"}))
    path = tmp_path / "knowledge.sqlite3"
    from eee_agent.knowledge.writer import write_cache

    write_cache(path, bundle, _manifest(bundle))
    return path


@pytest.fixture
def cache_path(tmp_path: Path) -> Path:
    return build_fixture_cache(tmp_path)


# --- open / read-only -----------------------------------------------------

def test_store_open_missing_raises_in_mode_ro(tmp_path: Path) -> None:
    # mode=ro refuses to create a database that does not exist.
    with pytest.raises(sqlite3.OperationalError):
        KnowledgeStore.open(tmp_path / "missing.sqlite3")


def test_store_query_only_pragma_is_on(cache_path: Path) -> None:
    with KnowledgeStore.open(cache_path) as store:
        assert store.connection.execute("PRAGMA query_only").fetchone()[0] == 1


def test_store_rejects_mutations(cache_path: Path) -> None:
    with KnowledgeStore.open(cache_path) as store:
        with pytest.raises(sqlite3.OperationalError):
            store.connection.execute("DELETE FROM entities")


def test_store_row_factory_is_row(cache_path: Path) -> None:
    with KnowledgeStore.open(cache_path) as store:
        assert store.connection.row_factory is sqlite3.Row


def test_store_context_manager_closes(cache_path: Path) -> None:
    store = KnowledgeStore.open(cache_path)
    store.close()
    with pytest.raises(sqlite3.ProgrammingError):
        store.connection.execute("SELECT 1")


# --- metadata -------------------------------------------------------------

def test_store_metadata_read(cache_path: Path) -> None:
    with KnowledgeStore.open(cache_path) as store:
        assert store.metadata_value("kb_schema_version") == 1
        meta = store.metadata()
        assert meta["houdini_build"] == "21.0.440"
        assert meta["manifest_sha256"]


# --- aliases --------------------------------------------------------------

def test_store_alias_candidates_at_max_priority(cache_path: Path) -> None:
    with KnowledgeStore.open(cache_path) as store:
        cands = store.alias_candidates("boolean")
        assert cands
        assert {c.entity_id for c in cands} == {BOOLEAN_ID}
        # only maximum-priority rows (the verified operator alias, 200)
        assert all(c.priority == cands[0].priority for c in cands)
        assert cands[0].priority == 200


def test_store_alias_candidates_ambiguous(cache_path: Path) -> None:
    with KnowledgeStore.open(cache_path) as store:
        cands = store.alias_candidates("dup")
        assert {c.entity_id for c in cands} == {"vex_function:a", "vex_function:b"}
        assert all(c.priority == cands[0].priority for c in cands)


def test_store_alias_candidates_missing(cache_path: Path) -> None:
    with KnowledgeStore.open(cache_path) as store:
        assert store.alias_candidates("does-not-exist") == []


# --- entity / facets ------------------------------------------------------

def test_store_entity_lookup(cache_path: Path) -> None:
    with KnowledgeStore.open(cache_path) as store:
        entity = store.entity(BOOLEAN_ID)
        assert entity is not None
        assert entity.kind == "node_document"
        assert entity.canonical_name == "boolean"
        assert entity.attributes["operator_type"] == "boolean"
        assert entity.attributes["operator_type_status"] == "verified_at_build"
        assert entity.is_current is True
        assert store.entity("no::such::entity") is None


def test_store_facets(cache_path: Path) -> None:
    with KnowledgeStore.open(cache_path) as store:
        ids = set(store.entities_by_facet("context", "sop"))
        assert BOOLEAN_ID in ids
        pairs = store.facets_for_entity(BOOLEAN_ID)
        assert ("context", "sop") in pairs
        assert ("tag", "model") in pairs
        assert ("operator_status", "verified_at_build") in pairs


def test_store_facets_unknown(cache_path: Path) -> None:
    with KnowledgeStore.open(cache_path) as store:
        assert store.entities_by_facet("context", "nope") == []


# --- sections / documents -------------------------------------------------

def test_store_section_lookup(cache_path: Path) -> None:
    with KnowledgeStore.open(cache_path) as store:
        secs = store.sections(BOOLEAN_ID)
        assert "overview" in {s.section_key for s in secs}
        one = store.section(BOOLEAN_ID, "overview")
        assert len(one) == 1
        assert one[0].heading == "Overview"
        assert store.section(BOOLEAN_ID, "missing") == []


def test_store_document_lookup(cache_path: Path) -> None:
    with KnowledgeStore.open(cache_path) as store:
        body = store.document(BOOLEAN_ID)
        assert body is not None and "Boolean" in body
        assert store.document("no::such") is None


# --- neighbors ------------------------------------------------------------

def test_store_outgoing_neighbors(cache_path: Path) -> None:
    with KnowledgeStore.open(cache_path) as store:
        refs = store.neighbors_outgoing(BOOLEAN_ID, "references", 10)
        assert any(n.entity_id == INTERSECT_ID for n in refs)
        assert all(n.predicate == "references" for n in refs)
        all_out = store.neighbors_outgoing(BOOLEAN_ID, None, 10)
        assert all_out


def test_store_incoming_neighbors(cache_path: Path) -> None:
    with KnowledgeStore.open(cache_path) as store:
        inc = store.neighbors_incoming(BOOLEAN_ID, "references", 10)
        # intersect references boolean -> incoming neighbor.
        assert any(n.entity_id == INTERSECT_ID for n in inc)


def test_store_neighbors_predicate_filter(cache_path: Path) -> None:
    with KnowledgeStore.open(cache_path) as store:
        decls = store.neighbors_outgoing("hom_class:hou.Node", "declares_method", 10)
        assert any(n.entity_id == "hom_method:hou.Node#createNode" for n in decls)
        none = store.neighbors_outgoing("hom_class:hou.Node", "references", 10)
        assert all(n.predicate == "references" for n in none)


# --- FTS ------------------------------------------------------------------

def test_build_fts_match_safe_tokenization() -> None:
    assert build_fts_match("boolean") == '"boolean"'
    assert build_fts_match("ray intersect") == '"ray" OR "intersect"'
    assert build_fts_match("hou.Node") == '"hou.Node"'
    # punctuation is stripped to safe tokens.
    assert build_fts_match("foo; bar!") == '"foo" OR "bar"'
    # SQL-like input becomes quoted FTS tokens, never SQL syntax.
    dangerous = build_fts_match("x; DROP TABLE edges; --")
    assert dangerous == '"x" OR "DROP" OR "TABLE" OR "edges"'
    assert ";" not in dangerous
    assert build_fts_match("") is None
    assert build_fts_match("!!!") is None


def test_store_fts_candidates(cache_path: Path) -> None:
    with KnowledgeStore.open(cache_path) as store:
        ids = store.fts_candidates("boolean", 10)
        assert BOOLEAN_ID in ids


def test_store_fts_candidates_with_sql_like_input(cache_path: Path) -> None:
    with KnowledgeStore.open(cache_path) as store:
        # The SQL-like payload is tokenized into quoted FTS terms; the table is
        # untouched (mode=ro + query_only) and boolean is still found.
        ids = store.fts_candidates("boolean; DROP TABLE edges", 10)
        assert BOOLEAN_ID in ids
        # Proof no SQL was executed: the entity is still present.
        assert store.entity(BOOLEAN_ID) is not None


def test_store_fts_candidates_empty_query(cache_path: Path) -> None:
    with KnowledgeStore.open(cache_path) as store:
        assert store.fts_candidates("!!!", 10) == []
