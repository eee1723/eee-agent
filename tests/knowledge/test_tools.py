"""Stage 6 Task 13 — LangChain knowledge tool adapter contracts.

The two read-only tools must be thin adapters: lazily build the service,
convert inputs to the Stage 5 request DTOs, call service methods, serialize
responses through ``to_dict()``, and never raise into the Agent. Tests inject
a fake service so they need no HFS, hython, RPC or model call.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from eee_agent.config import knowledge_config
from eee_agent.knowledge.api import (
    GetRequest,
    GetResponse,
    SearchRequest,
    SearchResponse,
)

import eee_agent.tools.knowledge as kb


class _FakeService:
    """A stand-in KnowledgeService that records requests and returns DTOs."""

    def __init__(self, *, search=None, get=None, raise_exc=None) -> None:
        self.search_requests: list[SearchRequest] = []
        self.get_requests: list[GetRequest] = []
        self._search = search
        self._get = get
        self._raise = raise_exc

    def search(self, request: SearchRequest):
        self.search_requests.append(request)
        if self._raise is not None:
            raise self._raise
        return self._search

    def get(self, request: GetRequest):
        self.get_requests.append(request)
        if self._raise is not None:
            raise self._raise
        return self._get


@pytest.fixture(autouse=True)
def _isolated_service(monkeypatch):
    """Each test starts with no cached service and KB enabled by default."""
    monkeypatch.setattr(kb, "_SERVICE", None)
    monkeypatch.delenv("EEE_KB_ENABLED", raising=False)
    yield


def _patch_service(monkeypatch, service: _FakeService) -> None:
    monkeypatch.setattr(kb, "_service", lambda: service)


# --- names / signatures ----------------------------------------------------


def test_tools_expose_exact_spec_names() -> None:
    assert kb.search_houdini_knowledge.name == "search_houdini_knowledge"
    assert kb.get_houdini_knowledge.name == "get_houdini_knowledge"


def test_search_tool_args_match_spec() -> None:
    args = set(kb.search_houdini_knowledge.args)
    assert args == {
        "symbol", "query", "kinds", "context", "tag", "superclass",
        "predicate", "direction", "include_historical", "limit",
    }


def test_get_tool_args_match_spec() -> None:
    args = set(kb.get_houdini_knowledge.args)
    assert args == {"entity_id", "section", "max_chars"}


# --- input/output contract -------------------------------------------------


def test_search_tool_converts_request_and_returns_dto_dict(monkeypatch) -> None:
    response = SearchResponse(ok=True, code=None)
    service = _FakeService(search=response)
    _patch_service(monkeypatch, service)

    result = kb.search_houdini_knowledge.invoke({
        "symbol": "hou.Node",
        "query": None,
        "kinds": ["hom_class", "hom_method"],
        "context": "sop",
        "tag": "model",
        "superclass": "hou.NodeReferenceCounted",
        "predicate": "inherits_from",
        "direction": "incoming",
        "include_historical": True,
        "limit": 3,
    })

    assert result == response.to_dict()
    assert len(service.search_requests) == 1
    req = service.search_requests[0]
    assert isinstance(req, SearchRequest)
    assert req.symbol == "hou.Node"
    assert req.kinds == ("hom_class", "hom_method")
    assert req.context == "sop"
    assert req.tag == "model"
    assert req.superclass == "hou.NodeReferenceCounted"
    assert req.predicate == "inherits_from"
    assert req.direction == "incoming"
    assert req.include_historical is True
    assert req.limit == 3


def test_search_tool_defaults_when_minimal_input(monkeypatch) -> None:
    response = SearchResponse(ok=True, code=None)
    service = _FakeService(search=response)
    _patch_service(monkeypatch, service)

    kb.search_houdini_knowledge.invoke({"symbol": "hou.Node"})

    req = service.search_requests[0]
    assert req.direction == "outgoing"
    assert req.include_historical is False
    assert req.limit == 5
    assert req.kinds == ()


def test_get_tool_converts_request_and_returns_dto_dict(monkeypatch) -> None:
    response = GetResponse(
        ok=True, code=None, entity_id="hom_class:hou.Node",
        title="hou.Node", body="A class.", truncated=False,
    )
    service = _FakeService(get=response)
    _patch_service(monkeypatch, service)

    result = kb.get_houdini_knowledge.invoke({
        "entity_id": "hom_class:hou.Node", "section": "overview", "max_chars": 2000,
    })

    assert result == response.to_dict()
    req = service.get_requests[0]
    assert isinstance(req, GetRequest)
    assert req.entity_id == "hom_class:hou.Node"
    assert req.section == "overview"
    assert req.max_chars == 2000


# --- disabled state --------------------------------------------------------


def test_disabled_search_returns_kb_disabled_without_service(monkeypatch) -> None:
    monkeypatch.setenv("EEE_KB_ENABLED", "false")
    monkeypatch.setattr(kb, "_service", lambda: pytest.fail("disabled must not build"))

    result = kb.search_houdini_knowledge.invoke({"symbol": "x"})

    assert result["ok"] is False
    assert result["code"] == "kb_disabled"


def test_disabled_get_returns_kb_disabled_without_service(monkeypatch) -> None:
    monkeypatch.setenv("EEE_KB_ENABLED", "false")
    monkeypatch.setattr(kb, "_service", lambda: pytest.fail("disabled must not build"))

    result = kb.get_houdini_knowledge.invoke({"entity_id": "x"})

    assert result["ok"] is False
    assert result["code"] == "kb_disabled"


# --- never raise -----------------------------------------------------------


def test_search_tool_never_raises_on_service_exception(monkeypatch) -> None:
    service = _FakeService(raise_exc=RuntimeError("boom"))
    _patch_service(monkeypatch, service)

    result = kb.search_houdini_knowledge.invoke({"symbol": "x"})

    assert result["ok"] is False
    assert result["code"] == "internal_error"


def test_get_tool_never_raises_when_service_factory_fails(monkeypatch) -> None:
    def _boom():
        raise RuntimeError("cannot build service")

    monkeypatch.setattr(kb, "_service", _boom)

    result = kb.get_houdini_knowledge.invoke({"entity_id": "x"})

    assert result["ok"] is False
    assert result["code"] == "internal_error"


# --- no import-time / query-time I/O ---------------------------------------


def test_import_does_not_open_database_or_build_cache(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(kb, "_SERVICE", None)

    cfg = knowledge_config()

    # Importing the adapter module must not construct a service ...
    assert kb._SERVICE is None
    # ... nor create the cache file.
    assert not Path(cfg.path).exists()


def test_disabled_query_does_not_create_cache_file(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("EEE_KB_ENABLED", "false")

    kb.search_houdini_knowledge.invoke({"symbol": "x"})

    assert not Path(knowledge_config().path).exists()
