"""Stage 6 Task 13 — strict config, registry and prompt integration contracts.

Covers the KnowledgeConfig contract, the exact 27-tool registry (both KB tools,
no implicit task), the compiled agent graph, READBACK_TOOLS exclusion and the
single compact knowledge prompt rule that preserves live introspection.
"""
from __future__ import annotations

import os

import pytest

from eee_agent.config import knowledge_config, repo_root
from eee_agent.context_trim import READBACK_TOOLS
from eee_agent.system_prompt import build_system_prompt
from eee_agent.tools.registry import ALL_TOOLS, all_tools


# --- KnowledgeConfig -------------------------------------------------------


def test_kb_enabled_defaults_to_true(monkeypatch) -> None:
    monkeypatch.delenv("EEE_KB_ENABLED", raising=False)
    assert knowledge_config().enabled is True


def test_kb_enabled_is_strict_boolean(monkeypatch) -> None:
    # Reuses the strict _env_bool semantics: an illegal value must raise at the
    # configuration boundary rather than silently disabling the tools.
    monkeypatch.setenv("EEE_KB_ENABLED", "maybe")
    with pytest.raises(ValueError):
        knowledge_config()


def test_kb_enabled_disabled_state(monkeypatch) -> None:
    monkeypatch.setenv("EEE_KB_ENABLED", "false")
    assert knowledge_config().enabled is False


def test_default_kb_cache_path_under_localappdata(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.delenv("EEE_KB_PATH", raising=False)
    expected = os.path.join(
        str(tmp_path),
        "EEEAgent", "cache", "knowledge", "houdini", "21.0.440",
        "knowledge.sqlite3",
    )
    assert knowledge_config().path == expected


def test_relative_kb_path_resolves_against_repo_root(monkeypatch) -> None:
    monkeypatch.setenv("EEE_KB_PATH", "relative/kb/knowledge.sqlite3")
    assert knowledge_config().path == os.path.join(
        repo_root(), "relative/kb/knowledge.sqlite3"
    )


def test_absolute_kb_path_is_preserved(monkeypatch) -> None:
    absolute = r"C:\some\where\knowledge.sqlite3"
    monkeypatch.setenv("EEE_KB_PATH", absolute)
    assert knowledge_config().path == absolute


def test_kb_hfs_override(monkeypatch) -> None:
    monkeypatch.setenv("EEE_HFS", r"D:\houdini")
    assert knowledge_config().hfs == r"D:\houdini"


def test_kb_hfs_defaults_to_none(monkeypatch) -> None:
    monkeypatch.delenv("EEE_HFS", raising=False)
    assert knowledge_config().hfs is None


# --- registry --------------------------------------------------------------


def test_all_tools_has_exactly_27_project_tools() -> None:
    assert len(all_tools()) == 27


def test_existing_tool_order_is_preserved() -> None:
    tools = all_tools()
    from eee_agent.tools import procedural, scene
    # The first and last of the original 25 Houdini tools are unchanged.
    assert tools[0] is scene.hou_status
    assert tools[24] is procedural.anchor_graph
    # The two knowledge tools are appended last.
    assert tools[25].name == "search_houdini_knowledge"
    assert tools[26].name == "get_houdini_knowledge"


def test_registry_contains_both_knowledge_tools() -> None:
    names = {getattr(t, "name", None) for t in all_tools()}
    assert {"search_houdini_knowledge", "get_houdini_knowledge"} <= names


def test_registry_has_no_task_tool() -> None:
    names = {getattr(t, "name", None) for t in all_tools()}
    assert "task" not in names


def test_all_tools_is_a_fresh_list_each_call() -> None:
    # all_tools() must return a copy so the module tuple stays immutable.
    assert all_tools() is not ALL_TOOLS


# --- compiled agent graph --------------------------------------------------


def test_compiled_graph_has_knowledge_tools_and_no_task(monkeypatch) -> None:
    monkeypatch.setenv("EEE_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-key")
    monkeypatch.setenv("EEE_COMPACT_TOOL", "false")
    monkeypatch.delenv("EEE_TRACING", raising=False)

    from eee_agent.app import build_agent

    graph = build_agent()
    names = set(graph.nodes["tools"].bound._tools_by_name)  # noqa: SLF001
    assert {"search_houdini_knowledge", "get_houdini_knowledge"} <= names
    assert "task" not in names


# --- READBACK_TOOLS --------------------------------------------------------


def test_get_houdini_knowledge_not_in_readback_tools() -> None:
    # Different entities' bodies are not interchangeable; trimming by tool name
    # would drop still-valid evidence (design §12.2).
    assert "get_houdini_knowledge" not in READBACK_TOOLS
    assert "search_houdini_knowledge" not in READBACK_TOOLS


# --- prompt ----------------------------------------------------------------


def test_prompt_contains_knowledge_rule_exactly_once() -> None:
    prompt = build_system_prompt()
    assert prompt.count("HOUDINI KNOWLEDGE TOOLS") == 1


def test_prompt_rule_preserves_live_introspection_authority() -> None:
    prompt = build_system_prompt()
    rule = prompt[prompt.find("HOUDINI KNOWLEDGE TOOLS"):]
    # The cache documents what official docs record ...
    assert "official Houdini documentation" in rule
    # ... it does NOT replace live introspection, which remains final authority.
    assert "describe_node_type" in rule
    assert "final authority" in rule
