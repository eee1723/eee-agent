"""SOP node-document parser.

Pure and I/O-free: the only inputs are ``source_path`` and ``text``. Document
identity is derived from the normalized logical source path and a
filename-based document version, never from ``#internal``. ``#internal`` is
kept as low-trust metadata only. No hython inventory is available here, so no
candidate is ever marked ``VERIFIED_AT_BUILD``.
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
    OperatorTypeStatus,
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

__all__ = ["parse_node_document"]

# A numeric version suffix, e.g. the "-2.0" in "agentlookat-2.0".
_VERSION_SUFFIX_RE = re.compile(r"^(.*)-([0-9]+\.[0-9]+)$")

# Parser-level alias priorities. Stage 3 may introduce verified operator types
# at a higher priority; here every candidate is documented-unverified.
_PRIORITY_OPERATOR_TYPE = 100
_PRIORITY_DOCUMENT_SLUG = 80
_PRIORITY_TITLE = 60
_PRIORITY_INTERNAL = 20

_NAMED_METADATA = {"type", "context", "namespace", "internal", "tags"}


def _basename_stem(logical_path: str) -> str:
    """Return the filename stem (no directory, no extension) as a slug."""
    basename = logical_path.rsplit("/", 1)[-1]
    if "." in basename:
        return basename.rsplit(".", 1)[0]
    return basename


def _derive_identity(stem: str) -> tuple[str | None, str, str | None, bool]:
    """Split a filename stem into (namespace, name, version, is_legacy).

    ``--`` separates the namespace from the rest and is never a version
    separator. A final ``-[0-9]+\\.[0-9]+`` is a historical version; a final
    ``-`` with no number is a legacy page; any other hyphen is part of the slug.
    """
    namespace: str | None = None
    name_part = stem
    if "--" in stem:
        namespace, name_part = stem.split("--", 1)

    version: str | None = None
    is_legacy = False
    match = _VERSION_SUFFIX_RE.match(name_part)
    if match:
        name = match.group(1)
        version = match.group(2)
    elif name_part.endswith("-"):
        name = name_part[:-1]
        is_legacy = True
    else:
        name = name_part
    return namespace, name, version, is_legacy


def _operator(namespace: str | None, name: str, version: str | None) -> str:
    parts: list[str] = []
    if namespace:
        parts.append(namespace)
    parts.append(name)
    if version:
        parts.append(version)
    return "::".join(parts)


def _dedupe(items: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _split_tags(value: str) -> tuple[str, ...]:
    return tuple(token for token in (s.strip() for s in re.split(r"[,\n]", value)) if token)


def _build_aliases(
    entity_id: str,
    stem: str,
    title: str,
    filename_candidates: list[str],
    internal: str,
) -> list[AliasDraft]:
    aliases: list[AliasDraft] = []
    for candidate in _dedupe(filename_candidates):
        aliases.append(
            AliasDraft(
                alias=candidate,
                entity_id=entity_id,
                alias_type="operator_type",
                priority=_PRIORITY_OPERATOR_TYPE,
            )
        )
    aliases.append(
        AliasDraft(
            alias=stem,
            entity_id=entity_id,
            alias_type="document_slug",
            priority=_PRIORITY_DOCUMENT_SLUG,
        )
    )
    if title:
        aliases.append(
            AliasDraft(
                alias=title,
                entity_id=entity_id,
                alias_type="title",
                priority=_PRIORITY_TITLE,
            )
        )
    if internal:
        aliases.append(
            AliasDraft(
                alias=internal,
                entity_id=entity_id,
                alias_type="internal_metadata",
                priority=_PRIORITY_INTERNAL,
            )
        )
    return aliases


def _build_edges(
    entity_id: str, logical_path: str, references
) -> list[EdgeDraft]:
    edges: list[EdgeDraft] = []
    for ref in references:
        if ref.target_kind == "Include":
            predicate = "includes"
            target_raw = ref.raw_target
        elif ref.target_kind == "Anchor":
            # Local anchor on the same page; no cross-entity target.
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


def parse_node_document(source_path: str, text: str) -> ParsedDocument:
    """Parse a typed SOP node-document page into a single node_document entity."""
    logical_path = normalize_source_path(source_path)
    metadata = parse_metadata(text)
    stem = _basename_stem(logical_path)
    namespace, name, version, is_legacy = _derive_identity(stem)

    if version is not None:
        document_version = version
        is_current = False
        status = OperatorTypeStatus.HISTORICAL_ONLY
    elif is_legacy:
        document_version = "legacy"
        is_current = False
        status = OperatorTypeStatus.HISTORICAL_ONLY
    else:
        document_version = "current"
        is_current = True
        status = OperatorTypeStatus.DOCUMENTED_UNVERIFIED

    # The operator candidate version is separate from document identity. The
    # filename namespace/version win; otherwise fall back to the documented
    # #namespace/#version. canonical_name and the namespace attribute both use
    # this single effective namespace.
    effective_namespace = (
        namespace
        if namespace is not None
        else (metadata.get("namespace", "") or None)
    )
    effective_version = (
        version
        if version is not None
        else (metadata.get("version", "") or None)
    )

    # Candidate order: namespace::name::version, namespace::name, then #internal.
    candidate_forms: list[str] = []
    if effective_version is not None:
        candidate_forms.append(_operator(effective_namespace, name, effective_version))
    candidate_forms.append(_operator(effective_namespace, name, None))
    internal = metadata.get("internal", "")
    operator_candidates = tuple(
        _dedupe(candidate_forms + ([internal] if internal else []))
    )

    context = metadata.get("context", "")
    namespace_attr = effective_namespace if effective_namespace is not None else ""
    title = parse_title(text)
    summary = parse_summary(text)
    entity_id = make_entity_id(
        EntityKind.NODE_DOCUMENT, f"{logical_path}@{document_version}"
    )

    attributes: dict[str, object] = {
        "context": context,
        "namespace": namespace_attr,
        "internal_metadata": internal,
        "document_version": document_version,
        "is_current_document": is_current,
        "operator_type_candidates": operator_candidates,
        "operator_type_status": status,
    }
    if "tags" in metadata:
        attributes["tags"] = _split_tags(metadata["tags"])
    for key, value in metadata.items():
        if key not in _NAMED_METADATA:
            attributes.setdefault(key, value)

    entity = EntityDraft(
        entity_id=entity_id,
        kind=EntityKind.NODE_DOCUMENT,
        subtype=context,
        canonical_name=_operator(effective_namespace, name, None),
        title=title,
        summary=summary,
        authority=Authority.OFFICIAL_HOUDINI_DOCS,
        source_path=logical_path,
        source_anchor=None,
        is_current=is_current,
        attributes=attributes,
        body=clean_body(text),
        sections=parse_sections(text),
    )

    aliases = _build_aliases(entity_id, stem, title, candidate_forms, internal)
    edges = _build_edges(entity_id, logical_path, parse_references(text))

    return ParsedDocument(
        entities=(entity,),
        aliases=tuple(aliases),
        edges=tuple(edges),
    )
