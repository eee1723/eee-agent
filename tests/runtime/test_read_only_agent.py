from __future__ import annotations

import pytest

import eee_agent.app as app_module
from eee_agent.app import build_agent
from eee_agent.runtime.agent_tools import build_read_only_tools
from langgraph.checkpoint.memory import InMemorySaver

EXPECTED_SECURE_READ_ONLY = {
    "scene_status",
    "query_scene",
    "inspect_workspace",
    "geometry_stats",
    "work_status",
    "search_houdini_knowledge",
    "get_houdini_knowledge",
}
FORBIDDEN = {
    "task",
    "scene_reset",
    "save_hip",
    "create_node",
    "connect_nodes",
    "set_parms",
    "delete_node",
    "set_vex",
    "merge_nodes",
    "copy_to_points",
    "cook_node",
    "export_geometry",
    "ensure_work_container",
    "add_root_parm",
    "make_component",
    "expose_anchors",
    "wire_anchor",
    "set_expression",
    "assemble_output",
}


def _pin_offline_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("EEE_LLM_PROVIDER", "deepseek")
    monkeypatch.setenv("DEEPSEEK_API_KEY", "unit-test-key")
    monkeypatch.setenv("EEE_COMPACT_TOOL", "false")
    monkeypatch.delenv("EEE_TRACING", raising=False)


def _capture_kwargs(monkeypatch: pytest.MonkeyPatch) -> dict:
    """Monkeypatch the agent factory to capture create_deep_agent kwargs."""
    monkeypatch.setattr(app_module, "build_model", lambda: object())
    monkeypatch.setattr(app_module, "build_system_prompt", lambda: "prompt")
    monkeypatch.setenv("EEE_COMPACT_TOOL", "false")
    monkeypatch.delenv("EEE_CONTEXTSEEK", raising=False)
    monkeypatch.delenv("EEE_TRACING", raising=False)
    captured: dict = {}

    def fake_create_deep_agent(**kwargs):
        captured.update(kwargs)
        return object()

    monkeypatch.setattr(app_module, "create_deep_agent", fake_create_deep_agent)
    return captured


def _tool_names(graph) -> set[str]:
    return set(graph.nodes["tools"].bound._tools_by_name)  # noqa: SLF001


# --------------------------------------------------------------------------
# registry exactness
# --------------------------------------------------------------------------

def test_runtime_secure_tool_allowlist_omits_legacy_write_tools() -> None:
    tools = build_read_only_tools()
    names = {item.name for item in tools}
    assert names == EXPECTED_SECURE_READ_ONLY
    assert not names & FORBIDDEN


def test_runtime_allowlist_returns_independent_list() -> None:
    first = build_read_only_tools()
    second = build_read_only_tools()
    assert first is not second
    first.append(object())
    first.clear()
    third = build_read_only_tools()
    assert {t.name for t in third} == EXPECTED_SECURE_READ_ONLY
    assert len(third) == len(EXPECTED_SECURE_READ_ONLY)


# --------------------------------------------------------------------------
# real compiled read-only agent
# --------------------------------------------------------------------------

def test_read_only_agent_omits_write_and_task_tools(monkeypatch) -> None:
    _pin_offline_env(monkeypatch)
    graph = build_agent(tools=build_read_only_tools())
    tool_names = _tool_names(graph)
    assert EXPECTED_SECURE_READ_ONLY <= tool_names
    # Every Houdini write tool and the implicit `task` tool are absent. Since
    # FORBIDDEN plus EXPECTED_READ_ONLY covers all 25 Houdini tools, this proves
    # the agent's Houdini surface is exactly the read-only allowlist. (Deep
    # Agents also contributes its own built-in file/exec tools, which are not
    # Houdini operations and are outside this Task 8 boundary.)
    assert not (tool_names & FORBIDDEN)


def test_build_agent_requires_explicit_tools() -> None:
    with pytest.raises(TypeError, match="explicit secure tools"):
        build_agent()


# --------------------------------------------------------------------------
# tools injection seam
# --------------------------------------------------------------------------

def test_explicit_empty_tools_do_not_fall_back_to_all_tools(monkeypatch) -> None:
    captured = _capture_kwargs(monkeypatch)
    build_agent(tools=[])
    assert captured["tools"] == []


def test_tuple_tools_are_copied_to_a_list(monkeypatch) -> None:
    captured = _capture_kwargs(monkeypatch)
    tools_tuple = tuple(build_read_only_tools()[:3])
    build_agent(tools=tools_tuple)
    assert captured["tools"] == list(tools_tuple)
    assert type(captured["tools"]) is list


def test_build_agent_rejects_positional_args() -> None:
    with pytest.raises(TypeError):
        build_agent(build_read_only_tools())  # type: ignore[misc]


# --------------------------------------------------------------------------
# checkpointer injection seam
# --------------------------------------------------------------------------

def test_build_agent_accepts_checkpointer(monkeypatch) -> None:
    _pin_offline_env(monkeypatch)
    saver = InMemorySaver()
    graph = build_agent(tools=build_read_only_tools(), checkpointer=saver)
    assert graph.checkpointer is saver


def test_checkpointer_passed_through_when_provided(monkeypatch) -> None:
    captured = _capture_kwargs(monkeypatch)
    saver = InMemorySaver()
    build_agent(tools=[], checkpointer=saver)
    assert captured["checkpointer"] is saver


def test_checkpointer_none_omits_kwarg(monkeypatch) -> None:
    captured = _capture_kwargs(monkeypatch)
    build_agent(tools=[], checkpointer=None)
    assert "checkpointer" not in captured
