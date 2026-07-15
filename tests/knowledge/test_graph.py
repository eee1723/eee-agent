"""Tests for knowledge-graph assembly, reconciliation and resolution.

Synthetic ParsedDocuments (built via the Stage 2 parsers or hand-crafted)
drive two-pass assembly. No real Houdini installation is required.
"""

from __future__ import annotations

import pytest

from eee_agent.knowledge.graph import GraphError, assemble_graph, validate_graph
from eee_agent.knowledge.models import (
    AliasDraft,
    Authority,
    EdgeDraft,
    EntityDraft,
    EntityKind,
    GraphBundle,
    OperatorTypeStatus,
    ParsedDocument,
)
from eee_agent.knowledge.parse_hom import parse_hom_document
from eee_agent.knowledge.parse_node import parse_node_document
from eee_agent.knowledge.parse_vex import parse_vex_document


BOOLEAN_FIXTURE = (
    "#type: node\n#context: sop\n#internal: boolean\n"
    "= Boolean =\n\"\"\"Boolean op.\"\"\"\n"
    "== See Also ==\n"
    "See [Node:sop/boolean], [Vex:intersect], [Hom:hou.Node] and [Hom:hou.Node#createNode].\n"
    ":include _common#geometry:\n"
)
APEX_FIXTURE = (
    "#type: node\n#context: sop\n#namespace: apex\n#internal: graph\n"
    "= APEX Build FK =\n\"\"\"Builds FK.\"\"\"\n"
)
POLYEXTRUDE_FIXTURE = "#type: node\n#context: sop\n= PolyExtrude =\n\"\"\"Extrude.\"\"\"\n"
MISSING_FIXTURE = "#type: node\n#context: sop\n#internal: missing\n= Missing =\n\"\"\"Missing.\"\"\"\n"
HISTORICAL_FIXTURE = "#type: node\n#context: sop\n= Agent Look At =\n\"\"\"Legacy.\"\"\"\n"
VEX_INTERSECT_FIXTURE = (
    "#type: vex\n#context: sop\n= intersect =\n\"\"\"Intersect.\"\"\"\n"
    ":usage: intersect(geo) -> int\n@related intersect\n"
)
HOM_NODE_FIXTURE = (
    "= hou.Node =\n#type: homclass\n#superclass: hou.NodeReferenceCounted\n"
    "\"\"\"Node class.\"\"\"\n"
    "::`createNode(self, type_name)` -> [Hom:hou.Node]:\n"
    "    #cppname: HOM_Node::createNode\n    Creates a node.\n"
)
HOM_SUPERCLASS_FIXTURE = "= hou.NodeReferenceCounted =\n#type: homclass\n\"\"\"Superclass.\"\"\"\n"

BOOLEAN_ID = "node_document:sop/boolean.txt@current"


def make_resolution_corpus() -> tuple[tuple[ParsedDocument, ...], frozenset[str]]:
    docs = (
        parse_node_document("sop/boolean.txt", BOOLEAN_FIXTURE),
        parse_node_document("sop/polyextrude-2.0.txt", POLYEXTRUDE_FIXTURE),
        parse_node_document("sop/missing.txt", MISSING_FIXTURE),
        parse_node_document("sop/agentlookat-2.0.txt", HISTORICAL_FIXTURE),
        parse_vex_document("functions/intersect.txt", VEX_INTERSECT_FIXTURE),
        parse_hom_document("hou/Node.txt", HOM_NODE_FIXTURE),
        parse_hom_document("hou/NodeReferenceCounted.txt", HOM_SUPERCLASS_FIXTURE),
    )
    return docs, frozenset({"boolean", "polyextrude::2.0"})


def _doc(entities=(), aliases=(), edges=()) -> ParsedDocument:
    return ParsedDocument(tuple(entities), tuple(aliases), tuple(edges))


def _entity(kind, entity_id, canonical_name, source_path, is_current=True, **attrs):
    return EntityDraft(
        entity_id=entity_id, kind=kind, subtype="", canonical_name=canonical_name,
        title=canonical_name, summary="", authority=Authority.OFFICIAL_HOUDINI_DOCS,
        source_path=source_path, source_anchor=None, is_current=is_current,
        attributes=attrs, body="", sections=(),
    )


def _alias(alias, entity_id, alias_type="qualified_name", priority=100):
    return AliasDraft(alias=alias, entity_id=entity_id, alias_type=alias_type, priority=priority)


def _edge(source_id, predicate, target_raw, target_anchor=None, source_location="loc:1"):
    return EdgeDraft(
        source_id=source_id, predicate=predicate, target_id=None,
        target_raw=target_raw, target_anchor=target_anchor, resolved=False,
        source_location=source_location,
    )


# --- node reconciliation --------------------------------------------------

def _node_entity(entity_id, canonical_name, source_path, candidates, is_current=True):
    return _entity(
        EntityKind.NODE_DOCUMENT, entity_id, canonical_name, source_path,
        is_current=is_current,
        operator_type_candidates=candidates,
        operator_type_status=OperatorTypeStatus.DOCUMENTED_UNVERIFIED,
        context="sop", namespace="", internal_metadata="",
        document_version="current" if is_current else "legacy",
        is_current_document=is_current, tags=(),
    )


def test_preferred_versioned_candidate_wins() -> None:
    e = _node_entity(
        "node_document:sop/polyextrude.txt@current", "polyextrude", "sop/polyextrude.txt",
        ("polyextrude::2.0", "polyextrude"),
    )
    bundle = assemble_graph((_doc([e]),), inventory=frozenset({"polyextrude::2.0", "polyextrude"}))
    entity = bundle.entities[0]
    assert entity.attributes["operator_type"] == "polyextrude::2.0"
    assert entity.attributes["operator_type_status"] == OperatorTypeStatus.VERIFIED_AT_BUILD


def test_unversioned_candidate_when_versioned_not_in_inventory() -> None:
    e = _node_entity(
        "node_document:sop/polyextrude.txt@current", "polyextrude", "sop/polyextrude.txt",
        ("polyextrude::2.0", "polyextrude"),
    )
    bundle = assemble_graph((_doc([e]),), inventory=frozenset({"polyextrude"}))
    entity = bundle.entities[0]
    assert entity.attributes["operator_type"] == "polyextrude"
    assert entity.attributes["operator_type_status"] == OperatorTypeStatus.VERIFIED_AT_BUILD


def test_filename_candidate_beats_incorrect_internal() -> None:
    docs = (parse_node_document("sop/apex--buildfkgraph.txt", APEX_FIXTURE),)
    bundle = assemble_graph(docs, inventory=frozenset({"apex::buildfkgraph"}))
    entity = bundle.entities[0]
    assert entity.attributes["operator_type"] == "apex::buildfkgraph"
    assert entity.attributes["operator_type_status"] == OperatorTypeStatus.VERIFIED_AT_BUILD


def test_exact_inventory_membership_only() -> None:
    docs = (parse_node_document("sop/boolean.txt", BOOLEAN_FIXTURE),)
    bundle = assemble_graph(docs, inventory=frozenset({"boolean"}))
    entity = bundle.entities[0]
    assert entity.attributes["operator_type"] == "boolean"
    assert entity.attributes["operator_type_status"] == OperatorTypeStatus.VERIFIED_AT_BUILD


def test_case_mismatch_does_not_verify() -> None:
    docs = (parse_node_document("sop/boolean.txt", BOOLEAN_FIXTURE),)
    bundle = assemble_graph(docs, inventory=frozenset({"Boolean"}))
    entity = bundle.entities[0]
    assert entity.attributes["operator_type_status"] == OperatorTypeStatus.UNRESOLVED
    assert entity.attributes["operator_type"] is None


def test_missing_candidates_become_unresolved() -> None:
    docs = (parse_node_document("sop/missing.txt", MISSING_FIXTURE),)
    bundle = assemble_graph(docs, inventory=frozenset())
    entity = bundle.entities[0]
    assert entity.attributes["operator_type_status"] == OperatorTypeStatus.UNRESOLVED


def test_current_verified_attributes_contain_operator_type() -> None:
    docs = (parse_node_document("sop/boolean.txt", BOOLEAN_FIXTURE),)
    bundle = assemble_graph(docs, inventory=frozenset({"boolean"}))
    entity = bundle.entities[0]
    assert entity.attributes["operator_type"] == "boolean"


def test_no_input_entity_attributes_mutation() -> None:
    doc = parse_node_document("sop/boolean.txt", BOOLEAN_FIXTURE)
    original = doc.entities[0]
    assert "operator_type" not in original.attributes
    assemble_graph((doc,), inventory=frozenset({"boolean"}))
    assert "operator_type" not in original.attributes
    assert original.attributes["operator_type_status"] == OperatorTypeStatus.DOCUMENTED_UNVERIFIED


def test_historical_remains_historical_only() -> None:
    docs = (parse_node_document("sop/agentlookat-2.0.txt", HISTORICAL_FIXTURE),)
    bundle = assemble_graph(docs, inventory=frozenset({"agentlookat::2.0"}))
    entity = bundle.entities[0]
    assert entity.attributes["operator_type_status"] == OperatorTypeStatus.HISTORICAL_ONLY
    assert entity.is_current is False


def test_historical_does_not_receive_verified_operator_alias() -> None:
    docs = (parse_node_document("sop/agentlookat-2.0.txt", HISTORICAL_FIXTURE),)
    bundle = assemble_graph(docs, inventory=frozenset({"agentlookat::2.0"}))
    entity = bundle.entities[0]
    verified = [
        a for a in bundle.aliases
        if a.entity_id == entity.entity_id and a.alias == "agentlookat::2.0"
        and a.alias_type == "operator_type" and a.priority > 100
    ]
    assert verified == []


def test_verified_operator_alias_has_highest_priority() -> None:
    docs = (parse_node_document("sop/boolean.txt", BOOLEAN_FIXTURE),)
    bundle = assemble_graph(docs, inventory=frozenset({"boolean"}))
    entity = bundle.entities[0]
    op_aliases = [a for a in bundle.aliases if a.entity_id == entity.entity_id and a.alias == "boolean"]
    assert op_aliases
    assert max(a.priority for a in op_aliases) > 100


def test_duplicate_candidate_names_remain_deterministic() -> None:
    docs = (parse_node_document("sop/boolean.txt", BOOLEAN_FIXTURE),)
    bundle = assemble_graph(docs, inventory=frozenset({"boolean"}))
    entity = bundle.entities[0]
    assert entity.attributes["operator_type_candidates"] == ("boolean",)


# --- reference resolution -------------------------------------------------

def test_node_typed_reference_resolves() -> None:
    docs, inv = make_resolution_corpus()
    bundle = assemble_graph(docs, inventory=inv)
    ref = next(e for e in bundle.edges if e.source_id == BOOLEAN_ID and e.target_raw == "Node:sop/boolean")
    assert ref.resolved is True
    assert ref.target_id == BOOLEAN_ID


def test_vex_typed_reference_resolves() -> None:
    docs, inv = make_resolution_corpus()
    bundle = assemble_graph(docs, inventory=inv)
    ref = next(e for e in bundle.edges if e.target_raw == "Vex:intersect")
    assert ref.resolved is True
    assert ref.target_id == "vex_function:intersect"


def test_hom_class_reference_resolves() -> None:
    docs, inv = make_resolution_corpus()
    bundle = assemble_graph(docs, inventory=inv)
    refs = [e for e in bundle.edges if e.target_raw == "Hom:hou.Node"]
    assert any(e.resolved and e.target_id == "hom_class:hou.Node" for e in refs)


def test_hom_method_reference_resolves() -> None:
    docs, inv = make_resolution_corpus()
    bundle = assemble_graph(docs, inventory=inv)
    ref = next(e for e in bundle.edges if e.target_raw == "Hom:hou.Node#createNode")
    assert ref.resolved is True
    assert ref.target_id == "hom_method:hou.Node#createNode"
    assert ref.target_anchor == "createNode"


def test_superclass_resolves() -> None:
    docs, inv = make_resolution_corpus()
    bundle = assemble_graph(docs, inventory=inv)
    edge = next(e for e in bundle.edges if e.predicate == "inherits_from")
    assert edge.resolved is True
    assert edge.target_id == "hom_class:hou.NodeReferenceCounted"


def test_vex_related_edge_resolves() -> None:
    docs, inv = make_resolution_corpus()
    bundle = assemble_graph(docs, inventory=inv)
    related = [e for e in bundle.edges if e.predicate == "related_to" and e.target_raw == "intersect"]
    assert related
    assert related[0].resolved is True
    assert related[0].target_id == "vex_function:intersect"


def test_unresolved_include_stays_unresolved() -> None:
    docs, inv = make_resolution_corpus()
    bundle = assemble_graph(docs, inventory=inv)
    includes = [e for e in bundle.edges if e.predicate == "includes"]
    assert includes
    for edge in includes:
        assert edge.resolved is False
        assert edge.target_id is None


def test_declares_method_remains_resolved() -> None:
    docs, inv = make_resolution_corpus()
    bundle = assemble_graph(docs, inventory=inv)
    declare = next(e for e in bundle.edges if e.predicate == "declares_method")
    assert declare.resolved is True
    assert declare.target_id == "hom_method:hou.Node#createNode"


def test_local_anchor_not_guessed() -> None:
    e = _entity(EntityKind.NODE_DOCUMENT, "node_document:sop/x.txt@current", "x", "sop/x.txt",
                operator_type_candidates=(), operator_type_status=OperatorTypeStatus.UNRESOLVED,
                context="sop", namespace="", internal_metadata="", document_version="current",
                is_current_document=True, tags=())
    edge = _edge("node_document:sop/x.txt@current", "references", "#local", target_anchor="local")
    bundle = assemble_graph((_doc([e], [], [edge]),), inventory=frozenset())
    assert bundle.edges[0].resolved is False
    assert bundle.edges[0].target_id is None


def test_ambiguous_reference_stays_unresolved() -> None:
    e1 = _entity(EntityKind.VEX_FUNCTION, "vex_function:amb1", "amb", "functions/amb1.txt")
    e2 = _entity(EntityKind.VEX_FUNCTION, "vex_function:amb2", "amb", "functions/amb2.txt")
    edge = _edge("vex_function:amb1", "references", "Vex:amb", source_location="functions/amb1.txt:1")
    bundle = assemble_graph((_doc([e1, e2], [_alias("amb", "vex_function:amb1"), _alias("amb", "vex_function:amb2")], [edge]),), inventory=frozenset())
    ref = bundle.edges[0]
    assert ref.resolved is False
    assert ref.target_id is None


# --- invariants -----------------------------------------------------------

def _bundle(entities=(), aliases=(), edges=()) -> GraphBundle:
    return GraphBundle(tuple(entities), tuple(aliases), tuple(edges))


def test_validate_rejects_duplicate_entity_id() -> None:
    e = _entity(EntityKind.VEX_FUNCTION, "vex_function:x", "x", "functions/x.txt")
    with pytest.raises(GraphError):
        validate_graph(_bundle([e, e]))


def test_validate_rejects_dangling_alias() -> None:
    e = _entity(EntityKind.VEX_FUNCTION, "vex_function:x", "x", "functions/x.txt")
    a = _alias("y", "vex_function:missing")
    with pytest.raises(GraphError):
        validate_graph(_bundle([e], [a]))


def test_validate_rejects_edge_unknown_source() -> None:
    edge = EdgeDraft(source_id="vex_function:ghost", predicate="references", target_id=None,
                     target_raw="Vex:z", target_anchor=None, resolved=False, source_location="l:1")
    with pytest.raises(GraphError):
        validate_graph(_bundle([], [], [edge]))


def test_validate_rejects_resolved_edge_with_none_target() -> None:
    e = _entity(EntityKind.VEX_FUNCTION, "vex_function:x", "x", "functions/x.txt")
    edge = EdgeDraft(source_id="vex_function:x", predicate="references", target_id=None,
                     target_raw="Vex:z", target_anchor=None, resolved=True, source_location="l:1")
    with pytest.raises(GraphError):
        validate_graph(_bundle([e], [], [edge]))


def test_validate_rejects_resolved_edge_unknown_target() -> None:
    e = _entity(EntityKind.VEX_FUNCTION, "vex_function:x", "x", "functions/x.txt")
    edge = EdgeDraft(source_id="vex_function:x", predicate="references", target_id="vex_function:ghost",
                     target_raw="Vex:z", target_anchor=None, resolved=True, source_location="l:1")
    with pytest.raises(GraphError):
        validate_graph(_bundle([e], [], [edge]))


def test_validate_rejects_unresolved_edge_with_target_id() -> None:
    e = _entity(EntityKind.VEX_FUNCTION, "vex_function:x", "x", "functions/x.txt")
    edge = EdgeDraft(source_id="vex_function:x", predicate="references", target_id="vex_function:x",
                     target_raw="Vex:z", target_anchor=None, resolved=False, source_location="l:1")
    with pytest.raises(GraphError):
        validate_graph(_bundle([e], [], [edge]))


# --- dedup and determinism ------------------------------------------------

def test_exact_duplicate_same_line_edge_deduped() -> None:
    e = _entity(EntityKind.VEX_FUNCTION, "vex_function:x", "x", "functions/x.txt")
    target = _entity(EntityKind.VEX_FUNCTION, "vex_function:y", "y", "functions/y.txt")
    edge = _edge("vex_function:x", "references", "Vex:y", source_location="functions/x.txt:1")
    bundle = assemble_graph((_doc([e, target], [_alias("y", "vex_function:y")], [edge, edge]),), inventory=frozenset())
    refs = [ed for ed in bundle.edges if ed.predicate == "references" and ed.source_id == "vex_function:x"]
    assert len(refs) == 1


def test_same_target_different_lines_kept() -> None:
    e = _entity(EntityKind.VEX_FUNCTION, "vex_function:x", "x", "functions/x.txt")
    target = _entity(EntityKind.VEX_FUNCTION, "vex_function:y", "y", "functions/y.txt")
    edge1 = _edge("vex_function:x", "references", "Vex:y", source_location="functions/x.txt:1")
    edge2 = _edge("vex_function:x", "references", "Vex:y", source_location="functions/x.txt:2")
    bundle = assemble_graph((_doc([e, target], [_alias("y", "vex_function:y")], [edge1, edge2]),), inventory=frozenset())
    refs = [ed for ed in bundle.edges if ed.predicate == "references" and ed.source_id == "vex_function:x"]
    assert len(refs) == 2


def test_input_tuple_order_determinism() -> None:
    docs, inv = make_resolution_corpus()
    b1 = assemble_graph(docs, inventory=inv)
    b2 = assemble_graph(tuple(reversed(docs)), inventory=inv)
    assert b1.entities == b2.entities
    assert b1.aliases == b2.aliases
    assert b1.edges == b2.edges


def test_assemble_graph_calls_validate() -> None:
    # Duplicate entity id in input -> assemble_graph raises (via validate).
    e = _entity(EntityKind.VEX_FUNCTION, "vex_function:x", "x", "functions/x.txt")
    with pytest.raises(GraphError):
        assemble_graph((_doc([e, e]),), inventory=frozenset())


# --- documented node versions (parser-driven) -----------------------------

def test_current_page_with_documented_version_picks_versioned_operator() -> None:
    # The page is current (filename has no suffix) but documents #version 2.0.
    # When the inventory contains both the versioned and the unversioned type,
    # reconciliation must select the versioned one because it leads the
    # candidate order.
    docs = (
        parse_node_document(
            "sop/boolean.txt",
            "#type: node\n#context: sop\n#version: 2.0\n"
            "= Boolean =\n\"\"\"Boolean op.\"\"\"",
        ),
    )
    bundle = assemble_graph(
        docs, inventory=frozenset({"boolean::2.0", "boolean"})
    )
    entity = bundle.entities[0]
    assert entity.attributes["operator_type"] == "boolean::2.0"
    assert (
        entity.attributes["operator_type_status"]
        == OperatorTypeStatus.VERIFIED_AT_BUILD
    )
