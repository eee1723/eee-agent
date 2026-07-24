"""Tests for the dynamic pitfall registry (knowledge-check gating).

The registry lets an operator grow a list of "node types the model historically
gets wrong" via EEE_PITFALL_NODES, instead of forcing a KB query before every
node creation. Empty default = trust the model's common sense.
"""
from __future__ import annotations

import importlib

import pytest


def _reload(mod):
    """Reload so env changes take effect for the module-level cache."""
    importlib.reload(mod)


def test_empty_registry_by_default(monkeypatch):
    monkeypatch.delenv("EEE_PITFALL_NODES", raising=False)
    from eee_agent import known_pitfalls
    _reload(known_pitfalls)
    assert known_pitfalls.known_pitfalls() == ()
    assert known_pitfalls.any_pitfalls_registered() is False
    # An empty registry requires NO kb check, even for obscure nodes.
    assert known_pitfalls.requires_knowledge_check("sweep2") is False
    assert known_pitfalls.requires_knowledge_check("anything") is False


def test_env_populates_registry(monkeypatch):
    monkeypatch.setenv("EEE_PITFALL_NODES", "copytopoints2, polyextrude2 ,,sweep2")
    from eee_agent import known_pitfalls
    _reload(known_pitfalls)
    assert known_pitfalls.known_pitfalls() == ("copytopoints2", "polyextrude2", "sweep2")
    assert known_pitfalls.any_pitfalls_registered() is True


def test_requires_check_matches_registered(monkeypatch):
    monkeypatch.setenv("EEE_PITFALL_NODES", "copytopoints2,polyextrude2")
    from eee_agent import known_pitfalls
    _reload(known_pitfalls)
    assert known_pitfalls.requires_knowledge_check("copytopoints2") is True
    assert known_pitfalls.requires_knowledge_check("polyextrude2") is True
    # Unregistered common nodes don't require a check.
    assert known_pitfalls.requires_knowledge_check("grid") is False
    assert known_pitfalls.requires_knowledge_check("box") is False


def test_prefix_match_covers_versioned_variants(monkeypatch):
    # Registering "copytopoints" (no version) should cover "copytopoints2" too,
    # so the operator doesn't enumerate every versioned variant.
    monkeypatch.setenv("EEE_PITFALL_NODES", "copytopoints")
    from eee_agent import known_pitfalls
    _reload(known_pitfalls)
    assert known_pitfalls.requires_knowledge_check("copytopoints2") is True
    assert known_pitfalls.requires_knowledge_check("copytopoints") is True
    # Reverse is NOT true: registering "copytopoints2" doesn't match bare "copytopoints".
    monkeypatch.setenv("EEE_PITFALL_NODES", "copytopoints2")
    _reload(known_pitfalls)
    assert known_pitfalls.requires_knowledge_check("copytopoints") is False


def test_match_is_case_insensitive(monkeypatch):
    monkeypatch.setenv("EEE_PITFALL_NODES", "Sweep2")
    from eee_agent import known_pitfalls
    _reload(known_pitfalls)
    assert known_pitfalls.requires_knowledge_check("sweep2") is True
    assert known_pitfalls.requires_knowledge_check("SWEEP2") is True


def test_garbage_input_is_safe(monkeypatch):
    monkeypatch.setenv("EEE_PITFALL_NODES", "copytopoints")
    from eee_agent import known_pitfalls
    _reload(known_pitfalls)
    assert known_pitfalls.requires_knowledge_check(None) is False
    assert known_pitfalls.requires_knowledge_check(123) is False
    assert known_pitfalls.requires_knowledge_check("") is False
    assert known_pitfalls.requires_knowledge_check("   ") is False


def test_whitespace_only_env_is_empty(monkeypatch):
    monkeypatch.setenv("EEE_PITFALL_NODES", "  ,  ,  ")
    from eee_agent import known_pitfalls
    _reload(known_pitfalls)
    assert known_pitfalls.known_pitfalls() == ()
    assert known_pitfalls.any_pitfalls_registered() is False


@pytest.fixture(autouse=True)
def _reset_registry():
    """Ensure each test starts from a clean registry state."""
    yield
    import os
    os.environ.pop("EEE_PITFALL_NODES", None)
    from eee_agent import known_pitfalls
    _reload(known_pitfalls)
