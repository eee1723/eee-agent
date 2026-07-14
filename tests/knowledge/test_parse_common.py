"""Tests for the common Houdini Creole document grammar parser.

All fixtures are synthetic minimal text — no real SideFX document bodies.
"""

import pytest

from eee_agent.knowledge.parse_common import (
    clean_body,
    decode_source,
    parse_metadata,
    parse_references,
    parse_sections,
    parse_summary,
    parse_title,
)


SOURCE = """﻿= Example =
#type: node
#tags: model,
    polygons
\"\"\"A short
summary.\"\"\"
== Overview == (overview)
See [Boolean SOP|Node:sop/boolean] and [Hom:hou.Node#createNode].
:include _common#geometry:
== Overview == (advanced)
Advanced text.
"""


def test_decode_source_strips_bom() -> None:
    text = decode_source("﻿= Title =".encode("utf-8"))
    assert text == "= Title ="
    assert not text.startswith("﻿")


def test_decode_source_rejects_invalid_utf8() -> None:
    with pytest.raises(UnicodeDecodeError):
        decode_source(b"\xff\xfe\x00bad bytes")


def test_common_document_fields_and_sections() -> None:
    text = decode_source(SOURCE.encode("utf-8"))
    assert parse_title(text) == "Example"
    assert parse_metadata(text)["tags"] == "model,\npolygons"
    assert parse_summary(text) == "A short summary."
    assert [section.key for section in parse_sections(text)] == [
        "overview", "advanced"
    ]


def test_metadata_keys_and_continuation() -> None:
    text = decode_source(
        b"= T =\n#type: node\n#context: sop\n#tags: a,\n    b\n"
    )
    md = parse_metadata(text)
    assert md == {"type": "node", "context": "sop", "tags": "a,\nb"}


def test_title_absent_returns_empty() -> None:
    assert parse_title(decode_source(b"#type: node\nNo title.\n")) == ""


def test_summary_absent_returns_empty() -> None:
    assert parse_summary(decode_source(b"= T =\n#type: node\nNo summary.\n")) == ""


def test_section_uses_explicit_anchor_as_key() -> None:
    text = decode_source(b"= T =\n== Overview == (custom)\nBody.\n")
    sections = parse_sections(text)
    assert len(sections) == 1
    assert sections[0].key == "custom"
    assert sections[0].heading == "Overview"
    assert sections[0].body == "Body."
    assert sections[0].ordinal == 0


def test_duplicate_slugified_section_keys_get_suffixes() -> None:
    text = decode_source(b"= T =\n== Overview ==\nFirst.\n== Overview ==\nSecond.\n")
    sections = parse_sections(text)
    assert [s.key for s in sections] == ["overview", "overview-2"]
    assert [s.ordinal for s in sections] == [0, 1]
    assert sections[0].body == "First."
    assert sections[1].body == "Second."


def test_section_body_spans_until_next_heading() -> None:
    text = decode_source(SOURCE.encode("utf-8"))
    sections = parse_sections(text)
    assert "See [Boolean SOP|Node:sop/boolean]" in sections[0].body
    assert ":include _common#geometry:" in sections[0].body
    assert sections[1].body == "Advanced text."


def test_labeled_anchor_and_include_references() -> None:
    refs = parse_references(decode_source(SOURCE.encode("utf-8")))
    assert [(r.target_kind, r.normalized_target, r.anchor) for r in refs] == [
        ("Node", "sop/boolean", None),
        ("Hom", "hou.Node", "createNode"),
        ("Include", "_common", "geometry"),
    ]


def test_reference_display_text_and_raw_target() -> None:
    refs = parse_references(decode_source(SOURCE.encode("utf-8")))
    node, hom, include = refs
    assert node.display_text == "Boolean SOP"
    assert node.raw_target == "sop/boolean"
    assert hom.display_text is None
    assert hom.raw_target == "hou.Node#createNode"
    assert include.display_text is None
    assert include.raw_target == "_common#geometry"


def test_reference_source_lines_are_one_based() -> None:
    refs = parse_references(decode_source(SOURCE.encode("utf-8")))
    # Node and Hom are on line 8; the include is on line 9.
    assert [r.source_line for r in refs] == [8, 8, 9]


def test_labeled_reference_not_duplicated_as_direct() -> None:
    text = decode_source(b"= T =\nSee [Boolean SOP|Node:sop/boolean].\n")
    refs = parse_references(text)
    assert len(refs) == 1
    assert refs[0].target_kind == "Node"
    assert refs[0].normalized_target == "sop/boolean"
    assert refs[0].display_text == "Boolean SOP"


def test_direct_vex_reference() -> None:
    refs = parse_references(decode_source(b"= T =\nSee [Vex:intersect].\n"))
    assert len(refs) == 1
    r = refs[0]
    assert r.target_kind == "Vex"
    assert r.normalized_target == "intersect"
    assert r.anchor is None
    assert r.display_text is None
    assert r.raw_target == "intersect"


def test_local_anchor_reference() -> None:
    # A local anchor ``[display|#anchor]`` points at an anchor on the same
    # page; it has no cross-entity target, so normalized_target is empty.
    refs = parse_references(decode_source(b"= T =\nUse [node()|#node].\n"))
    assert len(refs) == 1
    r = refs[0]
    assert r.target_kind == "Anchor"
    assert r.anchor == "node"
    assert r.display_text == "node()"
    assert r.normalized_target == ""


def test_references_returned_as_tuple() -> None:
    refs = parse_references(decode_source(b"= T =\n[Vex:intersect]\n"))
    assert isinstance(refs, tuple)


def test_clean_body_removes_directives_and_preserves_content() -> None:
    body = (
        "== Overview ==\n"
        "Some prose.\n"
        ":fig: image.png\n"
        ":usage: int foo(int x)\n"
        "\n\n\n"
        "More prose.\n"
    )
    cleaned = clean_body(body)
    assert "== Overview ==" in cleaned
    assert "Some prose." in cleaned
    assert ":usage: int foo(int x)" in cleaned
    assert "More prose." in cleaned
    assert ":fig:" not in cleaned
    assert "\n\n\n" not in cleaned


def test_clean_body_preserves_signatures_and_arguments() -> None:
    body = (
        "== Signatures ==\n"
        "@usage int clamp(int value; int min; int max)\n"
        "Argument ``value`` is required.\n"
    )
    cleaned = clean_body(body)
    assert "@usage int clamp(int value; int min; int max)" in cleaned
    assert "Argument ``value`` is required." in cleaned


@pytest.mark.parametrize(
    "indent",
    ["    ", "\t", "        "],
    ids=["spaces", "tab", "eight-spaces"],
)
def test_indented_include_directive_is_parsed(indent: str) -> None:
    # Real Houdini docs indent include directives; they must still be parsed.
    text = decode_source(
        f"= Title =\n{indent}:include _common#geometry:\n".encode("utf-8")
    )
    refs = parse_references(text)
    assert len(refs) == 1
    r = refs[0]
    assert r.target_kind == "Include"
    assert r.raw_target == "_common#geometry"
    assert r.normalized_target == "_common"
    assert r.anchor == "geometry"
    assert r.source_line == 2


@pytest.mark.parametrize("indent", ["", "    "], ids=["unindented", "indented"])
def test_clean_body_removes_vimeo_directive(indent: str) -> None:
    # Real Houdini docs use :vimeo: as a video-only directive; the whole
    # directive line (including its argument) must be dropped from the body.
    body = f"Some prose.\n{indent}:vimeo: 123456789\nMore prose.\n"
    cleaned = clean_body(body)
    assert ":vimeo:" not in cleaned
    assert "123456789" not in cleaned
    assert "Some prose." in cleaned
    assert "More prose." in cleaned


def test_parse_title_accepts_spaced_form() -> None:
    assert parse_title("= hou.Node =") == "hou.Node"


def test_parse_title_accepts_compact_form() -> None:
    # Real HOM class pages use compact titles with no space before the
    # closing delimiter, e.g. "= hou.Drawable2D=".
    assert parse_title("= hou.Drawable2D=") == "hou.Drawable2D"


def test_parse_title_still_rejects_section_headings() -> None:
    assert parse_title("== Overview ==") == ""
    assert parse_title("=== Sub ===") == ""
