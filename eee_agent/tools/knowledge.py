"""Read-only LangChain adapters over the Houdini knowledge service.

Two thin ``@tool`` functions expose exact symbol / FTS search and bounded
section reads from the offline Houdini documentation cache. They construct
:class:`~eee_agent.knowledge.service.KnowledgeService` lazily on first use,
convert inputs to the Stage 5 request DTOs, call the service, and serialize
responses through ``to_dict()``. Every expected and unexpected failure is
caught and mapped to a stable sanitized dict -- the tools never raise into the
Agent and never expose a traceback, SQL statement or absolute path.

Importing this module is pure: it opens no database and touches no HFS. The
service (and its read-only stale check over the three source archives) is built
only when a tool is first invoked with the cache enabled. The query path never
imports the rpyc bridge or ``hou``.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Mapping

from langchain_core.tools import tool

from eee_agent.config import KnowledgeConfig, knowledge_config
from eee_agent.knowledge.api import (
    GetRequest,
    KnowledgeErrorCode,
    SearchRequest,
)

if TYPE_CHECKING:  # type-checkers only; never imported at runtime
    from eee_agent.knowledge.service import KnowledgeService

__all__ = ["get_houdini_knowledge", "search_houdini_knowledge"]

# Lazily-constructed service singleton (None until a tool is first called).
_SERVICE: KnowledgeService | None = None

# Verified two-machine HFS roots (design §9.1). Mirrors the builder's
# candidates; used only to locate current sources for the read-only stale
# check, never stored in the cache or a response.
_DEFAULT_HFS_CANDIDATES = (
    Path("D:/houdini"),
    Path("C:/Program Files/Side Effects Software/Houdini 21.0.440"),
)
_SOURCE_ARCHIVES = ("nodes.zip", "hom.zip", "vex.zip")


def _error(code: KnowledgeErrorCode, message: str) -> dict:
    """A stable, sanitized error dict (no traceback, SQL or absolute path)."""
    return {"ok": False, "code": code.value, "error": message}


def _make_stale_checker(
    hfs: str | None,
) -> Callable[[Mapping[str, object]], bool]:
    """Build a stale checker comparing current HFS archive fingerprints.

    Resolves the configured HFS (explicit override > ``EEE_HFS`` > ``HFS`` env >
    known machine roots), re-fingerprints the three current source archives, and
    reports stale only when any archive's sha256 differs from the manifest stored
    in the cache. If no current HFS can be resolved -- or reading/fingerprinting
    fails for any reason -- it returns ``False``: it never invents staleness,
    leaving schema/integrity verification to the service.
    """
    from eee_agent.knowledge.inventory import resolve_hfs
    from eee_agent.knowledge.manifest import fingerprint_bytes

    def _is_stale(metadata: Mapping[str, object]) -> bool:
        try:
            hfs_path = resolve_hfs(hfs, dict(os.environ), _DEFAULT_HFS_CANDIDATES)
        except Exception:
            return False
        stored = metadata.get("source_archives")
        if not isinstance(stored, list):
            return False
        stored_sha = {
            entry.get("logical_name"): entry.get("sha256")
            for entry in stored
            if isinstance(entry, dict)
        }
        try:
            for name in _SOURCE_ARCHIVES:
                data = (hfs_path / "houdini" / "help" / name).read_bytes()
                if stored_sha.get(name) != fingerprint_bytes(name, data).sha256:
                    return True
            return False
        except Exception:
            return False

    return _is_stale


def _build_service(cfg: KnowledgeConfig) -> KnowledgeService:
    """Construct the service for ``cfg`` with its HFS-fingerprint stale checker."""
    from eee_agent.knowledge.service import KnowledgeService

    return KnowledgeService(cfg.path, stale_checker=_make_stale_checker(cfg.hfs))


def _service() -> KnowledgeService:
    """Return the lazily-constructed singleton knowledge service."""
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = _build_service(knowledge_config())
    return _SERVICE


def _reset_service_for_tests() -> None:
    """Drop the cached service (test seam)."""
    global _SERVICE
    _SERVICE = None


@tool
def search_houdini_knowledge(
    symbol: str | None = None,
    query: str | None = None,
    kinds: list[str] | None = None,
    context: str | None = None,
    tag: str | None = None,
    superclass: str | None = None,
    predicate: str | None = None,
    direction: str = "outgoing",
    include_historical: bool = False,
    limit: int = 5,
) -> dict:
    """Search the local Houdini documentation knowledge graph (offline cache).

    Pass ``symbol`` for exact/alias resolution of a node type, VEX function or
    ``hou.*`` symbol, or ``query`` for free-text search (optionally narrowed by
    ``kinds``/``context``/``tag``/``superclass``). ``predicate``/``direction``
    (outgoing|incoming) filter the one-hop relations of a uniquely resolved
    symbol. Returns a dict with ``ok`` and either ``results``/``candidates`` or
    a stable ``code``/``error``. The cache documents official docs only -- live
    Houdini introspection (describe_node_type) remains authoritative.
    """
    try:
        cfg = knowledge_config()
        if not cfg.enabled:
            return _error(KnowledgeErrorCode.KB_DISABLED, "knowledge tools are disabled")
        request = SearchRequest(
            symbol=symbol,
            query=query,
            kinds=tuple(kinds) if kinds else (),
            context=context,
            tag=tag,
            superclass=superclass,
            predicate=predicate,
            direction=direction,
            include_historical=include_historical,
            limit=limit,
        )
        return _service().search(request).to_dict()
    except Exception:
        return _error(KnowledgeErrorCode.INTERNAL_ERROR, "internal error")


@tool
def get_houdini_knowledge(
    entity_id: str,
    section: str | None = None,
    max_chars: int = 4000,
) -> dict:
    """Read a bounded body/section for one knowledge-graph entity (offline cache).

    ``entity_id`` is a cache primary key (never interpreted as a file path).
    Defaults to 4,000 chars (hard cap 8,000); with ``section`` returns that
    section plus the list of available sections. Returns a dict with ``ok`` and
    ``body`` or a stable ``code``/``error``. Documents official docs only; live
    Houdini introspection stays authoritative.
    """
    try:
        cfg = knowledge_config()
        if not cfg.enabled:
            return _error(KnowledgeErrorCode.KB_DISABLED, "knowledge tools are disabled")
        request = GetRequest(entity_id=entity_id, section=section, max_chars=max_chars)
        return _service().get(request).to_dict()
    except Exception:
        return _error(KnowledgeErrorCode.INTERNAL_ERROR, "internal error")
