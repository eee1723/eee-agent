"""Tests for the SOP node-document parser.

All fixtures are synthetic minimal text — no real SideFX document bodies.
"""

from __future__ import annotations

from eee_agent.knowledge.models import (
    AliasDraft,
    Authority,
    EdgeDraft,
    EntityDraft,
    EntityKind,
    OperatorTypeStatus,
    ParsedDocument,
)
from eee_agent.knowledge.parse_node import parse_node_document


APEX_FIXTURE = (
    "#type: node\n#context: sop\n#namespace: apex\n"
    "#internal: graph\n= APEX Build FK Graph =\n\n"
    "\"\"\"Builds a graph.\"\"\""
)

LOADSLICES_FIXTURE = (
    "#type: node\n#context: sop\n#internal: file\n"
    "= Load Slices =\n\"\"\"Loads slices from a file.\"\"\""
)

AGENTLOOKAT_CURRENT = (
    "#type: node\n#context: sop\n= Agent Look At =\n"
    "\"\"\"Orients an agent to look at a target.\"\"\""
)
AGENTLOOKAT_VERSIONED = (
    "#type: node\n#context: sop\n#version: 2.0\n= Agent Look At =\n"
    "\"\"\"Version 2 of agent look at.\"\"\""
)
AGENTLOOKAT_LEGACY = (
    "#type: node\n#context: sop\n= Agent Look At =\n"
    "\"\"\"Legacy agent look at page.\"\"\""
)

THREE_AGENTLOOKAT_FIXTURES = (
    ("sop/agentlookat.txt", AGENTLOOKAT_CURRENT),
    ("sop/agentlookat-2.0.txt", AGENTLOOKAT_VERSIONED),
    ("sop/agentlookat-.txt", AGENTLOOKAT_LEGACY),
)

BOOLEAN_FIXTURE = (
    "#type: node\n#context: sop\n#tags: model, polygons\n"
    "= Boolean =\n\"\"\"Boolean operation between solids.\"\"\"\n"
    "== Overview == (overview)\n"
    "See [Hom:hou.Node#createNode] and [Vex:intersect].\n"
    ":include _common#geometry:\n"
)


def test_namespace_filename_beats_incorrect_internal_metadata() -> None:
    parsed = parse_node_document("sop/apex--buildfkgraph.txt", APEX_FIXTURE)
    entity = parsed.entities[0]
    assert entity.entity_id == "node_document:sop/apex--buildfkgraph.txt@current"
    assert entity.attributes["operator_type_candidates"] == (
        "apex::buildfkgraph", "graph",
    )


def test_internal_metadata_does_not_override_filename_candidate() -> None:
    parsed = parse_node_document("sop/loadslices.txt", LOADSLICES_FIXTURE)
    entity = parsed.entities[0]
    # The filename-derived "loadslices" comes first; the incorrect #internal
    # "file" is only a low-trust trailing candidate.
    assert entity.attributes["operator_type_candidates"] == ("loadslices", "file")


def test_historical_versions_have_distinct_ids() -> None:
    ids = {
        parse_node_document(path, text).entities[0].entity_id
        for path, text in THREE_AGENTLOOKAT_FIXTURES
    }
    assert ids == {
        "node_document:sop/agentlookat.txt@current",
        "node_document:sop/agentlookat-2.0.txt@2.0",
        "node_document:sop/agentlookat-.txt@legacy",
    }


def test_current_document_status() -> None:
    entity = parse_node_document(
        "sop/agentlookat.txt", AGENTLOOKAT_CURRENT
    ).entities[0]
    assert entity.is_current is True
    assert entity.attributes["is_current_document"] is True
    assert entity.attributes["document_version"] == "current"
    assert (
        entity.attributes["operator_type_status"]
        == OperatorTypeStatus.DOCUMENTED_UNVERIFIED
    )


def test_versioned_historical_status() -> None:
    entity = parse_node_document(
        "sop/agentlookat-2.0.txt", AGENTLOOKAT_VERSIONED
    ).entities[0]
    assert entity.is_current is False
    assert entity.attributes["is_current_document"] is False
    assert entity.attributes["document_version"] == "2.0"
    assert (
        entity.attributes["operator_type_status"]
        == OperatorTypeStatus.HISTORICAL_ONLY
    )


def test_legacy_final_dash_historical_status() -> None:
    entity = parse_node_document(
        "sop/agentlookat-.txt", AGENTLOOKAT_LEGACY
    ).entities[0]
    assert entity.is_current is False
    assert entity.attributes["document_version"] == "legacy"
    assert (
        entity.attributes["operator_type_status"]
        == OperatorTypeStatus.HISTORICAL_ONLY
    )


def test_version_metadata_alone_does_not_make_current_filename_historical() -> None:
    # The filename has no historical suffix, so the page is current even though
    # #version is present.
    entity = parse_node_document(
        "sop/agentlookat.txt", AGENTLOOKAT_VERSIONED
    ).entities[0]
    assert entity.is_current is True
    assert entity.attributes["document_version"] == "current"
    assert (
        entity.attributes["operator_type_status"]
        == OperatorTypeStatus.DOCUMENTED_UNVERIFIED
    )


def test_normal_hyphen_in_slug_is_not_a_version() -> None:
    parsed = parse_node_document(
        "sop/build-fk.txt",
        "#type: node\n#context: sop\n= Build FK =\n\"\"\"Builds FK.\"\"\"",
    )
    entity = parsed.entities[0]
    assert entity.entity_id == "node_document:sop/build-fk.txt@current"
    assert entity.is_current is True
    assert entity.attributes["operator_type_candidates"] == ("build-fk",)


def test_candidate_ordering_and_deduplication() -> None:
    # #internal equals the filename-derived candidate -> deduped to one entry.
    parsed = parse_node_document(
        "sop/boolean.txt",
        "#type: node\n#context: sop\n#internal: boolean\n"
        "= Boolean =\n\"\"\"Boolean op.\"\"\"",
    )
    entity = parsed.entities[0]
    assert entity.attributes["operator_type_candidates"] == ("boolean",)


def test_versioned_candidate_ordering() -> None:
    entity = parse_node_document(
        "sop/agentlookat-2.0.txt", AGENTLOOKAT_VERSIONED
    ).entities[0]
    assert entity.attributes["operator_type_candidates"] == (
        "agentlookat::2.0", "agentlookat",
    )


def test_internal_is_low_priority_alias() -> None:
    parsed = parse_node_document("sop/apex--buildfkgraph.txt", APEX_FIXTURE)
    aliases = parsed.aliases
    internal = next(
        a for a in aliases if a.alias_type == "internal_metadata"
    )
    assert internal.alias == "graph"
    operator = next(
        a for a in aliases if a.alias_type == "operator_type"
    )
    assert operator.alias == "apex::buildfkgraph"
    assert internal.priority < operator.priority


def test_context_and_tags_attributes() -> None:
    entity = parse_node_document("sop/boolean.txt", BOOLEAN_FIXTURE).entities[0]
    assert entity.attributes["context"] == "sop"
    assert entity.attributes["tags"] == ("model", "polygons")
    assert entity.subtype == "sop"


def test_logical_normalized_source_path() -> None:
    entity = parse_node_document(
        r"sop\apex--buildfkgraph.txt", APEX_FIXTURE
    ).entities[0]
    assert entity.source_path == "sop/apex--buildfkgraph.txt"
    assert "\\" not in entity.source_path


def test_official_authority() -> None:
    entity = parse_node_document("sop/apex--buildfkgraph.txt", APEX_FIXTURE).entities[0]
    assert entity.authority == Authority.OFFICIAL_HOUDINI_DOCS


def test_title_summary_and_sections() -> None:
    entity = parse_node_document("sop/boolean.txt", BOOLEAN_FIXTURE).entities[0]
    assert entity.title == "Boolean"
    assert entity.summary == "Boolean operation between solids."
    assert [s.key for s in entity.sections] == ["overview"]
    assert "See [Hom:hou.Node#createNode]" in entity.sections[0].body


def test_unresolved_ordinary_reference_edge() -> None:
    parsed = parse_node_document("sop/boolean.txt", BOOLEAN_FIXTURE)
    ref_edges = [e for e in parsed.edges if e.predicate == "references"]
    targets = {(e.target_raw, e.target_anchor) for e in ref_edges}
    assert ("Hom:hou.Node#createNode", "createNode") in targets
    assert ("Vex:intersect", None) in targets
    for edge in ref_edges:
        assert edge.target_id is None
        assert edge.resolved is False
        assert edge.source_location.startswith("sop/boolean.txt:")


def test_typed_references_preserve_kind_in_target_raw() -> None:
    fixture = (
        "#type: node\n#context: sop\n= Typed =\n\"\"\"Typed refs.\"\"\"\n"
        "See [Node:sop/boolean], [Boolean SOP|Node:sop/boolean], "
        "[Hom:hou.Node#createNode] and [Vex:intersect].\n"
    )
    parsed = parse_node_document("sop/typed.txt", fixture)
    ref_edges = [e for e in parsed.edges if e.predicate == "references"]
    target_raws = {e.target_raw for e in ref_edges}
    assert target_raws == {
        "Node:sop/boolean",
        "Hom:hou.Node#createNode",
        "Vex:intersect",
    }
    hom = next(e for e in ref_edges if e.target_raw == "Hom:hou.Node#createNode")
    assert hom.target_anchor == "createNode"


def test_typed_reference_collision_remains_distinguishable() -> None:
    fixture = (
        "#type: node\n#context: sop\n= Collision =\n\"\"\"Collisions.\"\"\"\n"
        "See [Node:foo] [Vex:foo] [Hom:foo].\n"
    )
    parsed = parse_node_document("sop/collision.txt", fixture)
    ref_edges = [e for e in parsed.edges if e.predicate == "references"]
    assert {e.target_raw for e in ref_edges} == {"Node:foo", "Vex:foo", "Hom:foo"}


def test_unresolved_include_edge() -> None:
    parsed = parse_node_document("sop/boolean.txt", BOOLEAN_FIXTURE)
    include_edges = [e for e in parsed.edges if e.predicate == "includes"]
    assert len(include_edges) == 1
    edge = include_edges[0]
    assert edge.target_raw == "_common#geometry"
    assert edge.target_anchor == "geometry"
    assert edge.target_id is None
    assert edge.resolved is False
    assert edge.source_location.startswith("sop/boolean.txt:")


def test_source_location_is_one_based_and_logical() -> None:
    parsed = parse_node_document("sop/boolean.txt", BOOLEAN_FIXTURE)
    # Hom and Vex refs are on line 7; the include is on line 8.
    locations = sorted(e.source_location for e in parsed.edges)
    assert "sop/boolean.txt:7" in locations
    assert "sop/boolean.txt:8" in locations
    assert all("sop/boolean.txt:" in e.source_location for e in parsed.edges)


def test_returns_parsed_document_and_entity_kind() -> None:
    parsed = parse_node_document("sop/apex--buildfkgraph.txt", APEX_FIXTURE)
    assert isinstance(parsed, ParsedDocument)
    assert len(parsed.entities) == 1
    assert parsed.entities[0].kind == EntityKind.NODE_DOCUMENT


def test_parser_does_not_read_filesystem() -> None:
    # The source path does not exist on disk; parsing must succeed using only
    # the supplied path string and text.
    parsed = parse_node_document(
        "sop/does-not-exist-on-disk.txt",
        "#type: node\n#context: sop\n= Ghost =\n\"\"\"No file needed.\"\"\"",
    )
    assert parsed.entities[0].source_path == "sop/does-not-exist-on-disk.txt"


# --- documented version (#version) on a current page ----------------------
# Document identity is still decided only by the filename suffix. The operator
# candidate version is derived separately: the filename version (if any) wins,
# otherwise the documented #version is honored.

def test_current_page_honors_documented_version_candidate() -> None:
    # The filename has no historical suffix, so the page stays current even
    # though #version is documented. The operator candidate gains the versioned
    # form, ordered before the unversioned form.
    entity = parse_node_document(
        "sop/boolean.txt",
        "#type: node\n#context: sop\n#version: 2.0\n"
        "= Boolean =\n\"\"\"Boolean op.\"\"\"",
    ).entities[0]
    assert entity.attributes["operator_type_candidates"] == (
        "boolean::2.0", "boolean",
    )
    assert entity.is_current is True
    assert entity.attributes["document_version"] == "current"
    assert (
        entity.attributes["operator_type_status"]
        == OperatorTypeStatus.DOCUMENTED_UNVERIFIED
    )


def test_metadata_only_namespace_and_version() -> None:
    # No filename namespace/version; the effective namespace comes from
    # #namespace and the effective version from #version.
    entity = parse_node_document(
        "sop/tool.txt",
        "#type: node\n#context: sop\n#namespace: acme\n#version: 2.0\n"
        "= Tool =\n\"\"\"A namespaced tool.\"\"\"",
    ).entities[0]
    assert entity.attributes["operator_type_candidates"] == (
        "acme::tool::2.0", "acme::tool",
    )
    assert entity.attributes["namespace"] == "acme"
    assert entity.canonical_name == "acme::tool"


def test_namespaced_current_page_prioritizes_versioned_candidate() -> None:
    # Filename supplies the namespace; #version supplies the version. The
    # versioned candidate leads, then the unversioned, then #internal.
    entity = parse_node_document(
        "sop/apex--buildfkgraph.txt",
        "#type: node\n#context: sop\n#namespace: apex\n#version: 1.0\n"
        "#internal: graph\n= APEX Build FK Graph =\n\n"
        "\"\"\"Builds a graph.\"\"\"",
    ).entities[0]
    assert entity.attributes["operator_type_candidates"] == (
        "apex::buildfkgraph::1.0", "apex::buildfkgraph", "graph",
    )
    assert entity.is_current is True
    assert entity.attributes["document_version"] == "current"
    assert entity.attributes["namespace"] == "apex"
    assert entity.canonical_name == "apex::buildfkgraph"


def test_documented_version_does_not_change_document_identity() -> None:
    # A historical filename suffix still wins for document identity. #version
    # alone cannot turn a current filename historical and cannot override a
    # filename version; it only contributes operator candidates when the
    # filename has no version of its own.
    current_with_version = parse_node_document(
        "sop/agentlookat.txt",
        "#type: node\n#context: sop\n#version: 2.0\n"
        "= Agent Look At =\n\"\"\"Current with a version note.\"\"\"",
    ).entities[0]
    assert current_with_version.is_current is True
    assert current_with_version.attributes["document_version"] == "current"
    historical = parse_node_document(
        "sop/agentlookat-2.0.txt", AGENTLOOKAT_VERSIONED
    ).entities[0]
    assert historical.is_current is False
    assert historical.attributes["document_version"] == "2.0"
    assert historical.attributes["operator_type_candidates"] == (
        "agentlookat::2.0", "agentlookat",
    )
