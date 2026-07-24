"""Stable request/response contracts and error codes for the knowledge service.

All DTOs are frozen and serialize through ``to_dict()``. ``KnowledgeService``
returns these directly; the Runtime facade (``eee_agent.runtime.knowledge``)
and the LangChain tool adapter (``eee_agent.runtime.agent_tools``) validate
inputs, convert DTOs to dicts, and map exceptions to these codes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

__all__ = [
    "CandidateSummary",
    "GetRequest",
    "GetResponse",
    "KbProvenance",
    "KnowledgeErrorCode",
    "KnowledgeStatus",
    "NeighborResponse",
    "NeighborSummary",
    "SearchRequest",
    "SearchResponse",
    "SearchResult",
    "SectionSummary",
]


class KnowledgeErrorCode(StrEnum):
    KB_DISABLED = "kb_disabled"
    KB_NOT_BUILT = "kb_not_built"
    KB_BUILDING = "kb_building"
    KB_STALE = "kb_stale"
    KB_SCHEMA_MISMATCH = "kb_schema_mismatch"
    KB_CORRUPT = "kb_corrupt"
    INVALID_ARGUMENT = "invalid_argument"
    UNKNOWN_ENTITY = "unknown_entity"
    NO_MATCH = "no_match"
    AMBIGUOUS_SYMBOL = "ambiguous_symbol"
    INTERNAL_ERROR = "internal_error"


@dataclass(frozen=True)
class KbProvenance:
    manifest_sha256: str | None = None
    houdini_build: str | None = None
    kb_schema_version: int | None = None

    def to_dict(self) -> dict:
        result: dict = {}
        if self.manifest_sha256 is not None:
            result["manifest_sha256"] = self.manifest_sha256
        if self.houdini_build is not None:
            result["houdini_build"] = self.houdini_build
        if self.kb_schema_version is not None:
            result["kb_schema_version"] = self.kb_schema_version
        return result


@dataclass(frozen=True)
class KnowledgeStatus:
    available: bool
    code: KnowledgeErrorCode | None
    provenance: KbProvenance
    message: str = ""

    def to_dict(self) -> dict:
        return {
            "available": self.available,
            "code": self.code.value if self.code is not None else None,
            "provenance": self.provenance.to_dict(),
            "message": self.message,
        }


@dataclass(frozen=True)
class SearchRequest:
    symbol: str | None = None
    query: str | None = None
    kinds: tuple[str, ...] = ()
    context: str | None = None
    tag: str | None = None
    superclass: str | None = None
    predicate: str | None = None
    direction: str = "outgoing"
    include_historical: bool = False
    limit: int = 5


@dataclass(frozen=True)
class GetRequest:
    entity_id: str
    section: str | None = None
    max_chars: int = 4000


@dataclass(frozen=True)
class NeighborSummary:
    entity_id: str | None
    predicate: str
    target_raw: str | None
    target_anchor: str | None
    resolved: bool

    def to_dict(self) -> dict:
        return {
            "entity_id": self.entity_id,
            "predicate": self.predicate,
            "target_raw": self.target_raw,
            "target_anchor": self.target_anchor,
            "resolved": self.resolved,
        }


@dataclass(frozen=True)
class CandidateSummary:
    entity_id: str
    kind: str
    canonical_name: str
    title: str

    def to_dict(self) -> dict:
        return {
            "entity_id": self.entity_id,
            "kind": self.kind,
            "canonical_name": self.canonical_name,
            "title": self.title,
        }


@dataclass(frozen=True)
class SectionSummary:
    section_key: str
    heading: str

    def to_dict(self) -> dict:
        return {"section_key": self.section_key, "heading": self.heading}


@dataclass(frozen=True)
class SearchResult:
    entity_id: str
    kind: str
    subtype: str
    canonical_name: str
    title: str
    summary: str
    authority: str
    is_current: bool
    operator_type: str | None
    operator_type_status: str | None
    tags: tuple[str, ...]
    signatures: tuple[str, ...]
    match_reasons: tuple[str, ...]
    neighbors: tuple[NeighborSummary, ...]
    source_path: str
    source_anchor: str | None

    def to_dict(self) -> dict:
        return {
            "entity_id": self.entity_id,
            "kind": self.kind,
            "subtype": self.subtype,
            "canonical_name": self.canonical_name,
            "title": self.title,
            "summary": self.summary,
            "authority": self.authority,
            "is_current": self.is_current,
            "operator_type": self.operator_type,
            "operator_type_status": self.operator_type_status,
            "tags": list(self.tags),
            "signatures": list(self.signatures),
            "match_reasons": list(self.match_reasons),
            "neighbors": [n.to_dict() for n in self.neighbors],
            "source": {"path": self.source_path, "anchor": self.source_anchor},
        }


@dataclass(frozen=True)
class SearchResponse:
    ok: bool
    code: KnowledgeErrorCode | None
    results: tuple[SearchResult, ...] = ()
    candidates: tuple[CandidateSummary, ...] = ()
    provenance: KbProvenance = field(default_factory=KbProvenance)
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "code": self.code.value if self.code is not None else None,
            "results": [r.to_dict() for r in self.results],
            "candidates": [c.to_dict() for c in self.candidates],
            "provenance": self.provenance.to_dict(),
            "error": self.error,
        }


@dataclass(frozen=True)
class GetResponse:
    ok: bool
    code: KnowledgeErrorCode | None
    entity_id: str = ""
    title: str = ""
    body: str = ""
    truncated: bool = False
    section: str | None = None
    available_sections: tuple[SectionSummary, ...] = ()
    authority: str = ""
    source_path: str = ""
    source_anchor: str | None = None
    provenance: KbProvenance = field(default_factory=KbProvenance)
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "code": self.code.value if self.code is not None else None,
            "entity_id": self.entity_id,
            "title": self.title,
            "body": self.body,
            "truncated": self.truncated,
            "section": self.section,
            "available_sections": [s.to_dict() for s in self.available_sections],
            "authority": self.authority,
            "source": {"path": self.source_path, "anchor": self.source_anchor},
            "provenance": self.provenance.to_dict(),
            "error": self.error,
        }


@dataclass(frozen=True)
class NeighborResponse:
    ok: bool
    code: KnowledgeErrorCode | None
    entity_id: str = ""
    direction: str = "outgoing"
    predicate: str | None = None
    neighbors: tuple[NeighborSummary, ...] = ()
    provenance: KbProvenance = field(default_factory=KbProvenance)
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "ok": self.ok,
            "code": self.code.value if self.code is not None else None,
            "entity_id": self.entity_id,
            "direction": self.direction,
            "predicate": self.predicate,
            "neighbors": [n.to_dict() for n in self.neighbors],
            "provenance": self.provenance.to_dict(),
            "error": self.error,
        }
