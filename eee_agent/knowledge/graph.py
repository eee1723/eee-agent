"""Two-pass knowledge-graph assembly, node reconciliation and edge resolution.

Pure and I/O-free: the only input is a tuple of :class:`ParsedDocument` and a
SOP operator inventory. Entities/aliases/edges are rebuilt as new immutable
DTO instances; inputs are never mutated. Final tuples are sorted by stable
explicit keys so equal logical inputs produce an equal :class:`GraphBundle`
regardless of input order.
"""

from __future__ import annotations

import re
from typing import Mapping

from eee_agent.knowledge.models import (
    AliasDraft,
    EdgeDraft,
    EntityDraft,
    EntityKind,
    GraphBundle,
    OperatorTypeStatus,
    ParsedDocument,
)

__all__ = ["GraphError", "assemble_graph", "validate_graph"]

_PRIORITY_VERIFIED_OPERATOR = 200
_PRIORITY_FILENAME_ALIAS = 70

_TYPED_RE = re.compile(r"^(?P<kind>[A-Za-z][\w]*):(?P<target>.+)$")

_HOM_KINDS = frozenset({
    EntityKind.HOM_CLASS,
    EntityKind.HOM_METHOD,
    EntityKind.HOM_FUNCTION,
    EntityKind.HOM_MODULE,
    EntityKind.HOM_PACKAGE,
})
_ALL_KINDS = frozenset(EntityKind)
_KIND_BY_PREFIX = {
    "node": frozenset({EntityKind.NODE_DOCUMENT}),
    "vex": frozenset({EntityKind.VEX_FUNCTION}),
    "hom": _HOM_KINDS,
}


class GraphError(Exception):
    """A graph invariant was violated during assembly or validation."""


def _kinds_for_prefix(kind: str) -> frozenset[EntityKind]:
    return _KIND_BY_PREFIX.get(kind.casefold(), frozenset())


def _strip_extension(source_path: str) -> str:
    basename = source_path.rsplit("/", 1)[-1]
    if "." in basename:
        return source_path.rsplit(".", 1)[0]
    return source_path


def _reconcile_node(
    entity: EntityDraft, inventory: frozenset[str]
) -> tuple[EntityDraft, str | None]:
    """Return (new entity with reconciled operator attrs, verified operator or None)."""
    attrs = dict(entity.attributes)
    candidates = attrs.get("operator_type_candidates", ())
    verified: str | None = None
    if entity.is_current:
        for candidate in candidates:
            if candidate in inventory:  # exact, case-sensitive
                verified = candidate
                break
        if verified is not None:
            attrs["operator_type"] = verified
            attrs["operator_type_status"] = OperatorTypeStatus.VERIFIED_AT_BUILD
        else:
            attrs["operator_type"] = None
            attrs["operator_type_status"] = OperatorTypeStatus.UNRESOLVED
    else:
        attrs["operator_type"] = None
        attrs["operator_type_status"] = OperatorTypeStatus.HISTORICAL_ONLY
        verified = None
    new_entity = EntityDraft(
        entity_id=entity.entity_id,
        kind=entity.kind,
        subtype=entity.subtype,
        canonical_name=entity.canonical_name,
        title=entity.title,
        summary=entity.summary,
        authority=entity.authority,
        source_path=entity.source_path,
        source_anchor=entity.source_anchor,
        is_current=entity.is_current,
        attributes=attrs,
        body=entity.body,
        sections=entity.sections,
    )
    return new_entity, verified


def _retained_versioned_operator(entity: EntityDraft) -> str | None:
    """The single versioned operator_type alias a non-current node may keep.

    A numeric historical page keeps only ``canonical_name::document_version``;
    legacy pages (``document_version == "legacy"``) keep none, because no
    versioned form exists and the unversioned alias would collide with the
    current operator of the same name.
    """
    document_version = entity.attributes.get("document_version")
    if document_version in (None, "current", "legacy"):
        return None
    return f"{entity.canonical_name}::{document_version}"


def _isolate_historical_operator_aliases(
    aliases: list[AliasDraft],
    entities_by_id: Mapping[str, EntityDraft],
) -> list[AliasDraft]:
    """Drop unversioned operator_type aliases from non-current node documents.

    A non-current (historical/legacy) node document must not retain an
    unversioned ``operator_type`` alias that could masquerade as the current
    operator of the same name: a numeric historical page keeps only its
    explicitly-versioned operator, and a legacy page keeps none. Current
    entities and every other alias type are untouched, so parallel
    top-priority ambiguity between distinct current entities is preserved.
    """
    isolated: list[AliasDraft] = []
    for alias in aliases:
        if alias.alias_type == "operator_type":
            owner = entities_by_id.get(alias.entity_id)
            if (
                owner is not None
                and owner.kind == EntityKind.NODE_DOCUMENT
                and not owner.is_current
            ):
                retained = _retained_versioned_operator(owner)
                if alias.alias != retained:
                    continue
        isolated.append(alias)
    return isolated


def _dedupe_aliases(aliases: list[AliasDraft]) -> list[AliasDraft]:
    best: dict[tuple[str, str, str], AliasDraft] = {}
    for alias in aliases:
        key = (alias.alias, alias.entity_id, alias.alias_type)
        existing = best.get(key)
        if existing is None or alias.priority > existing.priority:
            best[key] = alias
    return list(best.values())


def _alias_sort_key(alias: AliasDraft) -> tuple:
    return (alias.alias, alias.alias_type, -alias.priority, alias.entity_id)


def _edge_sort_key(edge: EdgeDraft) -> tuple:
    return (
        edge.source_id,
        edge.predicate,
        edge.source_location,
        edge.target_raw or "",
        edge.target_id or "",
        edge.target_anchor or "",
    )


def _dedupe_edges(edges: list[EdgeDraft]) -> list[EdgeDraft]:
    seen: set[tuple] = set()
    deduped: list[EdgeDraft] = []
    for edge in edges:
        key = (
            edge.source_id,
            edge.predicate,
            edge.target_id,
            edge.target_raw,
            edge.target_anchor,
            edge.resolved,
            edge.source_location,
        )
        if key in seen:
            continue
        seen.add(key)
        deduped.append(edge)
    return deduped


def _resolve_by_alias(
    value: str,
    allowed_kinds: frozenset[EntityKind],
    alias_index: dict[str, list[AliasDraft]],
    entities_by_id: dict[str, EntityDraft],
) -> str | None:
    candidates = alias_index.get(value, [])
    filtered = [
        a for a in candidates
        if a.entity_id in entities_by_id
        and entities_by_id[a.entity_id].kind in allowed_kinds
    ]
    if not filtered:
        return None
    max_priority = max(a.priority for a in filtered)
    top = {a.entity_id for a in filtered if a.priority == max_priority}
    if len(top) == 1:
        return next(iter(top))
    return None


def _split_typed(target_raw: str) -> tuple[str | None, str]:
    match = _TYPED_RE.match(target_raw)
    if match:
        return match.group("kind"), match.group("target")
    return None, target_raw


def _context_kinds(source: EntityDraft | None) -> frozenset[EntityKind]:
    if source is None:
        return frozenset()
    if source.kind == EntityKind.VEX_FUNCTION:
        return frozenset({EntityKind.VEX_FUNCTION})
    if source.kind in _HOM_KINDS:
        return _HOM_KINDS
    if source.kind == EntityKind.NODE_DOCUMENT:
        return frozenset({EntityKind.NODE_DOCUMENT})
    return frozenset()


def _resolve_typed(
    kind: str,
    target: str,
    alias_index: dict[str, list[AliasDraft]],
    entities_by_id: dict[str, EntityDraft],
) -> str | None:
    allowed = _kinds_for_prefix(kind)
    if not allowed:
        return None
    # A HOM method target with a #anchor prefers the first-class method entity;
    # only if no method matches do we fall back to the class (base) entity.
    if kind.casefold() == "hom" and "#" in target:
        base = target.split("#", 1)[0]
        method = _resolve_by_alias(target, allowed, alias_index, entities_by_id)
        if method is not None:
            return method
        return _resolve_by_alias(base, allowed, alias_index, entities_by_id)
    return _resolve_by_alias(target, allowed, alias_index, entities_by_id)


def _resolve_edge(
    edge: EdgeDraft,
    entities_by_id: dict[str, EntityDraft],
    alias_index: dict[str, list[AliasDraft]],
) -> str | None:
    target_raw = edge.target_raw or ""
    if edge.resolved:
        return edge.target_id
    if edge.predicate == "references":
        if target_raw.startswith("#"):
            return None  # local same-document anchor: never guess
        kind, target = _split_typed(target_raw)
        if kind is None:
            return None
        return _resolve_typed(kind, target, alias_index, entities_by_id)
    if edge.predicate == "includes":
        target = target_raw.split("#", 1)[0]
        return _resolve_by_alias(target, _ALL_KINDS, alias_index, entities_by_id)
    if edge.predicate == "inherits_from":
        return _resolve_by_alias(target_raw, _HOM_KINDS, alias_index, entities_by_id)
    if edge.predicate == "related_to":
        kind, target = _split_typed(target_raw)
        if kind is not None:
            return _resolve_typed(kind, target, alias_index, entities_by_id)
        return _resolve_by_alias(
            target_raw,
            _context_kinds(entities_by_id.get(edge.source_id)),
            alias_index,
            entities_by_id,
        )
    return None


def assemble_graph(
    documents: tuple[ParsedDocument, ...],
    *,
    inventory: frozenset[str],
) -> GraphBundle:
    """Assemble a deterministic, resolved :class:`GraphBundle`.

    Pass 1 collects entities (rejecting duplicate IDs), reconciles node
    operator candidates against ``inventory``, and collects aliases (adding a
    highest-priority verified operator alias and a derived filename alias for
    nodes). Pass 2 resolves edges against the complete alias index, preserving
    unresolved and ambiguous targets explicitly. Edges are deduplicated by
    their full stable key and all tuples are sorted deterministically.
    """
    entities_by_id: dict[str, EntityDraft] = {}
    aliases: list[AliasDraft] = []
    edges: list[EdgeDraft] = []
    for document in documents:
        for entity in document.entities:
            if entity.entity_id in entities_by_id:
                raise GraphError(f"duplicate entity id: {entity.entity_id}")
            entities_by_id[entity.entity_id] = entity
        aliases.extend(document.aliases)
        edges.extend(document.edges)

    reconciled: dict[str, EntityDraft] = {}
    extra_aliases: list[AliasDraft] = []
    for entity_id, entity in entities_by_id.items():
        if entity.kind == EntityKind.NODE_DOCUMENT:
            new_entity, verified = _reconcile_node(entity, inventory)
            reconciled[entity_id] = new_entity
            if verified is not None:
                extra_aliases.append(
                    AliasDraft(
                        alias=verified,
                        entity_id=entity_id,
                        alias_type="operator_type",
                        priority=_PRIORITY_VERIFIED_OPERATOR,
                    )
                )
            filename_alias = _strip_extension(entity.source_path)
            if filename_alias:
                extra_aliases.append(
                    AliasDraft(
                        alias=filename_alias,
                        entity_id=entity_id,
                        alias_type="filename_alias",
                        priority=_PRIORITY_FILENAME_ALIAS,
                    )
                )
        else:
            reconciled[entity_id] = entity

    deduped_aliases = _dedupe_aliases(aliases + extra_aliases)
    isolated_aliases = _isolate_historical_operator_aliases(
        deduped_aliases, reconciled
    )
    alias_index: dict[str, list[AliasDraft]] = {}
    for alias in isolated_aliases:
        alias_index.setdefault(alias.alias, []).append(alias)

    resolved_edges: list[EdgeDraft] = []
    for edge in edges:
        target_id = _resolve_edge(edge, reconciled, alias_index)
        if target_id is not None and not edge.resolved:
            resolved_edges.append(
                EdgeDraft(
                    source_id=edge.source_id,
                    predicate=edge.predicate,
                    target_id=target_id,
                    target_raw=edge.target_raw,
                    target_anchor=edge.target_anchor,
                    resolved=True,
                    source_location=edge.source_location,
                )
            )
        else:
            resolved_edges.append(edge)

    final_entities = tuple(sorted(reconciled.values(), key=lambda e: e.entity_id))
    final_aliases = tuple(sorted(isolated_aliases, key=_alias_sort_key))
    final_edges = tuple(sorted(_dedupe_edges(resolved_edges), key=_edge_sort_key))

    bundle = GraphBundle(
        entities=final_entities,
        aliases=final_aliases,
        edges=final_edges,
    )
    validate_graph(bundle)
    return bundle


def validate_graph(bundle: GraphBundle) -> None:
    """Raise :class:`GraphError` if any graph invariant is violated."""
    entity_ids: set[str] = set()
    for entity in bundle.entities:
        if entity.entity_id in entity_ids:
            raise GraphError(f"duplicate entity id: {entity.entity_id}")
        entity_ids.add(entity.entity_id)

    for alias in bundle.aliases:
        if alias.entity_id not in entity_ids:
            raise GraphError(f"alias references unknown entity: {alias.entity_id}")

    for edge in bundle.edges:
        if edge.source_id not in entity_ids:
            raise GraphError(f"edge source unknown: {edge.source_id}")
        if edge.resolved:
            if edge.target_id is None:
                raise GraphError(f"resolved edge with target_id=None: {edge}")
            if edge.target_id not in entity_ids:
                raise GraphError(f"resolved edge target unknown: {edge.target_id}")
        else:
            if edge.target_id is not None:
                raise GraphError(
                    f"unresolved edge carries target_id={edge.target_id}"
                )
