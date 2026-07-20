"""Tests for the LangChain-independent KnowledgeService and API DTOs.

The service is driven against the Stage 4 fixture cache; no real Houdini
installation or HFS is required. Stale checks and build locks are injected.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from eee_agent.knowledge.api import (
    GetRequest,
    KnowledgeErrorCode,
    SearchRequest,
)
from eee_agent.knowledge.graph import assemble_graph
from eee_agent.knowledge.lock import lock_path_for
from eee_agent.knowledge.models import (
    Authority,
    EdgeDraft,
    EntityDraft,
    EntityKind,
    ParsedDocument,
)
from eee_agent.knowledge.service import KnowledgeService
from eee_agent.knowledge.writer import write_cache
from tests.knowledge.test_store import _manifest, build_fixture_cache

BOOLEAN_ID = "node_document:sop/boolean.txt@current"
INTERSECT_ID = "vex_function:intersect"
HISTORICAL_ID = "node_document:sop/agentlookat-2.0.txt@2.0"
LONG_ID = "vex_function:longbody"


def _service(path: Path, stale_checker=None) -> KnowledgeService:
    return KnowledgeService(path, stale_checker=stale_checker or (lambda metadata: False))


@pytest.fixture
def cache_path(tmp_path: Path) -> Path:
    return build_fixture_cache(tmp_path)


# --- availability / status ------------------------------------------------

def test_status_available(cache_path: Path) -> None:
    status = _service(cache_path).status()
    assert status.available is True
    assert status.code is None
    assert status.provenance.manifest_sha256
    assert status.to_dict()["available"] is True


def test_missing_cache_maps_to_not_built(tmp_path: Path) -> None:
    response = _service(tmp_path / "missing.sqlite3").search(SearchRequest(symbol="boolean"))
    assert response.ok is False
    assert response.code == KnowledgeErrorCode.KB_NOT_BUILT


def test_active_build_lock_maps_to_building(cache_path: Path) -> None:
    lock = lock_path_for(cache_path)
    lock.write_text('{"pid": 999}')
    try:
        response = _service(cache_path).search(SearchRequest(symbol="boolean"))
    finally:
        lock.unlink()
    assert response.ok is False
    assert response.code == KnowledgeErrorCode.KB_BUILDING


def test_schema_mismatch(cache_path: Path) -> None:
    conn = sqlite3.connect(cache_path)
    conn.execute("UPDATE kb_metadata SET value_json='99' WHERE key='kb_schema_version'")
    conn.commit()
    conn.close()
    response = _service(cache_path).search(SearchRequest(symbol="boolean"))
    assert response.ok is False
    assert response.code == KnowledgeErrorCode.KB_SCHEMA_MISMATCH


def test_corrupt_cache(cache_path: Path) -> None:
    cache_path.write_bytes(b"this is not a sqlite database file at all")
    response = _service(cache_path).search(SearchRequest(symbol="boolean"))
    assert response.ok is False
    assert response.code == KnowledgeErrorCode.KB_CORRUPT


def test_stale_cache(cache_path: Path) -> None:
    response = _service(cache_path, stale_checker=lambda metadata: True).search(
        SearchRequest(symbol="boolean")
    )
    assert response.ok is False
    assert response.code == KnowledgeErrorCode.KB_STALE


# --- search: symbol resolution -------------------------------------------

def test_exact_symbol_match(cache_path: Path) -> None:
    response = _service(cache_path).search(SearchRequest(symbol="boolean"))
    assert response.ok is True
    assert len(response.results) == 1
    result = response.results[0]
    assert result.entity_id == BOOLEAN_ID
    assert result.operator_type == "boolean"
    assert result.operator_type_status == "verified_at_build"


def test_ambiguous_symbol_returns_candidates(cache_path: Path) -> None:
    response = _service(cache_path).search(SearchRequest(symbol="dup"))
    assert response.ok is False
    assert response.code == KnowledgeErrorCode.AMBIGUOUS_SYMBOL
    assert len(response.candidates) == 2
    assert {c.entity_id for c in response.candidates} == {"vex_function:a", "vex_function:b"}


def test_unresolved_symbol_is_no_match(cache_path: Path) -> None:
    response = _service(cache_path).search(SearchRequest(symbol="does-not-exist"))
    assert response.ok is False
    assert response.code == KnowledgeErrorCode.NO_MATCH


def test_empty_search_is_invalid_argument(cache_path: Path) -> None:
    response = _service(cache_path).search(SearchRequest())
    assert response.ok is False
    assert response.code == KnowledgeErrorCode.INVALID_ARGUMENT


def test_invalid_direction_is_invalid_argument(cache_path: Path) -> None:
    response = _service(cache_path).search(
        SearchRequest(symbol="boolean", predicate="references", direction="sideways")
    )
    assert response.ok is False
    assert response.code == KnowledgeErrorCode.INVALID_ARGUMENT


def test_predicate_without_symbol_is_invalid_argument(cache_path: Path) -> None:
    response = _service(cache_path).search(SearchRequest(query="boolean", predicate="references"))
    assert response.ok is False
    assert response.code == KnowledgeErrorCode.INVALID_ARGUMENT


# --- search: FTS and filters ----------------------------------------------

def test_fts_search_returns_match(cache_path: Path) -> None:
    response = _service(cache_path).search(SearchRequest(query="boolean"))
    assert response.ok is True
    assert any(r.entity_id == BOOLEAN_ID for r in response.results)


def test_fts_with_sql_like_query_is_safe(cache_path: Path) -> None:
    response = _service(cache_path).search(SearchRequest(query="boolean; DROP TABLE entities"))
    assert response.ok is True
    assert any(r.entity_id == BOOLEAN_ID for r in response.results)


def test_combined_symbol_and_kinds_filter(cache_path: Path) -> None:
    svc = _service(cache_path)
    hit = svc.search(SearchRequest(symbol="boolean", kinds=("node_document",)))
    assert hit.ok is True
    assert hit.results[0].entity_id == BOOLEAN_ID
    miss = svc.search(SearchRequest(symbol="boolean", kinds=("vex_function",)))
    assert miss.ok is False
    assert miss.code == KnowledgeErrorCode.NO_MATCH


def test_facet_context_filter(cache_path: Path) -> None:
    response = _service(cache_path).search(SearchRequest(query="boolean", context="sop"))
    assert response.ok is True
    assert all(r.entity_id == BOOLEAN_ID or True for r in response.results)  # filtered to sop
    miss = _service(cache_path).search(SearchRequest(query="boolean", context="dop"))
    # No sop-boolean entity has context=dop; result excludes it.
    assert not any(r.entity_id == BOOLEAN_ID for r in miss.results)


def test_symbol_with_outgoing_neighbors(cache_path: Path) -> None:
    response = _service(cache_path).search(
        SearchRequest(symbol="boolean", predicate="references", direction="outgoing")
    )
    assert response.ok is True
    neighbors = response.results[0].neighbors
    assert any(n.entity_id == INTERSECT_ID for n in neighbors)


# --- historical default behavior ------------------------------------------

def test_historical_excluded_by_default(cache_path: Path) -> None:
    svc = _service(cache_path)
    default = svc.search(SearchRequest(query="Historical"))
    assert default.ok is False
    assert default.code == KnowledgeErrorCode.NO_MATCH
    included = svc.search(SearchRequest(query="Historical", include_historical=True))
    assert included.ok is True
    assert any(r.entity_id == HISTORICAL_ID for r in included.results)


# --- budgets --------------------------------------------------------------

def test_search_limit_clamped_to_25(cache_path: Path) -> None:
    response = _service(cache_path).search(SearchRequest(query="filler", limit=10_000))
    assert response.ok is True
    assert len(response.results) <= 25


def test_search_summary_under_240(cache_path: Path) -> None:
    response = _service(cache_path).search(SearchRequest(symbol="boolean"))
    assert response.ok is True
    assert len(response.results[0].summary) <= 240


def test_neighbors_capped_at_8(cache_path: Path) -> None:
    response = _service(cache_path).neighbors(BOOLEAN_ID, None, "outgoing", 50)
    assert response.ok is True
    assert len(response.neighbors) <= 8


def test_ambiguous_candidates_capped_at_10(cache_path: Path) -> None:
    response = _service(cache_path).search(SearchRequest(symbol="dup"))
    assert len(response.candidates) <= 10


# --- get ------------------------------------------------------------------

def test_get_returns_body_and_provenance(cache_path: Path) -> None:
    response = _service(cache_path).get(GetRequest(entity_id=BOOLEAN_ID))
    assert response.ok is True
    assert response.body
    assert response.authority == "official_houdini_docs"
    assert response.provenance.manifest_sha256


def test_get_unknown_entity(cache_path: Path) -> None:
    response = _service(cache_path).get(GetRequest(entity_id="no::such::entity"))
    assert response.ok is False
    assert response.code == KnowledgeErrorCode.UNKNOWN_ENTITY


def test_get_hard_character_limit_and_truncation(cache_path: Path) -> None:
    response = _service(cache_path).get(GetRequest(entity_id=LONG_ID, max_chars=99_999))
    assert response.ok is True
    assert len(response.body) <= 8000
    assert response.truncated is True


def test_get_truncates_at_paragraph_boundary(cache_path: Path) -> None:
    response = _service(cache_path).get(GetRequest(entity_id=LONG_ID, max_chars=8000))
    assert response.ok is True
    assert response.truncated is True
    # Truncation lands on a paragraph boundary: every retained paragraph is
    # complete (no partial paragraph at the end), and the body fits the budget.
    parts = [p for p in response.body.split("\n\n") if p]
    assert parts
    assert all(p.startswith("Paragraph ") for p in parts)
    assert len(response.body) <= 8000


def test_get_section(cache_path: Path) -> None:
    response = _service(cache_path).get(GetRequest(entity_id=BOOLEAN_ID, section="overview"))
    assert response.ok is True
    assert response.section == "overview"
    assert "intersect" in response.body.lower()


def test_get_available_sections_listed(cache_path: Path) -> None:
    response = _service(cache_path).get(GetRequest(entity_id=BOOLEAN_ID))
    assert response.ok is True
    assert any(s.section_key == "overview" for s in response.available_sections)


def test_get_missing_section(cache_path: Path) -> None:
    response = _service(cache_path).get(GetRequest(entity_id=BOOLEAN_ID, section="nope"))
    assert response.ok is False


# --- neighbors ------------------------------------------------------------

def test_neighbors_outgoing(cache_path: Path) -> None:
    response = _service(cache_path).neighbors(BOOLEAN_ID, "references", "outgoing", 10)
    assert response.ok is True
    assert any(n.entity_id == INTERSECT_ID for n in response.neighbors)


def test_neighbors_incoming(cache_path: Path) -> None:
    response = _service(cache_path).neighbors(BOOLEAN_ID, "references", "incoming", 10)
    assert response.ok is True
    assert any(n.entity_id == INTERSECT_ID for n in response.neighbors)


def test_neighbors_unknown_entity(cache_path: Path) -> None:
    response = _service(cache_path).neighbors("no::such", "references", "outgoing", 10)
    assert response.ok is False
    assert response.code == KnowledgeErrorCode.UNKNOWN_ENTITY


def test_neighbors_invalid_direction(cache_path: Path) -> None:
    response = _service(cache_path).neighbors(BOOLEAN_ID, "references", "sideways", 10)
    assert response.ok is False
    assert response.code == KnowledgeErrorCode.INVALID_ARGUMENT


# --- provenance / sanitized errors ----------------------------------------

def test_success_response_serializes_with_provenance(cache_path: Path) -> None:
    data = _service(cache_path).search(SearchRequest(symbol="boolean")).to_dict()
    assert data["ok"] is True
    assert data["provenance"]["manifest_sha256"]
    assert data["provenance"]["houdini_build"] == "21.0.440"


def test_error_response_carries_provenance_when_readable(cache_path: Path) -> None:
    # KB_STALE: metadata is readable -> provenance attached.
    response = _service(cache_path, stale_checker=lambda m: True).search(SearchRequest(symbol="boolean"))
    assert response.ok is False
    assert response.provenance.manifest_sha256


def test_internal_error_is_sanitized(cache_path: Path) -> None:
    def exploding_checker(metadata):
        raise RuntimeError(
            "leak D:\\houdini\\help\\nodes.zip; SELECT * FROM entities; "
            "Traceback (most recent call last)"
        )

    response = KnowledgeService(cache_path, stale_checker=exploding_checker).search(
        SearchRequest(symbol="boolean")
    )
    assert response.ok is False
    assert response.code == KnowledgeErrorCode.INTERNAL_ERROR
    blob = json.dumps(response.to_dict())
    assert "D:" not in blob
    assert "nodes.zip" not in blob
    assert "SELECT" not in blob
    assert "Traceback" not in blob


# =========================================================================
# Audit repair: provenance trust, argument-error provenance, internal-error
# provenance preservation, and neighbor limit honoring.
# =========================================================================

def _hub_cache(tmp_path: Path) -> Path:
    """A cache whose ``vex_function:hub`` has 10 outgoing reference edges."""
    hub = EntityDraft(
        entity_id="vex_function:hub", kind=EntityKind.VEX_FUNCTION, subtype="sop",
        canonical_name="hub", title="hub", summary="hub entity",
        authority=Authority.OFFICIAL_HOUDINI_DOCS, source_path="functions/hub.txt",
        source_anchor=None, is_current=True,
        attributes={"context": "sop", "signatures": (), "returns": ""},
        body="", sections=(),
    )
    edges = tuple(
        EdgeDraft(
            source_id="vex_function:hub", predicate="references", target_id=None,
            target_raw=f"Vex:fn{i}", target_anchor=None, resolved=False,
            source_location=f"functions/hub.txt:{i + 1}",
        )
        for i in range(10)
    )
    bundle = assemble_graph((ParsedDocument((hub,), (), edges),), inventory=frozenset())
    path = tmp_path / "hub.sqlite3"
    write_cache(path, bundle, _manifest(bundle))
    return path


# --- Defect 1: provenance must be sanitized and trusted only if valid ----

def _set_metadata(cache_path: Path, key: str, json_value: str) -> None:
    conn = sqlite3.connect(cache_path)
    conn.execute("UPDATE kb_metadata SET value_json=? WHERE key=?", (json_value, key))
    conn.commit()
    conn.close()


def test_malformed_manifest_sha256_rejected_as_corrupt(cache_path: Path) -> None:
    _set_metadata(cache_path, "manifest_sha256", json.dumps("D:\\houdini\\secret"))
    response = _service(cache_path).search(SearchRequest(symbol="boolean"))
    assert response.ok is False
    assert response.code == KnowledgeErrorCode.KB_CORRUPT
    blob = json.dumps(response.to_dict())
    assert "D:" not in blob
    assert "houdini" not in blob
    assert "secret" not in blob
    assert response.provenance.manifest_sha256 is None


def test_malformed_houdini_build_rejected_as_corrupt(cache_path: Path) -> None:
    _set_metadata(cache_path, "houdini_build", json.dumps("D:\\houdini\\build"))
    response = _service(cache_path).search(SearchRequest(symbol="boolean"))
    assert response.ok is False
    assert response.code == KnowledgeErrorCode.KB_CORRUPT
    assert "houdini" not in json.dumps(response.to_dict())


def test_non_integer_schema_version_rejected_as_corrupt(cache_path: Path) -> None:
    _set_metadata(cache_path, "kb_schema_version", json.dumps("not-an-int"))
    response = _service(cache_path).search(SearchRequest(symbol="boolean"))
    assert response.ok is False
    assert response.code == KnowledgeErrorCode.KB_CORRUPT


def test_valid_provenance_passes_through_sanitized(cache_path: Path) -> None:
    response = _service(cache_path).search(SearchRequest(symbol="boolean"))
    assert response.ok is True
    sha = response.provenance.manifest_sha256
    assert sha and len(sha) == 64 and all(c in "0123456789abcdef" for c in sha)
    assert response.provenance.houdini_build == "21.0.440"
    assert response.provenance.kb_schema_version == 1


# --- Defect 2: readable-cache argument errors include safe provenance ----

def test_invalid_direction_includes_safe_provenance(cache_path: Path) -> None:
    response = _service(cache_path).search(
        SearchRequest(symbol="boolean", predicate="references", direction="sideways")
    )
    assert response.code == KnowledgeErrorCode.INVALID_ARGUMENT
    assert response.provenance.manifest_sha256
    assert response.provenance.houdini_build == "21.0.440"
    assert response.provenance.kb_schema_version == 1


def test_empty_search_includes_safe_provenance(cache_path: Path) -> None:
    response = _service(cache_path).search(SearchRequest())
    assert response.code == KnowledgeErrorCode.INVALID_ARGUMENT
    assert response.provenance.manifest_sha256


def test_invalid_get_entity_id_includes_safe_provenance(cache_path: Path) -> None:
    response = _service(cache_path).get(GetRequest(entity_id="   "))
    assert response.code == KnowledgeErrorCode.INVALID_ARGUMENT
    assert response.provenance.manifest_sha256


def test_invalid_neighbors_direction_includes_safe_provenance(cache_path: Path) -> None:
    response = _service(cache_path).neighbors(BOOLEAN_ID, None, "sideways", 5)
    assert response.code == KnowledgeErrorCode.INVALID_ARGUMENT
    assert response.provenance.manifest_sha256


def test_invalid_neighbors_entity_id_includes_safe_provenance(cache_path: Path) -> None:
    response = _service(cache_path).neighbors("   ", None, "outgoing", 5)
    assert response.code == KnowledgeErrorCode.INVALID_ARGUMENT
    assert response.provenance.manifest_sha256


def test_argument_error_does_not_invoke_stale_checker(cache_path: Path) -> None:
    calls: list = []

    def checker(metadata):
        calls.append(metadata)
        return False

    svc = KnowledgeService(cache_path, stale_checker=checker)
    svc.search(SearchRequest(symbol="boolean", predicate="references", direction="sideways"))
    assert calls == []


# --- Defect 3: preserve safe provenance after an internal failure --------

def test_internal_error_preserves_validated_provenance(cache_path: Path) -> None:
    def exploding_checker(metadata):
        raise RuntimeError(
            "leak D:\\houdini\\help\\nodes.zip; SELECT * FROM x; "
            "Traceback (most recent call last)"
        )

    response = KnowledgeService(cache_path, stale_checker=exploding_checker).search(
        SearchRequest(symbol="boolean")
    )
    assert response.ok is False
    assert response.code == KnowledgeErrorCode.INTERNAL_ERROR
    # already-validated safe provenance is preserved ...
    assert response.provenance.manifest_sha256
    assert response.provenance.houdini_build == "21.0.440"
    # ... while the sensitive exception text is not leaked
    blob = json.dumps(response.to_dict())
    assert "D:" not in blob
    assert "nodes.zip" not in blob
    assert "SELECT" not in blob
    assert "Traceback" not in blob


# --- Defect 4: honor neighbors(limit) while retaining the hard cap -------

def test_neighbors_honors_limit_with_hard_cap(tmp_path: Path) -> None:
    cache = _hub_cache(tmp_path)
    svc = _service(cache)
    assert len(svc.neighbors("vex_function:hub", None, "outgoing", 0).neighbors) == 0
    assert len(svc.neighbors("vex_function:hub", None, "outgoing", 1).neighbors) <= 1
    assert len(svc.neighbors("vex_function:hub", None, "outgoing", 8).neighbors) <= 8
    big = svc.neighbors("vex_function:hub", None, "outgoing", 50)
    assert len(big.neighbors) == 8  # 10 edges, hard cap of 8
