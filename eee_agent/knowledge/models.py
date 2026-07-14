# eee_agent/knowledge/models.py
from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Mapping


class Authority(StrEnum):
    OFFICIAL_HOUDINI_DOCS = "official_houdini_docs"
    PROJECT_VERIFIED_SKILL = "project_verified_skill"


class EntityKind(StrEnum):
    NODE_DOCUMENT = "node_document"
    VEX_FUNCTION = "vex_function"
    HOM_CLASS = "hom_class"
    HOM_METHOD = "hom_method"
    HOM_FUNCTION = "hom_function"
    HOM_MODULE = "hom_module"
    HOM_PACKAGE = "hom_package"
    SKILL_REFERENCE = "skill_reference"


class OperatorTypeStatus(StrEnum):
    VERIFIED_AT_BUILD = "verified_at_build"
    DOCUMENTED_UNVERIFIED = "documented_unverified"
    AMBIGUOUS = "ambiguous"
    HISTORICAL_ONLY = "historical_only"
    UNRESOLVED = "unresolved"


@dataclass(frozen=True, slots=True)
class SectionDraft:
    key: str
    heading: str
    body: str
    ordinal: int


@dataclass(frozen=True, slots=True)
class ReferenceDraft:
    display_text: str | None
    target_kind: str
    raw_target: str
    normalized_target: str
    anchor: str | None
    source_line: int


@dataclass(frozen=True, slots=True)
class EntityDraft:
    entity_id: str
    kind: EntityKind
    subtype: str
    canonical_name: str
    title: str
    summary: str
    authority: Authority
    source_path: str
    source_anchor: str | None
    is_current: bool
    attributes: Mapping[str, object] = field(default_factory=dict)
    body: str = ""
    sections: tuple[SectionDraft, ...] = ()


@dataclass(frozen=True, slots=True)
class AliasDraft:
    alias: str
    entity_id: str
    alias_type: str
    priority: int


@dataclass(frozen=True, slots=True)
class EdgeDraft:
    source_id: str
    predicate: str
    target_id: str | None
    target_raw: str | None
    target_anchor: str | None
    resolved: bool
    source_location: str


@dataclass(frozen=True, slots=True)
class ParsedDocument:
    entities: tuple[EntityDraft, ...]
    aliases: tuple[AliasDraft, ...]
    edges: tuple[EdgeDraft, ...]


@dataclass(frozen=True, slots=True)
class GraphBundle:
    entities: tuple[EntityDraft, ...]
    aliases: tuple[AliasDraft, ...]
    edges: tuple[EdgeDraft, ...]
