from __future__ import annotations

import asyncio
from contextlib import aclosing
from typing import get_type_hints

import pytest
from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage

from eee_agent.config import recursion_limit
from eee_agent.providers.events import (
    ReasoningDelta,
    TextDelta,
    ToolCallArgumentsDelta,
    ToolCallStarted,
    UsageUpdated,
)
from eee_agent.runtime.agent_runner import (
    AgentRunner,
    RunnerCompleted,
    RunnerEvent,
    build_agent_runner,
)
from eee_agent.runtime.models import RetentionClass

EXPECTED_READ_ONLY = {
    "scene_status",
    "query_scene",
    "inspect_workspace",
    "geometry_stats",
    "work_status",
}


def _run(coro):
    return asyncio.run(coro)


class _FakeStream:
    def __init__(self, items, state, close_error=None):
        self._items = items
        self._i = 0
        self._state = state
        self._close_error = close_error

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self._i >= len(self._items):
            raise StopAsyncIteration
        item = self._items[self._i]
        self._i += 1
        if isinstance(item, BaseException):
            raise item
        return item

    async def aclose(self):
        if self._close_error is not None:
            self._state["close_attempted"] = True
            raise self._close_error
        self._state["closed"] = True


class _FakeGraph:
    def __init__(self, items, close_error=None):
        self._items = items
        self._close_error = close_error
        self.state = {"closed": False, "close_attempted": False}
        self.astream_calls = []

    def astream(self, input, *, config=None, stream_mode=None, context=None):
        self.astream_calls.append(
            {
                "input": input,
                "config": config,
                "stream_mode": list(stream_mode) if stream_mode is not None else None,
                "context": context,
            }
        )
        return _FakeStream(self._items, self.state, self._close_error)


async def _drain(runner, *, session_id, user_input):
    out = []
    async for ev in runner.stream(session_id=session_id, user_input=user_input):
        out.append(ev)
    return out


def _ev_types(events):
    return [e.event_type for e in events if isinstance(e, RunnerEvent)]


# --------------------------------------------------------------------------
# public dataclasses
# --------------------------------------------------------------------------

def test_runner_event_is_frozen_and_slotted() -> None:
    ev = RunnerEvent("model.text_delta", {"text": "hi"}, RetentionClass.OPERATIONAL)
    assert ev.event_type == "model.text_delta"
    assert ev.payload == {"text": "hi"}
    assert ev.retention_class is RetentionClass.OPERATIONAL
    with pytest.raises(AttributeError):
        ev.event_type = "x"
    assert not hasattr(ev, "__dict__")


def test_runner_completed_is_frozen_and_slotted() -> None:
    done = RunnerCompleted("done", {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3})
    assert done.final_response == "done"
    assert done.usage == {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}
    with pytest.raises(AttributeError):
        done.final_response = "x"
    assert not hasattr(done, "__dict__")


def test_public_type_hints() -> None:
    ev_hints = get_type_hints(RunnerEvent)
    assert ev_hints["event_type"] is str
    assert ev_hints["retention_class"] is RetentionClass
    done_hints = get_type_hints(RunnerCompleted)
    assert done_hints["final_response"] is str


# --------------------------------------------------------------------------
# graph call contract
# --------------------------------------------------------------------------

def test_graph_receives_correct_input_config_stream_mode() -> None:
    async def scenario() -> None:
        graph = _FakeGraph([])
        runner = AgentRunner(graph)
        await _drain(runner, session_id="ses_abc", user_input="hello")
        call = graph.astream_calls[0]
        assert call["input"] == {"messages": [{"role": "user", "content": "hello"}]}
        assert call["config"]["configurable"]["thread_id"] == "ses_abc"
        assert call["config"]["recursion_limit"] == recursion_limit()
        assert call["stream_mode"] == ["messages", "updates"]
        assert call["context"] is None
        assert graph.state["closed"] is True

    _run(scenario())


def test_opt_in_context_factory_is_frozen_into_one_graph_run() -> None:
    async def scenario() -> None:
        graph = _FakeGraph([])
        runner = AgentRunner(graph)

        async def context_factory(session_id: str, run_id: str):
            return {"session_id": session_id, "run_id": run_id}

        runner.set_context_factory(context_factory)
        await _drain(
            runner,
            session_id="ses_abc",
            user_input="hello",
        )
        # The direct drain helper has no run_id, so the factory requirement is
        # fail-closed before a graph call.

    with pytest.raises(ValueError, match="run_id is required"):
        _run(scenario())

    async def successful() -> None:
        graph = _FakeGraph([])
        runner = AgentRunner(graph)
        runner.set_context_factory(
            lambda session_id, run_id: {
                "session_id": session_id,
                "run_id": run_id,
            }
        )
        events = []
        async for event in runner.stream(
            session_id="ses_abc",
            run_id="run_def",
            user_input="hello",
        ):
            events.append(event)
        assert events[-1].final_response == ""
        assert graph.astream_calls[0]["context"] == {
            "session_id": "ses_abc",
            "run_id": "run_def",
        }

    _run(successful())


# --------------------------------------------------------------------------
# combined chunk -> event mapping
# --------------------------------------------------------------------------

def test_combined_chunk_maps_to_ordered_events() -> None:
    chunk = AIMessageChunk(
        content=[{"type": "thinking", "thinking": "why"}, {"type": "text", "text": "hello"}],
        tool_call_chunks=[{"id": "c1", "name": "find_nodes", "args": "", "index": 0}],
        usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15},
    )

    async def scenario() -> None:
        graph = _FakeGraph([("messages", (chunk, {}))])
        runner = AgentRunner(graph)
        events = await _drain(runner, session_id="ses_x", user_input="hi")
        assert _ev_types(events) == [
            "model.reasoning_delta",
            "model.text_delta",
            "tool.started",
            "model.usage_updated",
            "model.completed",
        ]
        op = [e for e in events if isinstance(e, RunnerEvent)]
        assert op[0].payload == {"text": "why"}
        assert op[0].retention_class is RetentionClass.OPERATIONAL
        assert op[1].payload == {"text": "hello"}
        assert op[2].payload == {"call_id": "c1", "name": "find_nodes", "index": 0}
        assert op[3].payload == {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
        assert op[4].retention_class is RetentionClass.DURABLE
        assert events[-1] == RunnerCompleted(
            "hello", {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
        )

    _run(scenario())


def test_continuation_tool_chunk_has_no_started_event() -> None:
    chunk = AIMessageChunk(
        content="",
        tool_call_chunks=[{"id": "", "name": "", "args": '{"x":1', "index": 0}],
    )

    async def scenario() -> None:
        graph = _FakeGraph([("messages", (chunk, {}))])
        runner = AgentRunner(graph)
        events = await _drain(runner, session_id="ses_x", user_input="hi")
        types = _ev_types(events)
        assert "tool.started" not in types
        assert "tool.arguments_delta" in types
        arg = next(e for e in events if isinstance(e, RunnerEvent) and e.event_type == "tool.arguments_delta")
        assert arg.payload == {"call_id": "", "arguments_delta": '{"x":1', "index": 0}

    _run(scenario())


# --------------------------------------------------------------------------
# ToolMessage -> tool.completed + preview
# --------------------------------------------------------------------------

def test_tool_message_emits_completed() -> None:
    tm = ToolMessage(content="result data", tool_call_id="c1", name="find_nodes")

    async def scenario() -> None:
        graph = _FakeGraph([("messages", (tm, {}))])
        runner = AgentRunner(graph)
        events = await _drain(runner, session_id="ses_x", user_input="hi")
        tc = next(e for e in events if isinstance(e, RunnerEvent) and e.event_type == "tool.completed")
        assert tc.payload == {
            "call_id": "c1",
            "name": "find_nodes",
            "content": "result data",
            "truncated": False,
        }
        assert tc.retention_class is RetentionClass.OPERATIONAL
        # tool output is not folded into the final response
        assert events[-1].final_response == ""

    _run(scenario())


def test_tool_preview_600_not_truncated() -> None:
    body = "x" * 600
    tm = ToolMessage(content=body, tool_call_id="c1", name="t")

    async def scenario() -> None:
        graph = _FakeGraph([("messages", (tm, {}))])
        runner = AgentRunner(graph)
        events = await _drain(runner, session_id="ses_x", user_input="hi")
        tc = next(e for e in events if isinstance(e, RunnerEvent) and e.event_type == "tool.completed")
        assert tc.payload["truncated"] is False
        assert tc.payload["content"] == body

    _run(scenario())


def test_tool_preview_601_truncated() -> None:
    body = "y" * 601
    tm = ToolMessage(content=body, tool_call_id="c1", name="t")

    async def scenario() -> None:
        graph = _FakeGraph([("messages", (tm, {}))])
        runner = AgentRunner(graph)
        events = await _drain(runner, session_id="ses_x", user_input="hi")
        tc = next(e for e in events if isinstance(e, RunnerEvent) and e.event_type == "tool.completed")
        assert tc.payload["truncated"] is True
        assert tc.payload["content"] == "y" * 600
        assert len(tc.payload["content"]) == 600

    _run(scenario())


def test_tool_non_string_content_is_stringified() -> None:
    tm = ToolMessage(content={"k": "v"}, tool_call_id="c1", name="t")

    async def scenario() -> None:
        graph = _FakeGraph([("messages", (tm, {}))])
        runner = AgentRunner(graph)
        events = await _drain(runner, session_id="ses_x", user_input="hi")
        tc = next(e for e in events if isinstance(e, RunnerEvent) and e.event_type == "tool.completed")
        assert isinstance(tc.payload["content"], str)
        assert tc.payload["truncated"] is False

    _run(scenario())


def test_tool_message_missing_call_id_defaults_to_empty() -> None:
    tm = ToolMessage(content="x", tool_call_id="", name="")

    async def scenario() -> None:
        graph = _FakeGraph([("messages", (tm, {}))])
        runner = AgentRunner(graph)
        events = await _drain(runner, session_id="ses_x", user_input="hi")
        tc = next(e for e in events if isinstance(e, RunnerEvent) and e.event_type == "tool.completed")
        assert tc.payload["call_id"] == ""
        assert tc.payload["name"] == "tool"

    _run(scenario())


# --------------------------------------------------------------------------
# final response semantics
# --------------------------------------------------------------------------

def test_text_delta_fallback_when_no_final_message() -> None:
    async def scenario() -> None:
        graph = _FakeGraph([
            ("messages", (AIMessageChunk(content="foo"), {})),
            ("messages", (AIMessageChunk(content="bar"), {})),
        ])
        runner = AgentRunner(graph)
        events = await _drain(runner, session_id="ses_x", user_input="hi")
        assert events[-1].final_response == "foobar"

    _run(scenario())


def test_final_aimessage_is_authoritative() -> None:
    async def scenario() -> None:
        graph = _FakeGraph([
            ("messages", (AIMessageChunk(content="partial"), {})),
            ("updates", {"model": {"messages": [AIMessage(content="full answer")]}}),
        ])
        runner = AgentRunner(graph)
        events = await _drain(runner, session_id="ses_x", user_input="hi")
        assert events[-1].final_response == "full answer"
        # the final AIMessage is not re-emitted as a text delta
        deltas = [e for e in events if isinstance(e, RunnerEvent) and e.event_type == "model.text_delta"]
        assert [d.payload["text"] for d in deltas] == ["partial"]

    _run(scenario())


def test_explicit_empty_final_message_is_respected() -> None:
    async def scenario() -> None:
        graph = _FakeGraph([
            ("messages", (AIMessageChunk(content="partial"), {})),
            ("updates", {"model": {"messages": [AIMessage(content="")]}}),
        ])
        runner = AgentRunner(graph)
        events = await _drain(runner, session_id="ses_x", user_input="hi")
        assert events[-1].final_response == ""

    _run(scenario())


def test_reasoning_excluded_from_final_response() -> None:
    async def scenario() -> None:
        graph = _FakeGraph([
            ("updates", {
                "model": {"messages": [AIMessage(content=[
                    {"type": "thinking", "thinking": "secret"},
                    {"type": "text", "text": "answer"},
                ])]}
            }),
        ])
        runner = AgentRunner(graph)
        events = await _drain(runner, session_id="ses_x", user_input="hi")
        assert events[-1].final_response == "answer"

    _run(scenario())


def test_multiple_text_blocks_concatenated() -> None:
    async def scenario() -> None:
        graph = _FakeGraph([
            ("updates", {
                "model": {"messages": [AIMessage(content=[
                    {"type": "text", "text": "foo"},
                    {"type": "text", "text": "bar"},
                ])]}
            }),
        ])
        runner = AgentRunner(graph)
        events = await _drain(runner, session_id="ses_x", user_input="hi")
        assert events[-1].final_response == "foobar"

    _run(scenario())


def test_last_aimessage_across_nodes_wins() -> None:
    async def scenario() -> None:
        graph = _FakeGraph([
            ("updates", {"a": {"messages": [AIMessage(content="first")]}}),
            ("updates", {"b": {"messages": [AIMessage(content="second")]}}),
        ])
        runner = AgentRunner(graph)
        events = await _drain(runner, session_id="ses_x", user_input="hi")
        assert events[-1].final_response == "second"

    _run(scenario())


def test_empty_stream_yields_empty_response() -> None:
    async def scenario() -> None:
        graph = _FakeGraph([])
        runner = AgentRunner(graph)
        events = await _drain(runner, session_id="ses_x", user_input="hi")
        assert events[-1].final_response == ""
        assert events[-1].usage == {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

    _run(scenario())


# --------------------------------------------------------------------------
# usage
# --------------------------------------------------------------------------

def test_usage_accumulates_across_chunks() -> None:
    async def scenario() -> None:
        graph = _FakeGraph([
            ("messages", (AIMessageChunk(content="", usage_metadata={"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}), {})),
            ("messages", (AIMessageChunk(content="", usage_metadata={"input_tokens": 3, "output_tokens": 2, "total_tokens": 5}), {})),
        ])
        runner = AgentRunner(graph)
        events = await _drain(runner, session_id="ses_x", user_input="hi")
        assert events[-1].usage == {"input_tokens": 13, "output_tokens": 7, "total_tokens": 20}

    _run(scenario())


def test_usage_dicts_are_independent() -> None:
    async def scenario() -> None:
        graph = _FakeGraph([
            ("messages", (AIMessageChunk(content="x", usage_metadata={"input_tokens": 1, "output_tokens": 2, "total_tokens": 3}), {})),
        ])
        runner = AgentRunner(graph)
        events = await _drain(runner, session_id="ses_x", user_input="hi")
        completed_payload = next(
            e.payload for e in events if isinstance(e, RunnerEvent) and e.event_type == "model.completed"
        )
        assert completed_payload["usage"] == events[-1].usage
        assert completed_payload["usage"] is not events[-1].usage

    _run(scenario())


# --------------------------------------------------------------------------
# success termination
# --------------------------------------------------------------------------

def test_success_yields_one_completed_then_one_terminal() -> None:
    async def scenario() -> None:
        graph = _FakeGraph([("messages", (AIMessageChunk(content="hi"), {}))])
        runner = AgentRunner(graph)
        events = await _drain(runner, session_id="ses_x", user_input="hi")
        assert _ev_types(events).count("model.completed") == 1
        assert sum(1 for e in events if isinstance(e, RunnerCompleted)) == 1
        assert isinstance(events[-1], RunnerCompleted)
        assert isinstance(events[-2], RunnerEvent)
        assert events[-2].event_type == "model.completed"

    _run(scenario())


# --------------------------------------------------------------------------
# exceptions / cancellation / early close
# --------------------------------------------------------------------------

def test_graph_exception_propagates_without_terminal_yields() -> None:
    async def scenario() -> None:
        graph = _FakeGraph([
            ("messages", (AIMessageChunk(content="x"), {})),
            ValueError("graph boom"),
        ])
        runner = AgentRunner(graph)
        events: list = []
        with pytest.raises(ValueError, match="graph boom"):
            async for ev in runner.stream(session_id="ses_x", user_input="hi"):
                events.append(ev)
        assert not any(isinstance(e, RunnerCompleted) for e in events)
        assert "model.completed" not in _ev_types(events)
        assert graph.state["closed"] is True

    _run(scenario())


def test_custom_base_exception_propagates() -> None:
    class Boom(BaseException):
        pass

    async def scenario() -> None:
        graph = _FakeGraph([Boom()])
        runner = AgentRunner(graph)
        with pytest.raises(Boom):
            async for _ev in runner.stream(session_id="ses_x", user_input="hi"):
                pass

    _run(scenario())


def test_cancellation_propagates_and_closes_stream() -> None:
    async def scenario() -> None:
        graph = _FakeGraph([("messages", (AIMessageChunk(content="x"), {}))])
        runner = AgentRunner(graph)
        started = asyncio.Event()

        async def consume() -> None:
            async with aclosing(runner.stream(session_id="ses_x", user_input="hi")) as gen:
                async for _ev in gen:
                    started.set()
                    await asyncio.sleep(3600)

        task = asyncio.create_task(consume())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert graph.state["closed"] is True

    _run(scenario())


def test_consumer_aclose_closes_underlying_stream() -> None:
    async def scenario() -> None:
        graph = _FakeGraph([
            ("messages", (AIMessageChunk(content="a"), {})),
            ("messages", (AIMessageChunk(content="b"), {})),
        ])
        runner = AgentRunner(graph)
        gen = runner.stream(session_id="ses_x", user_input="hi")
        first = await gen.__anext__()
        assert isinstance(first, RunnerEvent)
        await gen.aclose()
        assert graph.state["closed"] is True

    _run(scenario())


# --------------------------------------------------------------------------
# cleanup-failure exception priority
# --------------------------------------------------------------------------

def test_normal_completion_propagates_cleanup_failure_without_terminal_yields() -> None:
    async def scenario() -> None:
        graph = _FakeGraph(
            [("messages", (AIMessageChunk(content="hi"), {}))],
            close_error=RuntimeError("close boom"),
        )
        runner = AgentRunner(graph)
        events: list = []
        with pytest.raises(RuntimeError, match="close boom"):
            async for ev in runner.stream(session_id="ses_x", user_input="hi"):
                events.append(ev)
        assert graph.state["close_attempted"] is True
        assert not any(isinstance(e, RunnerCompleted) for e in events)
        assert "model.completed" not in _ev_types(events)

    _run(scenario())


def test_graph_exception_wins_over_cleanup_failure() -> None:
    async def scenario() -> None:
        graph = _FakeGraph(
            [
                ("messages", (AIMessageChunk(content="x"), {})),
                ValueError("graph boom"),
            ],
            close_error=RuntimeError("close boom"),
        )
        runner = AgentRunner(graph)
        events: list = []
        with pytest.raises(ValueError, match="graph boom"):
            async for ev in runner.stream(session_id="ses_x", user_input="hi"):
                events.append(ev)
        assert graph.state["close_attempted"] is True
        assert not any(isinstance(e, RunnerCompleted) for e in events)
        assert "model.completed" not in _ev_types(events)

    _run(scenario())


def test_cancellation_wins_over_cleanup_failure() -> None:
    async def scenario() -> None:
        graph = _FakeGraph(
            [("messages", (AIMessageChunk(content="x"), {}))],
            close_error=RuntimeError("close boom"),
        )
        runner = AgentRunner(graph)
        started = asyncio.Event()

        async def consume() -> None:
            async with aclosing(runner.stream(session_id="ses_x", user_input="hi")) as gen:
                async for _ev in gen:
                    started.set()
                    await asyncio.sleep(3600)

        task = asyncio.create_task(consume())
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert graph.state["close_attempted"] is True

    _run(scenario())


# --------------------------------------------------------------------------
# unknown / malformed stream elements
# --------------------------------------------------------------------------

def test_unknown_modes_and_shapes_are_ignored() -> None:
    async def scenario() -> None:
        graph = _FakeGraph([
            ("weird_mode", "something"),
            ("messages", ("not-a-message", {})),
            ("updates", "not-a-dict"),
            ("updates", {"node": "not-a-dict"}),
            ("updates", {"node": {"foo": "bar"}}),
        ])
        runner = AgentRunner(graph)
        events = await _drain(runner, session_id="ses_x", user_input="hi")
        assert events[-1].final_response == ""
        assert graph.state["closed"] is True

    _run(scenario())


# --------------------------------------------------------------------------
# factory seam
# --------------------------------------------------------------------------

def test_factory_uses_read_only_tools_and_same_checkpointer(monkeypatch) -> None:
    import eee_agent.runtime.agent_runner as ar_module

    captured: dict = {}
    sentinel = object()

    def fake_build_agent(*, tools=None, checkpointer=None, context_schema=None):
        captured["tools"] = list(tools) if tools is not None else None
        captured["checkpointer"] = checkpointer
        captured["context_schema"] = context_schema
        return sentinel

    monkeypatch.setattr(ar_module, "build_agent", fake_build_agent)
    saver = object()
    runner = build_agent_runner(saver)
    assert isinstance(runner, AgentRunner)
    assert runner._graph is sentinel  # noqa: SLF001
    assert captured["checkpointer"] is saver
    assert {t.name for t in captured["tools"]} == EXPECTED_READ_ONLY
    assert captured["context_schema"].__name__ == "RuntimeToolContext"


def test_factory_opt_in_modeling_adds_only_proposal_tool(monkeypatch) -> None:
    import eee_agent.runtime.agent_runner as ar_module

    captured: dict = {}
    sentinel = object()

    def fake_build_agent(**kwargs):
        captured.update(kwargs)
        return sentinel

    monkeypatch.setattr(ar_module, "build_agent", fake_build_agent)
    runner = build_agent_runner(object(), modeling=True)
    assert runner._graph is sentinel  # noqa: SLF001
    assert {tool.name for tool in captured["tools"]} == {
        *EXPECTED_READ_ONLY,
        "propose_modeling",
    }
    assert captured["context_schema"].__name__ == "RuntimeToolContext"


def test_agent_runner_builds_only_secure_tools(monkeypatch) -> None:
    import eee_agent.runtime.agent_runner as ar_module

    captured: dict = {}
    monkeypatch.setattr(
        ar_module,
        "build_agent",
        lambda **kwargs: captured.update(kwargs) or object(),
    )
    build_agent_runner(object(), modeling=True)
    names = {item.name for item in captured["tools"]}
    assert names == {*EXPECTED_READ_ONLY, "propose_modeling"}
    assert not names & {"create_node", "set_parms", "scene_reset", "save_hip"}
    assert captured["context_schema"].__name__ == "RuntimeToolContext"
