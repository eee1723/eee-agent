"""Tests for the invalid-tool-call guard middleware (H1 fix).

The guard prevents the 2026-07-24 incident: a provider streamed a tool call
whose args JSON was invalid (bareword ``POINTS_GRID``); LangChain demoted it to
``invalid_tool_calls``; the routing edge saw ``tool_calls == []`` and exited the
loop, finalizing the run on mid-task text. The middleware promotes such calls
to answered error tool calls so the model retries.
"""
from __future__ import annotations

from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from eee_agent.tool_call_guard import (
    InvalidToolCallGuardMiddleware,
    _error_content,
    is_enabled,
)


def _ai_with_invalid(*, call_id="call_1", name="scratch_build", args='{"v":BARE}', error="bad json"):
    return AIMessage(
        content="working on it...",
        tool_calls=[],
        invalid_tool_calls=[
            {
                "type": "invalid_tool_call",
                "id": call_id,
                "name": name,
                "args": args,
                "error": error,
            }
        ],
    )


def _state(*messages):
    return {"messages": list(messages)}


mw = InvalidToolCallGuardMiddleware()


class _NoRuntime:
    """The guard never touches runtime; pass None just like the factory does."""


def _apply(state):
    return mw.after_model(state, _NoRuntime())


# ── No-op cases ──────────────────────────────────────────────────────────────


def test_no_patch_when_no_invalid_calls():
    good = AIMessage(content="done", tool_calls=[{"name": "x", "args": {}, "id": "g1", "type": "tool_call"}])
    assert _apply(_state(HumanMessage(content="hi"), good)) is None


def test_no_patch_when_no_ai_message():
    assert _apply(_state(HumanMessage(content="hi"))) is None


def test_no_patch_when_disabled(monkeypatch):
    monkeypatch.setenv("EEE_TOOL_CALL_GUARD", "false")
    ai = _ai_with_invalid()
    assert _apply(_state(HumanMessage(content="hi"), ai)) is None


def test_is_enabled_default_true(monkeypatch):
    monkeypatch.delenv("EEE_TOOL_CALL_GUARD", raising=False)
    assert is_enabled() is True
    monkeypatch.setenv("EEE_TOOL_CALL_GUARD", "false")
    assert is_enabled() is False


# ── Core promotion ───────────────────────────────────────────────────────────


def test_invalid_call_promoted_to_answered_error_reply():
    ai = _ai_with_invalid(call_id="call_bad", args='{"value":POINTS_GRID}', error="bareword")
    result = _apply(_state(HumanMessage(content="make cones"), ai))

    assert result is not None
    msgs = result["messages"]
    # First element clears history (RemoveMessage sentinel), then originals, then repairs.
    assert msgs[0].type == "remove"
    # The rewritten AIMessage must now carry the promoted call as a real tool_call.
    rewritten_ai = [m for m in msgs if isinstance(m, AIMessage)]
    assert len(rewritten_ai) == 1
    assert rewritten_ai[0].tool_calls == [
        {"name": "scratch_build", "args": {}, "id": "call_bad", "type": "tool_call"}
    ]
    # invalid_tool_calls is cleared on the rewrite (it has been promoted).
    assert rewritten_ai[0].invalid_tool_calls == []
    # A paired error ToolMessage with the matching id is appended AFTER the AI msg.
    tool_msgs = [m for m in msgs if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].tool_call_id == "call_bad"
    assert tool_msgs[0].name == "scratch_build"
    assert "malformed" in tool_msgs[0].content.lower()
    # The raw args fragment (the diagnostic) is included so the model can fix it.
    assert "POINTS_GRID" in tool_msgs[0].content


def test_original_content_preserved_on_rewrite():
    ai = AIMessage(
        content="halfway through the task",
        tool_calls=[],
        invalid_tool_calls=[
            {"type": "invalid_tool_call", "id": "c1", "name": "scratch_build", "args": "x", "error": "e"}
        ],
    )
    result = _apply(_state(HumanMessage(content="hi"), ai))
    rewritten = [m for m in result["messages"] if isinstance(m, AIMessage)][0]
    assert rewritten.content == "halfway through the task"


def test_existing_good_tool_calls_preserved_alongside_promoted():
    ai = AIMessage(
        content="multi",
        tool_calls=[{"name": "scene_status", "args": {}, "id": "good1", "type": "tool_call"}],
        invalid_tool_calls=[
            {"type": "invalid_tool_call", "id": "bad1", "name": "scratch_build", "args": "x", "error": "e"}
        ],
    )
    result = _apply(_state(HumanMessage(content="hi"), ai))
    rewritten = [m for m in result["messages"] if isinstance(m, AIMessage)][0]
    ids = {c["id"] for c in rewritten.tool_calls}
    assert ids == {"good1", "bad1"}


def test_multiple_invalid_calls_all_promoted():
    ai = AIMessage(
        content="x",
        tool_calls=[],
        invalid_tool_calls=[
            {"type": "invalid_tool_call", "id": "a", "name": "scratch_build", "args": "x", "error": "e"},
            {"type": "invalid_tool_call", "id": "b", "name": "scratch_commit", "args": "y", "error": "e2"},
        ],
    )
    result = _apply(_state(HumanMessage(content="hi"), ai))
    tool_msgs = [m for m in result["messages"] if isinstance(m, ToolMessage)]
    assert {m.tool_call_id for m in tool_msgs} == {"a", "b"}


# ── Repair budget ────────────────────────────────────────────────────────────


def test_repair_budget_exhausted_stops_patch():
    ai = _ai_with_invalid(call_id="c1")
    # Three prior repair replies for the same id → at the default limit of 3.
    prior = [ToolMessage(content="err", name="scratch_build", tool_call_id="c1") for _ in range(3)]
    assert _apply(_state(HumanMessage(content="hi"), ai, *prior)) is None


def test_repair_budget_below_limit_still_patches(monkeypatch):
    monkeypatch.setenv("EEE_TOOL_CALL_REPAIR_LIMIT", "3")
    ai = _ai_with_invalid(call_id="c1")
    prior = [ToolMessage(content="err", name="scratch_build", tool_call_id="c1") for _ in range(2)]
    assert _apply(_state(HumanMessage(content="hi"), ai, *prior)) is not None


def test_repair_limit_env_override(monkeypatch):
    monkeypatch.setenv("EEE_TOOL_CALL_REPAIR_LIMIT", "1")
    ai = _ai_with_invalid(call_id="c1")
    # One prior reply + limit 1 → no patch.
    prior = [ToolMessage(content="err", name="scratch_build", tool_call_id="c1")]
    assert _apply(_state(HumanMessage(content="hi"), ai, *prior)) is None


# ── Error content helper ─────────────────────────────────────────────────────


def test_error_content_includes_name_error_and_args():
    text = _error_content("scratch_build", "call_x", "Unexpected token", '{"v":BAD}')
    assert "scratch_build" in text
    assert "call_x" in text
    assert "Unexpected token" in text
    assert '{"v":BAD}' in text


def test_error_content_bounds_huge_args():
    huge = "X" * 10_000
    text = _error_content("t", "i", None, huge)
    assert len(text) < 10_000
