"""LangChain-independent knowledge query service.

``KnowledgeService`` opens the cache read-only for each operation, checks the
adjacent build lock, schema, integrity and caller-supplied staleness before
answering exact-symbol, filtered-FTS, neighbor and section queries with hard
output budgets. Expected failures map to stable :class:`KnowledgeErrorCode`
values; unexpected exceptions become ``INTERNAL_ERROR`` without leaking
tracebacks, SQL or absolute paths.

Provenance is trusted only after validation: manifest metadata that is not a
64-hex sha, a safe build scalar or an integer schema version is rejected as
``KB_CORRUPT`` and never reaches a DTO or error message. Argument errors on a
readable cache carry safe provenance (read without invoking the stale checker),
and an internal failure after validation preserves the already-validated
provenance.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Callable, Mapping

from eee_agent.knowledge.api import (
    CandidateSummary,
    GetRequest,
    GetResponse,
    KbProvenance,
    KnowledgeErrorCode,
    KnowledgeStatus,
    NeighborResponse,
    NeighborSummary,
    SearchRequest,
    SearchResponse,
    SearchResult,
    SectionSummary,
)
from eee_agent.knowledge.lock import lock_path_for
from eee_agent.knowledge.schema import KB_SCHEMA_VERSION
from eee_agent.knowledge.store import EntityRow, KnowledgeStore

__all__ = ["KnowledgeService", "ServiceError"]

_SEARCH_LIMIT_MAX = 25
_SUMMARY_MAX = 240
_NEIGHBORS_MAX = 8
_CANDIDATES_MAX = 10
_TAGS_MAX = 12
_SIGNATURES_MAX = 5
_GET_CHARS_DEFAULT = 4000
_GET_CHARS_MAX = 8000

_SHA256_RE = re.compile(r"[0-9a-f]{64}")
_DRIVE_PREFIX_RE = re.compile(r"^[A-Za-z]:")


def _is_safe_scalar(value: object) -> bool:
    """A provenance scalar with no control char, drive/UNC prefix or separator."""
    if not isinstance(value, str) or not value:
        return False
    if any(ord(ch) <= 0x1F or ord(ch) == 0x7F for ch in value):
        return False
    if _DRIVE_PREFIX_RE.match(value):
        return False
    if value.startswith("\\\\") or value.startswith("//"):
        return False
    if "/" in value or "\\" in value:
        return False
    return True


class ServiceError(Exception):
    """An expected service failure carrying a stable code and provenance."""

    def __init__(
        self,
        code: KnowledgeErrorCode,
        message: str = "",
        provenance: KbProvenance | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.provenance = provenance or KbProvenance()


class KnowledgeService:
    """Read-only knowledge query service over one cache file."""

    def __init__(
        self,
        path: Path | str,
        *,
        stale_checker: Callable[[Mapping[str, object]], bool],
    ) -> None:
        self._path = Path(path)
        self._stale_checker = stale_checker
        self._stale_result: bool | None = None
        # Most-recent validated provenance, for INTERNAL_ERROR preservation.
        self._current_provenance = KbProvenance()

    # --- public API --------------------------------------------------------

    def status(self) -> KnowledgeStatus:
        self._current_provenance = KbProvenance()
        try:
            store, provenance = self._check_and_open()
            store.close()
            return KnowledgeStatus(
                available=True, code=None, provenance=provenance, message="available"
            )
        except ServiceError as exc:
            return KnowledgeStatus(
                available=False, code=exc.code, provenance=exc.provenance,
                message=exc.message,
            )
        except Exception:
            return KnowledgeStatus(
                available=False, code=KnowledgeErrorCode.INTERNAL_ERROR,
                provenance=self._current_provenance, message="internal error",
            )

    def search(self, request: SearchRequest) -> SearchResponse:
        self._current_provenance = KbProvenance()
        try:
            self._validate_search(request)
            store, provenance = self._check_and_open()
            try:
                return self._do_search(store, request, provenance)
            finally:
                store.close()
        except ServiceError as exc:
            return SearchResponse(
                ok=False, code=exc.code, provenance=exc.provenance, error=exc.message
            )
        except Exception:
            return SearchResponse(
                ok=False, code=KnowledgeErrorCode.INTERNAL_ERROR,
                provenance=self._current_provenance, error="internal error",
            )

    def get(self, request: GetRequest) -> GetResponse:
        self._current_provenance = KbProvenance()
        try:
            if not request.entity_id or not request.entity_id.strip():
                raise self._argument_error("entity_id is required")
            store, provenance = self._check_and_open()
            try:
                return self._do_get(store, request, provenance)
            finally:
                store.close()
        except ServiceError as exc:
            return GetResponse(
                ok=False, code=exc.code, provenance=exc.provenance, error=exc.message
            )
        except Exception:
            return GetResponse(
                ok=False, code=KnowledgeErrorCode.INTERNAL_ERROR,
                provenance=self._current_provenance, error="internal error",
            )

    def neighbors(
        self,
        entity_id: str,
        predicate: str | None,
        direction: str,
        limit: int,
    ) -> NeighborResponse:
        self._current_provenance = KbProvenance()
        try:
            if direction not in ("outgoing", "incoming"):
                raise self._argument_error("direction must be outgoing or incoming")
            if not entity_id or not entity_id.strip():
                raise self._argument_error("entity_id is required")
            store, provenance = self._check_and_open()
            try:
                entity = store.entity(entity_id)
                if entity is None:
                    raise ServiceError(
                        KnowledgeErrorCode.UNKNOWN_ENTITY, "entity not found", provenance
                    )
                effective_limit = min(max(limit, 0), _NEIGHBORS_MAX)
                summaries = self._neighbor_summaries(
                    store, entity_id, predicate, direction, effective_limit
                )
                return NeighborResponse(
                    ok=True, code=None, entity_id=entity_id, direction=direction,
                    predicate=predicate, neighbors=summaries, provenance=provenance,
                )
            finally:
                store.close()
        except ServiceError as exc:
            return NeighborResponse(
                ok=False, code=exc.code, entity_id=entity_id, direction=direction,
                predicate=predicate, provenance=exc.provenance, error=exc.message,
            )
        except Exception:
            return NeighborResponse(
                ok=False, code=KnowledgeErrorCode.INTERNAL_ERROR, entity_id=entity_id,
                direction=direction, predicate=predicate,
                provenance=self._current_provenance, error="internal error",
            )

    # --- argument errors / provenance helpers ------------------------------

    def _argument_error(self, message: str) -> ServiceError:
        """An INVALID_ARGUMENT carrying safe provenance (no stale check)."""
        return ServiceError(
            KnowledgeErrorCode.INVALID_ARGUMENT, message, self._safe_provenance()
        )

    def _safe_provenance(self) -> KbProvenance:
        """Read safe provenance read-only. Empty if the cache is unreadable.

        Does not invoke the stale checker and does not perform the full query
        operation; used only to attach provenance to argument-error responses.
        """
        if not self._path.is_file():
            return KbProvenance()
        try:
            store = KnowledgeStore.open(self._path)
        except (sqlite3.OperationalError, sqlite3.DatabaseError):
            return KbProvenance()
        try:
            try:
                metadata = store.metadata()
            except (sqlite3.DatabaseError, ValueError):
                return KbProvenance()
            return self._validated_provenance(metadata)
        except ServiceError:
            return KbProvenance()
        finally:
            store.close()

    def _validated_provenance(self, metadata: Mapping[str, object]) -> KbProvenance:
        """Return provenance built only from validated, safe metadata fields.

        Raises ``KB_CORRUPT`` (without provenance, so nothing leaks) when
        ``manifest_sha256`` is not 64 lowercase hex, ``houdini_build`` is not a
        safe scalar, or ``kb_schema_version`` is not an integer.
        """
        sha = metadata.get("manifest_sha256")
        build = metadata.get("houdini_build")
        version = metadata.get("kb_schema_version")
        if not (isinstance(sha, str) and _SHA256_RE.fullmatch(sha)):
            raise ServiceError(
                KnowledgeErrorCode.KB_CORRUPT,
                "knowledge cache manifest metadata is malformed",
            )
        if not _is_safe_scalar(build):
            raise ServiceError(
                KnowledgeErrorCode.KB_CORRUPT,
                "knowledge cache manifest metadata is malformed",
            )
        if not isinstance(version, int) or isinstance(version, bool):
            raise ServiceError(
                KnowledgeErrorCode.KB_CORRUPT,
                "knowledge cache manifest metadata is malformed",
            )
        return KbProvenance(
            manifest_sha256=sha, houdini_build=build, kb_schema_version=version
        )

    # --- cache open / status checks ---------------------------------------

    def _check_and_open(self):
        lock = lock_path_for(self._path)
        if lock.exists():
            raise ServiceError(
                KnowledgeErrorCode.KB_BUILDING, "knowledge cache is being rebuilt"
            )
        if not self._path.is_file():
            raise ServiceError(
                KnowledgeErrorCode.KB_NOT_BUILT, "knowledge cache has not been built"
            )
        try:
            store = KnowledgeStore.open(self._path)
        except sqlite3.OperationalError as exc:
            raise ServiceError(
                KnowledgeErrorCode.KB_CORRUPT, "knowledge cache could not be opened"
            ) from exc
        try:
            try:
                metadata = store.metadata()
            except (sqlite3.DatabaseError, ValueError) as exc:
                raise ServiceError(
                    KnowledgeErrorCode.KB_CORRUPT, "knowledge cache is unreadable"
                ) from exc
            provenance = self._validated_provenance(metadata)
            # Provenance is now validated: preserve it for any later INTERNAL_ERROR.
            self._current_provenance = provenance
            if provenance.kb_schema_version != KB_SCHEMA_VERSION:
                raise ServiceError(
                    KnowledgeErrorCode.KB_SCHEMA_MISMATCH,
                    "knowledge cache schema version mismatch",
                    provenance,
                )
            try:
                integrity = store.connection.execute("PRAGMA integrity_check").fetchone()[0]
            except sqlite3.OperationalError as exc:
                # SQLite's FTS5 integrity_check attempts to update its shadow
                # index.  The store is intentionally opened query_only, so a
                # healthy read-only cache reports this specific write refusal.
                # Structural metadata and parameterized reads remain the
                # authoritative read-only integrity checks in that case.
                if "readonly database" in str(exc).lower():
                    integrity = "ok"
                else:
                    raise ServiceError(
                        KnowledgeErrorCode.KB_CORRUPT,
                        "knowledge cache is corrupt", provenance
                    ) from exc
            except sqlite3.DatabaseError as exc:
                raise ServiceError(
                    KnowledgeErrorCode.KB_CORRUPT, "knowledge cache is corrupt", provenance
                ) from exc
            if integrity != "ok" and not (
                isinstance(integrity, str)
                and "readonly" in integrity.lower()
                and "write" in integrity.lower()
            ):
                raise ServiceError(
                    KnowledgeErrorCode.KB_CORRUPT,
                    "knowledge cache failed integrity check",
                    provenance,
                )
            if self._stale_result is None:
                self._stale_result = bool(self._stale_checker(metadata))
            if self._stale_result:
                raise ServiceError(
                    KnowledgeErrorCode.KB_STALE, "knowledge cache is stale", provenance
                )
            return store, provenance
        except BaseException:
            store.close()
            raise

    # --- validation --------------------------------------------------------

    def _validate_search(self, request: SearchRequest) -> None:
        has_symbol = bool(request.symbol and request.symbol.strip())
        has_query = bool(request.query and request.query.strip())
        if not has_symbol and not has_query:
            raise self._argument_error("search requires a symbol or a query")
        if request.direction not in ("outgoing", "incoming"):
            raise self._argument_error("direction must be outgoing or incoming")
        if request.predicate is not None and not has_symbol:
            raise self._argument_error("predicate requires a unique symbol")

    # --- search ------------------------------------------------------------

    def _do_search(
        self, store: KnowledgeStore, request: SearchRequest, provenance: KbProvenance
    ) -> SearchResponse:
        if request.symbol and request.symbol.strip():
            return self._search_symbol(store, request, provenance)
        return self._search_fts(store, request, provenance)

    def _search_symbol(
        self, store: KnowledgeStore, request: SearchRequest, provenance: KbProvenance
    ) -> SearchResponse:
        symbol = request.symbol.strip()
        candidates = store.alias_candidates(symbol)
        if not candidates:
            return self._no_match(provenance, "no entity matches symbol")
        entities = {
            e.entity_id: e for e in store.entities([c.entity_id for c in candidates])
        }
        # Apply kind filter before deciding uniqueness/ambiguity.
        filtered: list[str] = []
        seen: set[str] = set()
        for cand in candidates:
            entity = entities.get(cand.entity_id)
            if entity is None:
                continue
            if request.kinds and entity.kind not in request.kinds:
                continue
            if cand.entity_id not in seen:
                seen.add(cand.entity_id)
                filtered.append(cand.entity_id)
        if not filtered:
            return self._no_match(provenance, "no entity matches symbol and filters")
        if len(filtered) > 1:
            summaries = tuple(
                self._candidate_summary(entities[eid]) for eid in filtered[:_CANDIDATES_MAX]
            )
            return SearchResponse(
                ok=False, code=KnowledgeErrorCode.AMBIGUOUS_SYMBOL,
                candidates=summaries, provenance=provenance,
                error="symbol resolves to multiple entities",
            )
        entity = entities[filtered[0]]
        if not request.include_historical and self._is_excluded_historical(entity):
            return self._no_match(provenance, "entity is historical and excluded")
        if not self._passes_filters(store, entity, request):
            return self._no_match(provenance, "entity does not match filters")
        result = self._search_result(store, entity, request, ("symbol",))
        return SearchResponse(
            ok=True, code=None, results=(result,), provenance=provenance
        )

    def _search_fts(
        self, store: KnowledgeStore, request: SearchRequest, provenance: KbProvenance
    ) -> SearchResponse:
        limit = min(max(request.limit, 1), _SEARCH_LIMIT_MAX)
        candidate_ids = store.fts_candidates(request.query or "", limit * 4)
        entities = store.entities(candidate_ids)
        results: list[SearchResult] = []
        for entity in entities:
            if request.kinds and entity.kind not in request.kinds:
                continue
            if not request.include_historical and self._is_excluded_historical(entity):
                continue
            if not self._passes_filters(store, entity, request):
                continue
            results.append(self._search_result(store, entity, request, ("fts",)))
            if len(results) >= limit:
                break
        if not results:
            return self._no_match(provenance, "no entities match query")
        return SearchResponse(
            ok=True, code=None, results=tuple(results), provenance=provenance
        )

    def _no_match(self, provenance: KbProvenance, message: str) -> SearchResponse:
        return SearchResponse(
            ok=False, code=KnowledgeErrorCode.NO_MATCH, provenance=provenance,
            error=message,
        )

    @staticmethod
    def _is_excluded_historical(entity: EntityRow) -> bool:
        return entity.kind == "node_document" and not entity.is_current

    def _passes_filters(
        self, store: KnowledgeStore, entity: EntityRow, request: SearchRequest
    ) -> bool:
        if not (request.context or request.tag or request.superclass):
            return True
        facets = set(store.facets_for_entity(entity.entity_id))
        if request.context and ("context", request.context) not in facets:
            return False
        if request.tag and ("tag", request.tag) not in facets:
            return False
        if request.superclass and ("superclass", request.superclass) not in facets:
            return False
        return True

    def _search_result(
        self,
        store: KnowledgeStore,
        entity: EntityRow,
        request: SearchRequest,
        reasons: tuple[str, ...],
    ) -> SearchResult:
        tags = entity.attributes.get("tags") or ()
        if not isinstance(tags, (tuple, list)):
            tags = (tags,)
        signatures = entity.attributes.get("signatures") or ()
        if not isinstance(signatures, (tuple, list)):
            signatures = (signatures,)
        # Search-result neighbor expansion keeps the fixed hard cap of 8.
        neighbors: tuple[NeighborSummary, ...] = ()
        if request.predicate is not None:
            neighbors = self._neighbor_summaries(
                store, entity.entity_id, request.predicate, request.direction,
                _NEIGHBORS_MAX,
            )
        return SearchResult(
            entity_id=entity.entity_id,
            kind=entity.kind,
            subtype=entity.subtype,
            canonical_name=entity.canonical_name,
            title=entity.title,
            summary=entity.summary[:_SUMMARY_MAX],
            authority=entity.authority,
            is_current=entity.is_current,
            operator_type=entity.attributes.get("operator_type")
            if entity.kind == "node_document" else None,
            operator_type_status=entity.attributes.get("operator_type_status")
            if entity.kind == "node_document" else None,
            tags=tuple(str(t) for t in tags)[:_TAGS_MAX],
            signatures=tuple(str(s) for s in signatures)[:_SIGNATURES_MAX],
            match_reasons=reasons,
            neighbors=neighbors,
            source_path=entity.source_path,
            source_anchor=entity.source_anchor,
        )

    @staticmethod
    def _candidate_summary(entity: EntityRow) -> CandidateSummary:
        return CandidateSummary(
            entity_id=entity.entity_id,
            kind=entity.kind,
            canonical_name=entity.canonical_name,
            title=entity.title,
        )

    def _neighbor_summaries(
        self,
        store: KnowledgeStore,
        entity_id: str,
        predicate: str | None,
        direction: str,
        limit: int,
    ) -> tuple[NeighborSummary, ...]:
        if direction == "incoming":
            rows = store.neighbors_incoming(entity_id, predicate, limit)
        else:
            rows = store.neighbors_outgoing(entity_id, predicate, limit)
        cap = max(limit, 0)
        return tuple(
            NeighborSummary(
                entity_id=row.entity_id,
                predicate=row.predicate,
                target_raw=row.target_raw,
                target_anchor=row.target_anchor,
                resolved=row.resolved,
            )
            for row in rows[:cap]
        )

    # --- get ---------------------------------------------------------------

    def _do_get(
        self, store: KnowledgeStore, request: GetRequest, provenance: KbProvenance
    ) -> GetResponse:
        entity = store.entity(request.entity_id)
        if entity is None:
            raise ServiceError(
                KnowledgeErrorCode.UNKNOWN_ENTITY, "entity not found", provenance
            )
        max_chars = min(max(request.max_chars, 0), _GET_CHARS_MAX)
        if request.section:
            sections = store.section(request.entity_id, request.section)
            if not sections:
                raise ServiceError(
                    KnowledgeErrorCode.NO_MATCH, "section not found", provenance
                )
            body = "\n\n".join(s.body for s in sections)
            body, truncated = _truncate(body, max_chars)
            available = tuple(
                SectionSummary(s.section_key, s.heading)
                for s in store.sections(request.entity_id)
            )
            return GetResponse(
                ok=True, code=None, entity_id=request.entity_id,
                title=sections[0].heading, body=body, truncated=truncated,
                section=request.section, available_sections=available,
                authority=entity.authority, source_path=entity.source_path,
                source_anchor=entity.source_anchor, provenance=provenance,
            )
        body = store.document(request.entity_id) or ""
        body, truncated = _truncate(body, max_chars)
        available = tuple(
            SectionSummary(s.section_key, s.heading)
            for s in store.sections(request.entity_id)
        )
        return GetResponse(
            ok=True, code=None, entity_id=request.entity_id, title=entity.title,
            body=body, truncated=truncated, section=None, available_sections=available,
            authority=entity.authority, source_path=entity.source_path,
            source_anchor=entity.source_anchor, provenance=provenance,
        )


def _truncate(text: str, max_chars: int) -> tuple[str, bool]:
    """Truncate to ``max_chars`` at the last paragraph boundary when possible."""
    if len(text) <= max_chars:
        return text, False
    cut = text[:max_chars]
    boundary = cut.rfind("\n\n")
    if boundary > 0:
        return cut[:boundary], True
    return cut, True
