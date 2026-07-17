from __future__ import annotations

import pytest

import eee_agent.app as app_module
from eee_agent.app import build_agent
from eee_agent.runtime.agent_tools import build_read_only_tools
from eee_agent.tools.registry import all_tools, read_only_tools
from langgraph.checkpoint.memory import InMemorySaver

EXPECTED_READ_ONLY = {
    "hou_status",
    "find_nodes",
    "describe_node_type",
    "geometry_stats",
    "validate_geometry",
    "work_status",
    "anchor_graph",
}
EXPECTED_SECURE_READ_ONLY = {
    "scene_status",
    "query_scene",
    "inspect_workspace",
    "geometry_stats",
    "work_status",
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

def test_read_only_registry_is_exact() -> None:
    tools = read_only_tools()
    names = [t.name for t in tools]
    assert set(names) == EXPECTED_READ_ONLY
    assert len(tools) == 7
    assert len(set(names)) == 7  # no duplicates


def test_runtime_secure_tool_allowlist_omits_legacy_write_tools() -> None:
    tools = build_read_only_tools()
    names = {item.name for item in tools}
    assert names == EXPECTED_SECURE_READ_ONLY
    assert not names & FORBIDDEN


def test_read_only_registry_returns_independent_list() -> None:
    first = read_only_tools()
    second = read_only_tools()
    assert first is not second
    first.append(object())
    first.clear()
    third = read_only_tools()
    assert {t.name for t in third} == EXPECTED_READ_ONLY
    assert len(third) == 7


def test_read_only_registry_does_not_mutate_all_tools() -> None:
    from eee_agent.tools.registry import ALL_TOOLS

    before = [t.name for t in ALL_TOOLS]
    mutated = read_only_tools()
    mutated.clear()
    after = [t.name for t in ALL_TOOLS]
    assert before == after
    assert len(all_tools()) == len(ALL_TOOLS)


# --------------------------------------------------------------------------
# real compiled read-only agent
# --------------------------------------------------------------------------

def test_read_only_agent_omits_write_and_task_tools(monkeypatch) -> None:
    _pin_offline_env(monkeypatch)
    graph = build_agent(tools=read_only_tools())
    tool_names = _tool_names(graph)
    # The seven read-only Houdini tools are present.
    assert EXPECTED_READ_ONLY <= tool_names
    # Every Houdini write tool and the implicit `task` tool are absent. Since
    # FORBIDDEN plus EXPECTED_READ_ONLY covers all 25 Houdini tools, this proves
    # the agent's Houdini surface is exactly the read-only allowlist. (Deep
    # Agents also contributes its own built-in file/exec tools, which are not
    # Houdini operations and are outside this Task 8 boundary.)
    assert not (tool_names & FORBIDDEN)


def test_default_build_agent_has_create_node_and_no_task(monkeypatch) -> None:
    _pin_offline_env(monkeypatch)
    graph = build_agent()
    tool_names = _tool_names(graph)
    assert "create_node" in tool_names
    assert "task" not in tool_names


# --------------------------------------------------------------------------
# tools injection seam
# --------------------------------------------------------------------------

def test_explicit_empty_tools_do_not_fall_back_to_all_tools(monkeypatch) -> None:
    captured = _capture_kwargs(monkeypatch)
    build_agent(tools=[])
    assert captured["tools"] == []


def test_tuple_tools_are_copied_to_a_list(monkeypatch) -> None:
    captured = _capture_kwargs(monkeypatch)
    tools_tuple = tuple(read_only_tools()[:3])
    build_agent(tools=tools_tuple)
    assert captured["tools"] == list(tools_tuple)
    assert type(captured["tools"]) is list


def test_build_agent_rejects_positional_args() -> None:
    with pytest.raises(TypeError):
        build_agent(read_only_tools())  # type: ignore[misc]


# --------------------------------------------------------------------------
# checkpointer injection seam
# --------------------------------------------------------------------------

def test_build_agent_accepts_checkpointer(monkeypatch) -> None:
    _pin_offline_env(monkeypatch)
    saver = InMemorySaver()
    graph = build_agent(tools=read_only_tools(), checkpointer=saver)
    assert graph.checkpointer is saver


def test_checkpointer_passed_through_when_provided(monkeypatch) -> None:
    captured = _capture_kwargs(monkeypatch)
    saver = InMemorySaver()
    build_agent(checkpointer=saver)
    assert captured["checkpointer"] is saver


def test_checkpointer_none_omits_kwarg(monkeypatch) -> None:
    captured = _capture_kwargs(monkeypatch)
    build_agent(checkpointer=None)
    assert "checkpointer" not in captured
