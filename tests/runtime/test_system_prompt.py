"""B-3 + Phase 3: the BASE_PROMPT must surface the sandbox+verify+commit
workflow so the LLM does not improvise.

The original B-3 concern (ses_65fefe0a5dbe41798d341d9362a27a0e) was that the
LLM had no feedback contract after a rolled-back apply. With the Phase 3
retirement of propose_modeling, the commit workflow replaces the old
typed-proposal+approval path: scratch_commit returns a bounded receipt the
LLM must reference (it cannot rewrite the numbers), and a refused commit
preserves the sandbox for retry. These tests assert the new contract is
present in the prompt.
"""
from __future__ import annotations

from eee_agent.system_prompt import BASE_PROMPT, build_system_prompt


def test_build_system_prompt_returns_base_prompt() -> None:
    assert build_system_prompt() == BASE_PROMPT


def test_prompt_lists_scratch_tools_as_available() -> None:
    # The iterative sandbox workflow tools must be visible to the LLM.
    assert "scratch_build" in BASE_PROMPT
    assert "scratch_commit" in BASE_PROMPT


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


def test_prompt_requires_kb_lookup_before_building() -> None:
    # The agent must query the knowledge base to confirm node types/parameters
    # before creating nodes, to avoid blind-guess failures.
    assert "search_houdini_knowledge" in BASE_PROMPT
