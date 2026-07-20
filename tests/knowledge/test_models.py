"""Contract tests for knowledge-graph enums, DTOs and entity IDs.

These lock the exact names/values pinned by the implementation plan's
"Stable Contracts" block and the ID/path normalization rules.
"""

import dataclasses

import pytest

import eee_agent.knowledge as knowledge_pkg
from eee_agent.knowledge.ids import make_entity_id, normalize_source_path
from eee_agent.knowledge.models import (
    AliasDraft,
    Authority,
    EdgeDraft,
    EntityDraft,
    EntityKind,
    GraphBundle,
    OperatorTypeStatus,
    ParsedDocument,
    ReferenceDraft,
    SectionDraft,
)


def test_authority_values_match_plan() -> None:
    assert Authority.OFFICIAL_HOUDINI_DOCS.value == "official_houdini_docs"
    assert Authority.PROJECT_VERIFIED_SKILL.value == "project_verified_skill"


def test_entity_kind_values_match_plan() -> None:
    assert EntityKind.NODE_DOCUMENT.value == "node_document"
    assert EntityKind.VEX_FUNCTION.value == "vex_function"
    assert EntityKind.HOM_CLASS.value == "hom_class"
    assert EntityKind.HOM_METHOD.value == "hom_method"
    assert EntityKind.HOM_FUNCTION.value == "hom_function"
    assert EntityKind.HOM_MODULE.value == "hom_module"
    assert EntityKind.HOM_PACKAGE.value == "hom_package"
    assert EntityKind.SKILL_REFERENCE.value == "skill_reference"


def test_operator_type_status_values_match_plan() -> None:
    assert OperatorTypeStatus.VERIFIED_AT_BUILD.value == "verified_at_build"
    assert OperatorTypeStatus.DOCUMENTED_UNVERIFIED.value == "documented_unverified"
    assert OperatorTypeStatus.AMBIGUOUS.value == "ambiguous"
    assert OperatorTypeStatus.HISTORICAL_ONLY.value == "historical_only"
    assert OperatorTypeStatus.UNRESOLVED.value == "unresolved"


def test_dto_field_names_match_contract() -> None:
    assert [f.name for f in dataclasses.fields(SectionDraft)] == [
        "key", "heading", "body", "ordinal",
    ]
    assert [f.name for f in dataclasses.fields(ReferenceDraft)] == [
        "display_text", "target_kind", "raw_target",
        "normalized_target", "anchor", "source_line",
    ]
    assert [f.name for f in dataclasses.fields(EntityDraft)] == [
        "entity_id", "kind", "subtype", "canonical_name", "title", "summary",
        "authority", "source_path", "source_anchor", "is_current",
        "attributes", "body", "sections",
    ]
    assert [f.name for f in dataclasses.fields(AliasDraft)] == [
        "alias", "entity_id", "alias_type", "priority",
    ]
    assert [f.name for f in dataclasses.fields(EdgeDraft)] == [
        "source_id", "predicate", "target_id", "target_raw",
        "target_anchor", "resolved", "source_location",
    ]
    assert [f.name for f in dataclasses.fields(ParsedDocument)] == [
        "entities", "aliases", "edges",
    ]
    assert [f.name for f in dataclasses.fields(GraphBundle)] == [
        "entities", "aliases", "edges",
    ]


@pytest.mark.parametrize(
    "dto",
    [
        SectionDraft(key="k", heading="h", body="b", ordinal=0),
        ReferenceDraft(
            display_text=None,
            target_kind="Node",
            raw_target="sop/boolean",
            normalized_target="sop/boolean",
            anchor=None,
            source_line=1,
        ),
        EntityDraft(
            entity_id="node_document:sop/x@current",
            kind=EntityKind.NODE_DOCUMENT,
            subtype="",
            canonical_name="x",
            title="X",
            summary="",
            authority=Authority.OFFICIAL_HOUDINI_DOCS,
            source_path="sop/x.txt",
            source_anchor=None,
            is_current=True,
        ),
        AliasDraft(
            alias="boolean",
            entity_id="node_document:sop/x@current",
            alias_type="short_name",
            priority=1,
        ),
        EdgeDraft(
            source_id="node_document:sop/x@current",
            predicate="references",
            target_id=None,
            target_raw="sop/boolean",
            target_anchor=None,
            resolved=False,
            source_location="sop/x.txt:1",
        ),
        ParsedDocument(entities=(), aliases=(), edges=()),
        GraphBundle(entities=(), aliases=(), edges=()),
    ],
)
def test_dtos_are_frozen_and_use_slots(dto: object) -> None:
    assert dataclasses.is_dataclass(dto)
    first_field = dataclasses.fields(dto)[0].name
    with pytest.raises(dataclasses.FrozenInstanceError):
        setattr(dto, first_field, "forbidden")
    assert not hasattr(dto, "__dict__")


def test_entity_draft_defaults_are_empty() -> None:
    entity = EntityDraft(
        entity_id="node_document:sop/x@current",
        kind=EntityKind.NODE_DOCUMENT,
        subtype="",
        canonical_name="x",
        title="X",
        summary="",
        authority=Authority.OFFICIAL_HOUDINI_DOCS,
        source_path="sop/x.txt",
        source_anchor=None,
        is_current=True,
    )
    assert entity.attributes == {}
    assert entity.body == ""
    assert entity.sections == ()


def test_source_path_is_logical_posix_and_rejects_escape() -> None:
    assert normalize_source_path(r"sop\apex--buildfkgraph.txt") == (
        "sop/apex--buildfkgraph.txt"
    )
    with pytest.raises(ValueError, match="unsafe source path"):
        normalize_source_path("../nodes.zip")
    with pytest.raises(ValueError, match="unsafe source path"):
        normalize_source_path("C:/houdini/help/nodes.zip")


def test_source_path_rejects_absolute_empty_and_control() -> None:
    with pytest.raises(ValueError, match="unsafe source path"):
        normalize_source_path("/etc/passwd")
    with pytest.raises(ValueError, match="unsafe source path"):
        normalize_source_path("D:\\houdini\\help\\nodes.zip")
    with pytest.raises(ValueError, match="unsafe source path"):
        normalize_source_path("")
    with pytest.raises(ValueError, match="unsafe source path"):
        normalize_source_path("   ")
    with pytest.raises(ValueError, match="unsafe source path"):
        normalize_source_path("sop/boolean.txt\x00")
    with pytest.raises(ValueError, match="unsafe source path"):
        normalize_source_path("sop/../vex/intersect.txt")


def test_source_path_normalizes_redundant_separators() -> None:
    assert normalize_source_path(r"hou\Node.txt") == "hou/Node.txt"


def test_entity_id_is_stable_and_kind_prefixed() -> None:
    assert make_entity_id(
        EntityKind.NODE_DOCUMENT,
        "sop/apex--buildfkgraph.txt@current",
    ) == "node_document:sop/apex--buildfkgraph.txt@current"


def test_entity_id_preserves_case_and_anchor_characters() -> None:
    assert make_entity_id(EntityKind.HOM_CLASS, "hou.Node") == "hom_class:hou.Node"
    assert make_entity_id(
        EntityKind.HOM_METHOD, "hou.Node#createNode"
    ) == "hom_method:hou.Node#createNode"


def test_entity_id_rejects_empty_and_control_keys() -> None:
    with pytest.raises(ValueError, match="unsafe entity key"):
        make_entity_id(EntityKind.HOM_CLASS, "")
    with pytest.raises(ValueError, match="unsafe entity key"):
        make_entity_id(EntityKind.HOM_CLASS, "hou.Node\x01")


def test_package_exports_only_enums_and_dtos() -> None:
    assert set(knowledge_pkg.__all__) == {
        "Authority",
        "EntityKind",
        "OperatorTypeStatus",
        "SectionDraft",
        "ReferenceDraft",
        "EntityDraft",
        "AliasDraft",
        "EdgeDraft",
        "ParsedDocument",
        "GraphBundle",
    }
    for name in knowledge_pkg.__all__:
        assert hasattr(knowledge_pkg, name), name
