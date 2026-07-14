"""Tests for the HOM page and method parser.

All fixtures are synthetic minimal text — no real SideFX document bodies.
"""

from __future__ import annotations

import pytest

from eee_agent.knowledge.models import (
    Authority,
    EdgeDraft,
    EntityKind,
    ParsedDocument,
)
from eee_agent.knowledge.parse_hom import parse_hom_document


HOM_NODE_FIXTURE = (
    "#type: homclass\n#namespace: hou\n#class: Node\n"
    "#superclass: hou.NodeReferenceCounted\n#cppname: HOM_Node\n"
    "= hou.Node =\n"
    "\"\"\"Node class summary.\"\"\"\n"
    "::createNode\n"
    ":signature: createNode(type, name=None) -> Node\n"
    ":returns: hou.Node\n"
    ":cppname: createNode\n"
    "Creates a new node.\n"
    "::createNode\n"
    ":signature: createNode(type, name) -> Node\n"
    "Second overload body.\n"
    "::path\n"
    ":signature: path() -> str\n"
    ":returns: str\n"
    ":cppname: path\n"
    "Returns the node path.\n"
    "== See Also ==\n"
    "See [Hom:hou.Node#createNode] and [Vex:intersect].\n"
)

HOM_NODE_FUNCTION_FIXTURE = (
    "#type: homfunction\n#namespace: hou\n#function: node\n"
    "#cppname: HOM_node\n= hou.node =\n"
    "\"\"\"Return the node at a path.\"\"\"\n"
)

HOM_MODULE_FIXTURE = (
    "#type: hommodule\n#namespace: hou\n#module: qt\n"
    "= hou.qt =\n\"\"\"Qt helpers for Houdini.\"\"\"\n"
)

HOM_PYPACKAGE_FIXTURE = (
    "#type: pypackage\n#namespace: hou\n#package: ui\n"
    "= hou.ui =\n\"\"\"UI package.\"\"\"\n"
)

HOM_HOMPACKAGE_FIXTURE = (
    "#type: hompackage\n#namespace: hou\n#package: hooks\n"
    "= hou.hooks =\n\"\"\"Hooks package.\"\"\"\n"
)

HOM_INCLUDE_FIXTURE = (
    "#type: include\n= shared =\n\"\"\"Shared content.\"\"\"\n"
)


@pytest.mark.parametrize(
    ("page_type", "kind"),
    [
        ("homclass", EntityKind.HOM_CLASS),
        ("homfunction", EntityKind.HOM_FUNCTION),
        ("hommodule", EntityKind.HOM_MODULE),
        ("pypackage", EntityKind.HOM_PACKAGE),
        ("hompackage", EntityKind.HOM_PACKAGE),
    ],
)
def test_hom_page_types_route_correctly(page_type: str, kind: EntityKind) -> None:
    text = f"#type: {page_type}\n#namespace: hou\n= hou.x =\n\"\"\"Summary.\"\"\"\n"
    parsed = parse_hom_document(f"hou/{page_type}.txt", text)
    assert len(parsed.entities) == 1
    assert parsed.entities[0].kind == kind
    assert parsed.entities[0].subtype == page_type


def test_include_page_returns_empty_parsed_document() -> None:
    parsed = parse_hom_document("hou/_shared.txt", HOM_INCLUDE_FIXTURE)
    assert isinstance(parsed, ParsedDocument)
    assert parsed.entities == ()
    assert parsed.aliases == ()
    assert parsed.edges == ()


def test_unknown_page_type_returns_empty() -> None:
    text = "#type: notahomtype\n#namespace: hou\n= hou.x =\n\"\"\"S.\"\"\"\n"
    parsed = parse_hom_document("hou/x.txt", text)
    assert parsed.entities == ()


def test_hom_class_entity() -> None:
    parsed = parse_hom_document("hou/Node.txt", HOM_NODE_FIXTURE)
    entity = parsed.entities[0]
    assert entity.entity_id == "hom_class:hou.Node"
    assert entity.kind == EntityKind.HOM_CLASS
    assert entity.title == "hou.Node"
    assert entity.summary == "Node class summary."
    assert entity.attributes["cppname"] == "HOM_Node"
    assert entity.attributes["namespace"] == "hou"


def test_hom_function_entity() -> None:
    entity = parse_hom_document("hou/node_.txt", HOM_NODE_FUNCTION_FIXTURE).entities[0]
    assert entity.entity_id == "hom_function:hou.node"
    assert entity.kind == EntityKind.HOM_FUNCTION
    assert entity.attributes["cppname"] == "HOM_node"


def test_hom_module_entity() -> None:
    entity = parse_hom_document("hou/qt.txt", HOM_MODULE_FIXTURE).entities[0]
    assert entity.entity_id == "hom_module:hou.qt"
    assert entity.kind == EntityKind.HOM_MODULE


def test_both_package_spellings_are_hom_package() -> None:
    py = parse_hom_document("hou/ui.txt", HOM_PYPACKAGE_FIXTURE).entities[0]
    hom = parse_hom_document("hou/hooks.txt", HOM_HOMPACKAGE_FIXTURE).entities[0]
    assert py.kind == EntityKind.HOM_PACKAGE
    assert hom.kind == EntityKind.HOM_PACKAGE
    assert py.entity_id == "hom_package:hou.ui"
    assert hom.entity_id == "hom_package:hou.hooks"
    assert py.subtype == "pypackage"
    assert hom.subtype == "hompackage"


def test_class_and_function_case_are_distinct() -> None:
    klass = parse_hom_document("hou/Node.txt", HOM_NODE_FIXTURE)
    func = parse_hom_document("hou/node_.txt", HOM_NODE_FUNCTION_FIXTURE)
    assert klass.entities[0].entity_id == "hom_class:hou.Node"
    assert func.entities[0].entity_id == "hom_function:hou.node"
    assert klass.entities[0].entity_id != func.entities[0].entity_id


def test_two_methods_become_entities() -> None:
    parsed = parse_hom_document("hou/Node.txt", HOM_NODE_FIXTURE)
    by_id = {e.entity_id: e for e in parsed.entities}
    assert "hom_method:hou.Node#createNode" in by_id
    assert "hom_method:hou.Node#path" in by_id


def test_repeated_method_signatures_aggregate_in_source_order() -> None:
    parsed = parse_hom_document("hou/Node.txt", HOM_NODE_FIXTURE)
    method = {e.entity_id: e for e in parsed.entities}["hom_method:hou.Node#createNode"]
    assert method.attributes["signatures"] == (
        "createNode(type, name=None) -> Node",
        "createNode(type, name) -> Node",
    )


def test_overloads_do_not_overwrite() -> None:
    parsed = parse_hom_document("hou/Node.txt", HOM_NODE_FIXTURE)
    method = {e.entity_id: e for e in parsed.entities}["hom_method:hou.Node#createNode"]
    assert len(method.attributes["signatures"]) == 2


def test_method_block_stops_at_next_method() -> None:
    parsed = parse_hom_document("hou/Node.txt", HOM_NODE_FIXTURE)
    path = {e.entity_id: e for e in parsed.entities}["hom_method:hou.Node#path"]
    assert path.attributes["signatures"] == ("path() -> str",)
    assert "Creates a new node." not in path.body
    assert "Second overload" not in path.body


def test_method_block_stops_at_next_section() -> None:
    parsed = parse_hom_document("hou/Node.txt", HOM_NODE_FIXTURE)
    path = {e.entity_id: e for e in parsed.entities}["hom_method:hou.Node#path"]
    assert "See Also" not in path.body
    assert "[Hom:" not in path.body


def test_method_owner_name_qualified_name_attributes() -> None:
    parsed = parse_hom_document("hou/Node.txt", HOM_NODE_FIXTURE)
    method = {e.entity_id: e for e in parsed.entities}["hom_method:hou.Node#createNode"]
    assert method.attributes["owner"] == "hou.Node"
    assert method.attributes["name"] == "createNode"
    assert method.attributes["qualified_name"] == "hou.Node#createNode"
    assert method.source_anchor == "createNode"


def test_method_returns_summary_cppname() -> None:
    parsed = parse_hom_document("hou/Node.txt", HOM_NODE_FIXTURE)
    method = {e.entity_id: e for e in parsed.entities}["hom_method:hou.Node#createNode"]
    assert method.attributes["returns"] == "hou.Node"
    assert method.attributes["cppname"] == "createNode"
    assert method.summary == "Creates a new node."


def test_declares_method_edge_targets_method_entity() -> None:
    parsed = parse_hom_document("hou/Node.txt", HOM_NODE_FIXTURE)
    declare_edges = [e for e in parsed.edges if e.predicate == "declares_method"]
    assert len(declare_edges) == 2
    targets = {e.target_id for e in declare_edges}
    assert "hom_method:hou.Node#createNode" in targets
    assert "hom_method:hou.Node#path" in targets
    for edge in declare_edges:
        assert edge.resolved is True
        assert edge.source_location.startswith("hou/Node.txt:")


def test_inherits_from_edge_is_unresolved() -> None:
    parsed = parse_hom_document("hou/Node.txt", HOM_NODE_FIXTURE)
    inherit_edges = [e for e in parsed.edges if e.predicate == "inherits_from"]
    assert len(inherit_edges) == 1
    edge = inherit_edges[0]
    assert edge.source_id == "hom_class:hou.Node"
    assert edge.target_raw == "hou.NodeReferenceCounted"
    assert edge.target_id is None
    assert edge.resolved is False
    assert edge.source_location.startswith("hou/Node.txt:")


def test_aliases_exact_short_casefold_with_relative_priority() -> None:
    parsed = parse_hom_document("hou/Node.txt", HOM_NODE_FIXTURE)
    class_id = "hom_class:hou.Node"
    aliases = [a for a in parsed.aliases if a.entity_id == class_id]
    by_type = {a.alias_type: a for a in aliases}
    assert by_type["qualified_name"].alias == "hou.Node"
    assert by_type["short_name"].alias == "Node"
    assert by_type["casefold_alias"].alias == "hou.node"
    assert by_type["qualified_name"].priority > by_type["short_name"].priority
    assert by_type["short_name"].priority > by_type["casefold_alias"].priority


def test_unresolved_ordinary_references_remain_unresolved() -> None:
    parsed = parse_hom_document("hou/Node.txt", HOM_NODE_FIXTURE)
    ref_edges = [e for e in parsed.edges if e.predicate == "references"]
    targets = {(e.target_raw, e.target_anchor) for e in ref_edges}
    assert ("hou.Node#createNode", "createNode") in targets
    assert ("intersect", None) in targets
    for edge in ref_edges:
        assert edge.target_id is None
        assert edge.resolved is False


def test_official_authority() -> None:
    parsed = parse_hom_document("hou/Node.txt", HOM_NODE_FIXTURE)
    for entity in parsed.entities:
        assert entity.authority == Authority.OFFICIAL_HOUDINI_DOCS


def test_normalized_logical_source_path() -> None:
    entity = parse_hom_document(r"hou\Node.txt", HOM_NODE_FIXTURE).entities[0]
    assert entity.source_path == "hou/Node.txt"
    assert "\\" not in entity.source_path
