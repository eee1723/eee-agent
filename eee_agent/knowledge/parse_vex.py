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
# @related may be inline ("@related intersect" / "@related [Vex:foo]") or a
# block ("@related" / "@related:") followed by "- [Kind:target]" list items.
_RELATED_RE = re.compile(r"^@related(?![A-Za-z0-9_]):?[ \t]*(?P<target>.*)$")
_LIST_ITEM_RE = re.compile(r"^-\s+\[")

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


def _related_target(inline: str) -> tuple[str, str | None]:
    """Return ``(target_raw, anchor)`` for an inline @related target.

    Explicit typed bracket syntax preserves the full typed target; a plain
    target keeps its plain spelling.
    """
    inline = inline.strip()
    if inline.startswith("["):
        for ref in parse_references(inline):
            if ref.target_kind == "Anchor":
                continue
            target_raw = (
                ref.raw_target
                if ref.target_kind == "Include"
                else f"{ref.target_kind}:{ref.raw_target}"
            )
            return target_raw, ref.anchor
    return inline, None


def _parse_related(
    text: str, logical_path: str, entity_id: str
) -> tuple[list[EdgeDraft], set[int]]:
    """Parse @related directives into unresolved related_to edges.

    Returns the edges and the set of source lines they occupy, so the same
    bracket references are not also emitted as ordinary references.
    """
    lines = text.split("\n")
    edges: list[EdgeDraft] = []
    related_lines: set[int] = set()
    index = 0
    while index < len(lines):
        match = _RELATED_RE.match(lines[index])
        if not match:
            index += 1
            continue
        line_number = index + 1
        related_lines.add(line_number)
        inline = match.group("target").strip()
        if inline:
            target_raw, anchor = _related_target(inline)
            edges.append(
                EdgeDraft(
                    source_id=entity_id,
                    predicate="related_to",
                    target_id=None,
                    target_raw=target_raw,
                    target_anchor=anchor,
                    resolved=False,
                    source_location=f"{logical_path}:{line_number}",
                )
            )
            index += 1
            continue
        # Block form: consume blank lines and "- [Kind:target]" list items.
        index += 1
        while index < len(lines):
            stripped = lines[index].strip()
            if _LIST_ITEM_RE.match(stripped):
                related_lines.add(index + 1)
                for ref in parse_references(lines[index]):
                    if ref.target_kind == "Anchor":
                        continue
                    target_raw = (
                        ref.raw_target
                        if ref.target_kind == "Include"
                        else f"{ref.target_kind}:{ref.raw_target}"
                    )
                    edges.append(
                        EdgeDraft(
                            source_id=entity_id,
                            predicate="related_to",
                            target_id=None,
                            target_raw=target_raw,
                            target_anchor=ref.anchor,
                            resolved=False,
                            source_location=f"{logical_path}:{index + 1}",
                        )
                    )
                index += 1
            elif stripped == "":
                related_lines.add(index + 1)
                index += 1
            else:
                break
    return edges, related_lines


def _build_edges(
    entity_id: str, logical_path: str, text: str
) -> list[EdgeDraft]:
    edges, related_lines = _parse_related(text, logical_path, entity_id)
    for ref in parse_references(text):
        # A bracket inside an @related block is represented by the related_to
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
    lines = text.split("\n")
    index = 0
    while index < len(lines):
        line = lines[index]
        usage = _USAGE_RE.match(line)
        if usage:
            value = usage.group("value").strip()
            if value:
                signatures.append(value)
            index += 1
            continue
        ret = _RETURNS_RE.match(line)
        if ret:
            inline = ret.group("value").strip()
            parts = [inline] if inline else []
            index += 1
            # Block form: indented continuation lines until a non-indented line.
            while index < len(lines) and (
                lines[index].startswith(" ") or lines[index].startswith("\t")
            ):
                continuation = lines[index].strip()
                if continuation:
                    parts.append(continuation)
                index += 1
            returns = "\n".join(parts)
            continue
        index += 1

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
