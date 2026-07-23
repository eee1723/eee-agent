"""B-3: the BASE_PROMPT must surface propose_modeling and the rolled_back
feedback contract so the LLM does not improvise.

The reported symptom from ses_65fefe0a5dbe41798d341d9362a27a0e: after a
rolled-back apply (applied_op_ids=[]), the LLM described the scene as
"似乎仅部分应用" because it had no idea the apply had failed at all. The
tool list in the old prompt also omitted propose_modeling, so the LLM
could not tell that proposing a fresh spec was even possible.
"""
from __future__ import annotations

from eee_agent.system_prompt import BASE_PROMPT, build_system_prompt


def test_build_system_prompt_returns_base_prompt() -> None:
    assert build_system_prompt() == BASE_PROMPT


def test_prompt_lists_propose_modeling_as_available_tool() -> None:
    # The old prompt enumerated only the read-only tools and never mentioned
    # propose_modeling. The LLM must see the proposal tool to use it.
    assert "propose_modeling" in BASE_PROMPT


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


def test_prompt_describes_rolled_back_feedback_contract() -> None:
    # The LLM must read changeset.applied / changeset.rolled_back events and
    # branch on error_code; it must NOT infer the outcome from the scene.
    assert "changeset.rolled_back" in BASE_PROMPT
    assert "changeset.applied" in BASE_PROMPT
    assert "error_code" in BASE_PROMPT
    assert "error_message" in BASE_PROMPT
    # The "applied_op_ids=[] means zero ops, not partial" rule must be stated.
    assert "applied_op_ids=[]" in BASE_PROMPT


def test_prompt_branches_on_known_error_codes() -> None:
    # The prompt must tell the LLM how to react to each common error code.
    assert "houdini.operation_failed" in BASE_PROMPT
    assert "bridge.stale_scene" in BASE_PROMPT
    assert "apply.postcondition_failed" in BASE_PROMPT


def test_prompt_tells_user_to_approve_in_ui() -> None:
    # ses_65fefe0a5d symptom: the user approved but the LLM did not realize
    # approval was a UI action it could not observe directly. The prompt must
    # tell the LLM to ask the user to approve (批准) in the UI and not claim
    # success until the apply event arrives. The prompt is Chinese, so assert
    # the Chinese approval term rather than the English word.
    assert "批准" in BASE_PROMPT
