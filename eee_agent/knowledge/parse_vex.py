"""VEX function-document parser.

Pure and I/O-free: the only inputs are ``source_path`` and ``text``. Only
``#type: vex`` pages that are not the shared ``_common`` page become
``VEX_FUNCTION`` entities. All ``:usage:`` signatures are preserved in
document order; ``@related`` targets become unresolved ``related_to`` edges
and typed cross-references become unresolved ``references`` edges.
"""

from __future__ import annotations

import re

from eee_agent.knowledge.ids import make_entity_id, normalize_source_path
from eee_agent.knowledge.models import (
    AliasDraft,
    Authority,
    EdgeDraft,
    EntityDraft,
    EntityKind,
    ParsedDocument,
)
from eee_agent.knowledge.parse_common import (
    clean_body,
    parse_metadata,
    parse_references,
    parse_sections,
    parse_summary,
    parse_title,
)

__all__ = ["parse_vex_document"]

_USAGE_RE = re.compile(r"^:usage:\s*(?P<value>.*)$")
_RETURNS_RE = re.compile(r"^:returns:\s*(?P<value>.*)$")
_RELATED_RE = re.compile(r"^@related\s+(?P<target>.+?)\s*$")

_PRIORITY_QUALIFIED = 100
_PRIORITY_TITLE = 60

_NAMED_METADATA = {"type", "context", "group", "tags"}
_EMPTY = ParsedDocument(entities=(), aliases=(), edges=())


def _basename_stem(logical_path: str) -> str:
    basename = logical_path.rsplit("/", 1)[-1]
    if "." in basename:
        return basename.rsplit(".", 1)[0]
    return basename


def _split_tags(value: str) -> tuple[str, ...]:
    return tuple(token for token in (s.strip() for s in re.split(r"[,\n]", value)) if token)


def _build_edges(
    entity_id: str, logical_path: str, text: str
) -> list[EdgeDraft]:
    edges: list[EdgeDraft] = []
    related_lines: set[int] = set()
    for line_number, line in enumerate(text.split("\n"), start=1):
        if not line.startswith("@related"):
            continue
        related_lines.add(line_number)
        match = _RELATED_RE.match(line)
        if match:
            edges.append(
                EdgeDraft(
                    source_id=entity_id,
                    predicate="related_to",
                    target_id=None,
                    target_raw=match.group("target").strip(),
                    target_anchor=None,
                    resolved=False,
                    source_location=f"{logical_path}:{line_number}",
                )
            )

    for ref in parse_references(text):
        # A bracket inside an @related line is represented by the related_to
        # edge above, not duplicated as an ordinary reference.
        if ref.source_line in related_lines:
            continue
        if ref.target_kind == "Include":
            predicate = "includes"
            target_raw = ref.raw_target
        elif ref.target_kind == "Anchor":
            predicate = "references"
            target_raw = f"#{ref.anchor}" if ref.anchor else ""
        else:
            predicate = "references"
            target_raw = f"{ref.target_kind}:{ref.raw_target}"
        edges.append(
            EdgeDraft(
                source_id=entity_id,
                predicate=predicate,
                target_id=None,
                target_raw=target_raw,
                target_anchor=ref.anchor,
                resolved=False,
                source_location=f"{logical_path}:{ref.source_line}",
            )
        )
    return edges


def parse_vex_document(source_path: str, text: str) -> ParsedDocument:
    """Parse a VEX documentation page into a single vex_function entity."""
    logical_path = normalize_source_path(source_path)
    metadata = parse_metadata(text)
    stem = _basename_stem(logical_path)

    if metadata.get("type", "") != "vex" or stem == "_common":
        return _EMPTY

    title = parse_title(text)
    summary = parse_summary(text)
    entity_id = make_entity_id(EntityKind.VEX_FUNCTION, stem)

    signatures: list[str] = []
    returns = ""
    for line in text.split("\n"):
        usage = _USAGE_RE.match(line)
        if usage:
            value = usage.group("value").strip()
            if value:
                signatures.append(value)
            continue
        ret = _RETURNS_RE.match(line)
        if ret:
            returns = ret.group("value").strip()

    attributes: dict[str, object] = {
        "context": metadata.get("context", ""),
        "group": metadata.get("group", ""),
        "signatures": tuple(signatures),
        "returns": returns,
    }
    if "tags" in metadata:
        attributes["tags"] = _split_tags(metadata["tags"])
    for key, value in metadata.items():
        if key not in _NAMED_METADATA:
            attributes.setdefault(key, value)

    entity = EntityDraft(
        entity_id=entity_id,
        kind=EntityKind.VEX_FUNCTION,
        subtype=metadata.get("context", ""),
        canonical_name=stem,
        title=title,
        summary=summary,
        authority=Authority.OFFICIAL_HOUDINI_DOCS,
        source_path=logical_path,
        source_anchor=None,
        is_current=True,
        attributes=attributes,
        body=clean_body(text),
        sections=parse_sections(text),
    )

    aliases = [
        AliasDraft(
            alias=stem,
            entity_id=entity_id,
            alias_type="qualified_name",
            priority=_PRIORITY_QUALIFIED,
        )
    ]
    if title:
        aliases.append(
            AliasDraft(
                alias=title,
                entity_id=entity_id,
                alias_type="title",
                priority=_PRIORITY_TITLE,
            )
        )

    edges = _build_edges(entity_id, logical_path, text)

    return ParsedDocument(
        entities=(entity,),
        aliases=tuple(aliases),
        edges=tuple(edges),
    )
