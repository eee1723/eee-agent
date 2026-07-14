"""Tests for the VEX function and project skill parsers.

All fixtures are synthetic minimal text — no real SideFX document bodies and
no real project skill content.
"""

from __future__ import annotations

import hashlib

from eee_agent.knowledge.models import Authority, EntityKind, ParsedDocument
from eee_agent.knowledge.parse_skill import parse_skill_document
from eee_agent.knowledge.parse_vex import parse_vex_document


VEX_FIXTURE = (
    "#type: vex\n#context: sop\n#group: geometry\n#tags: intersect, ray\n"
    "= intersect_all =\n"
    "\"\"\"Intersect a ray with geometry, returning all hits.\"\"\"\n"
    ":usage: intersect_all(geometry, ray, positions) -> int\n"
    ":usage: intersect_all(geometry, ray, positions, tolerance) -> int\n"
    ":returns: int\n"
    "@related intersect\n"
    "@related rayhit\n"
    "== See Also ==\n"
    "See [Node:sop/boolean].\n"
)

VEX_COMMON_FIXTURE = "#type: vex\n= _common =\n\"\"\"Common include text.\"\"\"\n"
VEX_SUITE_FIXTURE = "#type: suite\n= suite =\n\"\"\"Suite page.\"\"\"\n"
VEX_CONTEXT_FIXTURE = "#type: context\n= context =\n\"\"\"Context page.\"\"\"\n"
VEX_INCLUDE_FIXTURE = "#type: include\n= inc =\n\"\"\"Include page.\"\"\"\n"

SKILL_FIXTURE = (
    "---\nname: vex-patterns\n"
    "description: Reusable VEX snippets for procedural modeling.\n"
    "---\n\n"
    "# VEX Patterns\n\n"
    "## Overview\n"
    "Use [Vex:intersect] for ray hits. You can use the intersect function here.\n\n"
    "## Snippets\n"
    "See [Node:sop/boolean].\n"
)


# --- VEX ------------------------------------------------------------------

def test_vex_entity_id() -> None:
    entity = parse_vex_document("functions/intersect_all.txt", VEX_FIXTURE).entities[0]
    assert entity.entity_id == "vex_function:intersect_all"
    assert entity.kind == EntityKind.VEX_FUNCTION


def test_vex_signatures_and_related_edges() -> None:
    parsed = parse_vex_document("functions/intersect_all.txt", VEX_FIXTURE)
    entity = parsed.entities[0]
    assert entity.entity_id == "vex_function:intersect_all"
    assert len(entity.attributes["signatures"]) == 2
    assert any(edge.predicate == "related_to" for edge in parsed.edges)


def test_vex_signatures_in_document_order() -> None:
    entity = parse_vex_document("functions/intersect_all.txt", VEX_FIXTURE).entities[0]
    assert entity.attributes["signatures"] == (
        "intersect_all(geometry, ray, positions) -> int",
        "intersect_all(geometry, ray, positions, tolerance) -> int",
    )


def test_vex_returns() -> None:
    entity = parse_vex_document("functions/intersect_all.txt", VEX_FIXTURE).entities[0]
    assert entity.attributes["returns"] == "int"


def test_vex_context_group_tags() -> None:
    entity = parse_vex_document("functions/intersect_all.txt", VEX_FIXTURE).entities[0]
    assert entity.attributes["context"] == "sop"
    assert entity.attributes["group"] == "geometry"
    assert entity.attributes["tags"] == ("intersect", "ray")


def test_vex_related_to_edges_are_unresolved() -> None:
    parsed = parse_vex_document("functions/intersect_all.txt", VEX_FIXTURE)
    related = [e for e in parsed.edges if e.predicate == "related_to"]
    assert {e.target_raw for e in related} == {"intersect", "rayhit"}
    for edge in related:
        assert edge.target_id is None
        assert edge.resolved is False
        assert edge.source_location.startswith("functions/intersect_all.txt:")


def test_vex_typed_reference_edge_is_unresolved() -> None:
    parsed = parse_vex_document("functions/intersect_all.txt", VEX_FIXTURE)
    ref_edges = [e for e in parsed.edges if e.predicate == "references"]
    assert len(ref_edges) == 1
    assert ref_edges[0].target_raw == "Node:sop/boolean"
    assert ref_edges[0].target_id is None
    assert ref_edges[0].resolved is False


def test_vex_official_authority() -> None:
    entity = parse_vex_document("functions/intersect_all.txt", VEX_FIXTURE).entities[0]
    assert entity.authority == Authority.OFFICIAL_HOUDINI_DOCS


def test_vex_normalized_logical_source_path() -> None:
    entity = parse_vex_document(r"functions\intersect_all.txt", VEX_FIXTURE).entities[0]
    assert entity.source_path == "functions/intersect_all.txt"


def test_vex_common_page_excluded() -> None:
    parsed = parse_vex_document("functions/_common.txt", VEX_COMMON_FIXTURE)
    assert parsed.entities == ()


def test_vex_suite_page_excluded() -> None:
    parsed = parse_vex_document("functions/suite.txt", VEX_SUITE_FIXTURE)
    assert parsed.entities == ()


def test_vex_context_page_excluded() -> None:
    parsed = parse_vex_document("functions/context.txt", VEX_CONTEXT_FIXTURE)
    assert parsed.entities == ()


def test_vex_include_page_excluded() -> None:
    parsed = parse_vex_document("functions/inc.txt", VEX_INCLUDE_FIXTURE)
    assert parsed.entities == ()


def test_vex_parser_does_not_read_filesystem() -> None:
    parsed = parse_vex_document(
        "functions/does-not-exist.txt",
        "#type: vex\n#context: sop\n= ghost =\n\"\"\"No file.\"\"\"\n",
    )
    assert parsed.entities[0].source_path == "functions/does-not-exist.txt"


# --- Skill ----------------------------------------------------------------

def test_skill_entity_id_and_slug() -> None:
    entity = parse_skill_document("skills/vex-patterns/SKILL.md", SKILL_FIXTURE).entities[0]
    assert entity.entity_id == "skill_reference:vex-patterns"
    assert entity.kind == EntityKind.SKILL_REFERENCE
    assert entity.attributes["slug"] == "vex-patterns"


def test_skill_has_project_authority_and_content_hash() -> None:
    parsed = parse_skill_document("skills/vex-patterns/SKILL.md", SKILL_FIXTURE)
    entity = parsed.entities[0]
    assert entity.authority.value == "project_verified_skill"
    assert len(entity.attributes["sha256"]) == 64


def test_skill_project_authority() -> None:
    entity = parse_skill_document("skills/vex-patterns/SKILL.md", SKILL_FIXTURE).entities[0]
    assert entity.authority == Authority.PROJECT_VERIFIED_SKILL


def test_skill_normalized_repository_relative_path() -> None:
    entity = parse_skill_document(
        r"skills\vex-patterns\SKILL.md", SKILL_FIXTURE
    ).entities[0]
    assert entity.source_path == "skills/vex-patterns/SKILL.md"


def test_skill_sha256_is_64_lowercase_hex() -> None:
    entity = parse_skill_document("skills/vex-patterns/SKILL.md", SKILL_FIXTURE).entities[0]
    sha = entity.attributes["sha256"]
    assert len(sha) == 64
    assert all(c in "0123456789abcdef" for c in sha)


def test_skill_sha256_is_deterministic_from_text() -> None:
    first = parse_skill_document("skills/vex-patterns/SKILL.md", SKILL_FIXTURE).entities[0]
    second = parse_skill_document("skills/other/SKILL.md", SKILL_FIXTURE).entities[0]
    expected = hashlib.sha256(SKILL_FIXTURE.encode("utf-8")).hexdigest()
    assert first.attributes["sha256"] == expected
    assert second.attributes["sha256"] == expected
    # Same text -> same hash, independent of path.
    assert first.entity_id != second.entity_id


def test_skill_frontmatter_removed_from_body() -> None:
    entity = parse_skill_document("skills/vex-patterns/SKILL.md", SKILL_FIXTURE).entities[0]
    assert "---" not in entity.body
    assert "name: vex-patterns" not in entity.body
    assert "description:" not in entity.body
    assert "# VEX Patterns" in entity.body
    assert entity.summary == "Reusable VEX snippets for procedural modeling."


def test_skill_sections_retained() -> None:
    entity = parse_skill_document("skills/vex-patterns/SKILL.md", SKILL_FIXTURE).entities[0]
    assert [s.key for s in entity.sections] == ["overview", "snippets"]


def test_skill_explicit_typed_links_create_unresolved_edges() -> None:
    parsed = parse_skill_document("skills/vex-patterns/SKILL.md", SKILL_FIXTURE)
    ref_edges = [e for e in parsed.edges if e.predicate == "references"]
    targets = {e.target_raw for e in ref_edges}
    assert "Vex:intersect" in targets
    assert "Node:sop/boolean" in targets
    for edge in ref_edges:
        assert edge.target_id is None
        assert edge.resolved is False


def test_skill_edge_source_location_uses_original_lines() -> None:
    text = (
        "---\nname: demo\ndescription: demo\n---\n\n"
        "# Demo\n\n## Links\nSee [Vex:intersect].\n"
    )
    parsed = parse_skill_document("skills/demo/SKILL.md", text)
    ref_edges = [e for e in parsed.edges if e.predicate == "references"]
    assert len(ref_edges) == 1
    assert ref_edges[0].source_location == "skills/demo/SKILL.md:9"
    assert ref_edges[0].target_raw == "Vex:intersect"


def test_skill_without_frontmatter_keeps_original_lines() -> None:
    text = "# Demo\n\n## Links\nSee [Vex:intersect].\n"
    parsed = parse_skill_document("skills/demo/SKILL.md", text)
    ref_edges = [e for e in parsed.edges if e.predicate == "references"]
    assert len(ref_edges) == 1
    assert ref_edges[0].source_location == "skills/demo/SKILL.md:4"


def test_skill_plain_prose_does_not_infer_edge() -> None:
    text = (
        "---\nname: plain\ndescription: plain skill.\n---\n\n"
        "# Plain\n\nYou can use the intersect function here.\n"
    )
    parsed = parse_skill_document("skills/plain/SKILL.md", text)
    assert parsed.edges == ()


def test_skill_parser_does_not_read_filesystem() -> None:
    parsed = parse_skill_document(
        "skills/does-not-exist/SKILL.md",
        "---\nname: ghost\ndescription: g.\n---\n\n# Ghost\n\nBody.\n",
    )
    assert parsed.entities[0].source_path == "skills/does-not-exist/SKILL.md"
    assert isinstance(parsed, ParsedDocument)
