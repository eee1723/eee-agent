"""B-3 + Phase 3: the BASE_PROMPT must surface the sandbox+verify+commit
workflow so the LLM does not improvise.

The original B-3 concern (ses_65fefe0a5dbe41798d341d9362a27a0e) was that the
LLM had no feedback contract after a rolled-back apply. With the Phase 3
retirement of propose_modeling, the commit workflow replaces the old
typed-proposal+approval path: scratch_commit returns a bounded receipt the
LLM must reference (it cannot rewrite the numbers), and a refused commit
preserves the sandbox for retry. These tests assert the new contract is
present in the prompt.

The prompt also drives efficiency: it steers the agent to batch functional
units (not one node per turn) and to query the KB only for registered pitfall
nodes (not every node), grown dynamically via EEE_PITFALL_NODES.
"""
from __future__ import annotations

import importlib

from eee_agent.system_prompt import BASE_PROMPT


def test_build_system_prompt_returns_base_prompt_when_no_pitfalls(monkeypatch) -> None:
    # With no pitfalls registered, the dynamic clause is empty, so the built
    # prompt equals BASE_PROMPT exactly.
    monkeypatch.delenv("EEE_PITFALL_NODES", raising=False)
    from eee_agent import known_pitfalls
    importlib.reload(known_pitfalls)
    importlib.reload(importlib.import_module("eee_agent.system_prompt"))
    from eee_agent.system_prompt import build_system_prompt as bsp
    assert bsp() == BASE_PROMPT


def test_prompt_lists_scratch_tools_as_available() -> None:
    # The iterative sandbox workflow tools must be visible to the LLM.
    assert "scratch_build" in BASE_PROMPT
    assert "scratch_commit" in BASE_PROMPT
    assert "render_sketch" in BASE_PROMPT
    assert "verify_geometry" in BASE_PROMPT


def test_prompt_requires_html_route_for_procedural_assets() -> None:
    assert "程序化" in BASE_PROMPT
    assert "HTML → 用户审核 → Houdini" in BASE_PROMPT
    assert "禁止从 scene_status 直接进入 scratch_build" in BASE_PROMPT
    assert "等待用户明确批准草图" in BASE_PROMPT
    assert "render_sketch 成功不等于用户批准" in BASE_PROMPT
    assert "procedural-modeling html-to-houdini workflow" in BASE_PROMPT


def test_prompt_documents_every_read_only_tool() -> None:
    for tool in (
        "scene_status",
        "query_scene",
        "inspect_workspace",
        "geometry_stats",
        "work_status",
        "search_houdini_knowledge",
        "get_houdini_knowledge",
    ):
        assert tool in BASE_PROMPT, f"prompt must mention {tool}"


def test_prompt_describes_commit_receipt_contract() -> None:
    # The LLM must read the scratch_commit receipt fields and branch on
    # committed/refused; it must NOT fabricate a committed result.
    assert "receipt" in BASE_PROMPT
    assert "committed" in BASE_PROMPT
    assert "refused" in BASE_PROMPT


def test_prompt_describes_refused_commit_preserves_sandbox() -> None:
    # A refused commit must tell the LLM the sandbox is preserved for retry,
    # not to blindly re-commit.
    assert "沙箱保留" in BASE_PROMPT or "保留" in BASE_PROMPT


def test_prompt_lists_verify_gates() -> None:
    # The four hard gates must be named so the LLM knows what can refuse.
    for gate in ("bake", "structure", "orientation", "health"):
        assert gate in BASE_PROMPT, f"prompt must mention the {gate} gate"


def test_prompt_instructs_agent_to_use_todos_for_modeling() -> None:
    # deepagents injects write_todos but its built-in description tells the
    # model to SKIP todos for "simple" tasks — which is why modeling runs
    # produced todos_json=None. Our prompt must explicitly require write_todos
    # for modeling so the UI's plan view is populated.
    assert "write_todos" in BASE_PROMPT
    assert "建模" in BASE_PROMPT


def test_prompt_directs_kb_unavailable_to_rebuild_button() -> None:
    # When search_houdini_knowledge returns kb_unavailable, the agent must tell
    # the user to click "Rebuild KB" instead of blindly retrying.
    assert "kb_unavailable" in BASE_PROMPT
    assert "Rebuild KB" in BASE_PROMPT
    assert "search_houdini_knowledge" in BASE_PROMPT


def test_prompt_no_longer_forces_kb_before_every_node() -> None:
    # The old unconditional "必须先用 search_houdini_knowledge 查询你要使用的节点
    # 类型" mandate wasted ~38% of tool calls on doc queries. The new prompt
    # trusts common-sense nodes and only queries for unfamiliar/pitfall nodes.
    assert "必须先用 search_houdini_knowledge 查询你要使用的节点类型" not in BASE_PROMPT
    # It now frames KB use as on-demand, not mandatory-before-everything.
    assert "按需查询" in BASE_PROMPT or "常见节点" in BASE_PROMPT


def test_prompt_encourages_functional_unit_batching() -> None:
    # The agent must batch a functional unit per scratch_build call (not one
    # node per turn) to cut turns and repeated-input tokens.
    assert "功能单元" in BASE_PROMPT


def test_build_system_prompt_injects_pitfalls_dynamically(monkeypatch) -> None:
    # When pitfalls are registered, the built prompt appends a clause naming
    # them as KB-required — grown from observed failures, not a static list.
    monkeypatch.setenv("EEE_PITFALL_NODES", "copytopoints2,sweep2")
    from eee_agent import known_pitfalls
    importlib.reload(known_pitfalls)
    importlib.reload(importlib.import_module("eee_agent.system_prompt"))
    from eee_agent.system_prompt import build_system_prompt as bsp
    prompt = bsp()
    assert "特别注意的易错节点" in prompt
    assert "copytopoints2" in prompt
    assert "sweep2" in prompt
    # BASE_PROMPT itself is untouched (the clause is appended at build time).
    assert "特别注意的易错节点" not in BASE_PROMPT


def test_build_system_prompt_omits_clause_when_no_pitfalls(monkeypatch) -> None:
    monkeypatch.delenv("EEE_PITFALL_NODES", raising=False)
    from eee_agent import known_pitfalls
    importlib.reload(known_pitfalls)
    importlib.reload(importlib.import_module("eee_agent.system_prompt"))
    from eee_agent.system_prompt import build_system_prompt as bsp
    assert "特别注意的易错节点" not in bsp()
