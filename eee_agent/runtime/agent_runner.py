from __future__ import annotations

import sys
from collections.abc import AsyncIterator, Awaitable, Callable
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langgraph.checkpoint.base import BaseCheckpointSaver

from eee_agent.app import build_agent
from eee_agent.config import recursion_limit
from eee_agent.core.events import JsonValue
from eee_agent.providers.events import (
    ReasoningDelta,
    TextDelta,
    ToolCallArgumentsDelta,
    ToolCallStarted,
    UsageUpdated,
)
from eee_agent.providers.normalize import normalize_message_chunk
from eee_agent.runtime.models import RetentionClass, TOOL_RESULT_PREVIEW_CHARS
from eee_agent.runtime.agent_context import RuntimeToolContext
from eee_agent.runtime.agent_tools import build_read_only_tools




@runtime_checkable
class _StreamingGraph(Protocol):
    """The graph stream surface consumed by :class:`AgentRunner`."""

    def astream(
        self,
        input: dict[str, list[dict[str, str]]],
        *,
        config: dict[str, object],
        stream_mode: list[str],
        context: object | None = None,
    ) -> AsyncIterator[tuple[str, object]]: ...


@dataclass(frozen=True, slots=True)
class RunnerEvent:
    event_type: str
    payload: dict[str, JsonValue]
    retention_class: RetentionClass


@dataclass(frozen=True, slots=True)
class RunnerCompleted:
    final_response: str
    usage: dict[str, int]


def _extract_ai_text(message: AIMessage) -> str:
    """Return only the assistant text from an AIMessage (no reasoning/tool blocks)."""
    content = message.content
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, str):
                parts.append(block)
            elif isinstance(block, MappingABC) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def _tool_completed_event(message: ToolMessage) -> RunnerEvent:
    call_id = getattr(message, "tool_call_id", "") or ""
    name = getattr(message, "name", None) or "tool"
    raw = message.content
    content = raw if isinstance(raw, str) else str(raw)
    if len(content) > TOOL_RESULT_PREVIEW_CHARS:
        preview = content[:TOOL_RESULT_PREVIEW_CHARS]
        truncated = True
    else:
        preview = content
        truncated = False
    return RunnerEvent(
        event_type="tool.completed",
        payload={
            "call_id": call_id,
            "name": name,
            "content": preview,
            "truncated": truncated,
        },
        retention_class=RetentionClass.OPERATIONAL,
    )


# D-1: maximum number of todo items we are willing to forward to the UI.
# deepagents does not bound TodoListMiddleware; a misbehaving model could
# otherwise flood the event stream. 64 is generous for any realistic plan.
_MAX_TODOS = 64
_MAX_TODO_CONTENT_CHARS = 512
_VALID_TODO_STATUSES = frozenset({"pending", "in_progress", "completed"})


def _normalize_todos(value: object) -> list[JsonValue] | None:
    """Validate and bound a deepagents todos list.

    Accepts the list shape emitted by ``TodoListMiddleware`` via
    ``stream_mode='updates'``: ``[{content: str, status: str}, ...]``.
    Returns a clean list of dicts (or None when the value is missing/invalid)
    so the caller can skip emitting the event entirely on no-op updates.
    """
    if not isinstance(value, list):
        return None
    if not value:
        # An empty list is a valid 'cleared todos' signal; forward it.
        return []
    cleaned: list[JsonValue] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        status = item.get("status")
        if not isinstance(content, str) or not content:
            continue
        if not isinstance(status, str) or status not in _VALID_TODO_STATUSES:
            continue
        cleaned.append({
            "content": content[:_MAX_TODO_CONTENT_CHARS],
            "status": status,
        })
        if len(cleaned) >= _MAX_TODOS:
            break
    return cleaned


class AgentRunner:
    """Provider-neutral streaming adapter over a compiled Deep Agents graph.

    Owns no database, no WebSocket, no checkpoint lifecycle. It maps Foundation
    provider-neutral events (from ``normalize_message_chunk``) and ToolMessage
    results to Runtime ``RunnerEvent``s, accumulates the final response/usage,
    and emits exactly one terminal ``RunnerCompleted`` on success. Exceptions and
    cancellation propagate to the service boundary; this layer never synthesizes
    ``model.failed`` or an ``AgentError``.
    """

    def __init__(self, graph: object) -> None:
        self._graph = graph
        self._context_factory: (
            Callable[[str, str], object | Awaitable[object | None]] | None
        ) = None

    def set_context_factory(
        self,
        factory: Callable[[str, str], object | Awaitable[object | None]] | None,
    ) -> None:
        """Install a trusted per-Run context factory for opt-in capabilities."""
        if factory is not None and not callable(factory):
            raise TypeError("context factory must be callable or None")
        self._context_factory = factory

    async def stream(
        self,
        *,
        session_id: str,
        user_input: str,
        run_id: str | None = None,
    ) -> AsyncIterator[RunnerEvent | RunnerCompleted]:
        text_deltas: list[str] = []
        final_message: AIMessage | None = None
        usage: dict[str, int] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cache_read_tokens": 0,
            "cache_creation_tokens": 0,
        }

        context: object | None = None
        if self._context_factory is not None:
            if run_id is None:
                raise ValueError("run_id is required when a context factory is set")
            candidate = self._context_factory(session_id, run_id)
            context = await candidate if isinstance(candidate, Awaitable) else candidate
        config: dict[str, object] = {
            "configurable": {"thread_id": session_id},
            "recursion_limit": recursion_limit(),
        }
        graph = self._graph
        if not isinstance(graph, _StreamingGraph):
            raise TypeError("graph must expose the Runtime streaming interface")
        stream = graph.astream(
            {"messages": [{"role": "user", "content": user_input}]},
            config=config,
            stream_mode=["messages", "updates"],
            context=context,
        )
        try:
            async for mode, data in stream:
                if mode == "messages":
                    if not (isinstance(data, tuple) and len(data) == 2):
                        continue
                    chunk, _metadata = data
                    if isinstance(chunk, AIMessageChunk):
                        for event in normalize_message_chunk(chunk):
                            if isinstance(event, ReasoningDelta):
                                yield RunnerEvent(
                                    event_type="model.reasoning_delta",
                                    payload={"text": event.text},
                                    retention_class=RetentionClass.OPERATIONAL,
                                )
                            elif isinstance(event, TextDelta):
                                text_deltas.append(event.text)
                                yield RunnerEvent(
                                    event_type="model.text_delta",
                                    payload={"text": event.text},
                                    retention_class=RetentionClass.OPERATIONAL,
                                )
                            elif isinstance(event, ToolCallStarted):
                                yield RunnerEvent(
                                    event_type="tool.started",
                                    payload={
                                        "call_id": event.call_id,
                                        "name": event.name,
                                        "index": event.index,
                                    },
                                    retention_class=RetentionClass.OPERATIONAL,
                                )
                            elif isinstance(event, ToolCallArgumentsDelta):
                                yield RunnerEvent(
                                    event_type="tool.arguments_delta",
                                    payload={
                                        "call_id": event.call_id,
                                        "arguments_delta": event.arguments_delta,
                                        "index": event.index,
                                    },
                                    retention_class=RetentionClass.OPERATIONAL,
                                )
                            elif isinstance(event, UsageUpdated):
                                usage["input_tokens"] += event.input_tokens
                                usage["output_tokens"] += event.output_tokens
                                usage["total_tokens"] += event.total_tokens
                                usage["cache_read_tokens"] = (
                                    usage.get("cache_read_tokens", 0)
                                    + event.cache_read
                                )
                                usage["cache_creation_tokens"] = (
                                    usage.get("cache_creation_tokens", 0)
                                    + event.cache_creation
                                )
                                payload: dict[str, JsonValue] = {
                                    "input_tokens": event.input_tokens,
                                    "output_tokens": event.output_tokens,
                                    "total_tokens": event.total_tokens,
                                }
                                # Surface cache metrics only when the provider
                                # actually reported them (0 = not reported vs
                                # reported-as-zero is indistinguishable, but
                                # omitting keeps payloads small for providers
                                # that never populate the field).
                                if event.cache_read or event.cache_creation:
                                    payload["cache_read_tokens"] = event.cache_read
                                    payload["cache_creation_tokens"] = event.cache_creation
                                yield RunnerEvent(
                                    event_type="model.usage_updated",
                                    payload=payload,
                                    retention_class=RetentionClass.OPERATIONAL,
                                )
                    elif isinstance(chunk, ToolMessage):
                        yield _tool_completed_event(chunk)
                elif mode == "updates":
                    if not isinstance(data, dict):
                        continue
                    for _node, upd in data.items():
                        if not isinstance(upd, dict):
                            continue
                        messages = upd.get("messages")
                        if isinstance(messages, list):
                            for msg in messages:
                                if isinstance(msg, AIMessage):
                                    final_message = msg
                        # D-1: deepagents' TodoListMiddleware publishes the
                        # current todo list on the 'todos' state key whenever
                        # write_todos runs. Surface it as a bounded
                        # todos.updated event so the UI can render progress
                        # and the agent's plan is visible to the user.
                        todos_payload = _normalize_todos(upd.get("todos"))
                        if todos_payload is not None:
                            yield RunnerEvent(
                                event_type="todos.updated",
                                payload={"todos": todos_payload},
                                retention_class=RetentionClass.OPERATIONAL,
                            )
        finally:
            # Detect an in-flight exception BEFORE attempting cleanup: a cleanup
            # failure must propagate only on the normal path (so it is not hidden
            # behind spurious success terminals). If the graph already raised,
            # the consumer was cancelled, or the generator is being closed, the
            # original exception/GeneratorExit must win and a cleanup error must
            # not replace it.
            had_in_flight_error = sys.exc_info()[0] is not None
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except BaseException:
                    if not had_in_flight_error:
                        raise

        if final_message is not None:
            final_response = _extract_ai_text(final_message)
        else:
            final_response = "".join(text_deltas)

        yield RunnerEvent(
            event_type="model.completed",
            payload={"usage": dict(usage)},
            retention_class=RetentionClass.DURABLE,
        )
        yield RunnerCompleted(final_response=final_response, usage=dict(usage))


# Defined after AgentRunner so the runtime-evaluated alias can reference it.
RunnerFactory = Callable[[BaseCheckpointSaver], AgentRunner]


def build_agent_runner(
    checkpointer: BaseCheckpointSaver,
    *,
    modeling: bool = False,
) -> AgentRunner:
    """Construct a real read-only AgentRunner over a fresh compiled graph.

    The factory only builds the graph/runner; it does not open or own the
    checkpointer. Task 10's RuntimeService supplies the live checkpointer.

    ``modeling=True`` exposes the scratch sandbox tools (scratch_build +
    scratch_commit) so the agent can build iteratively in an isolated
    container and promote verified results through hard gates. The legacy
    ``propose_modeling`` tool (the blind-whole-spec-at-once approach) was
    retired in favor of the iterative sandbox workflow; its module and tests
    are retained for now but it is no longer registered on the agent graph.
    """
    if type(modeling) is not bool:
        raise TypeError("modeling must be a bool")
    tools = build_read_only_tools()
    if modeling:
        from eee_agent.modeling.scratch_coordinator import (
            cleanup_nodes,
            scratch_build,
            scratch_commit,
            task_graph_status,
        )
        from eee_agent.runtime.sketch_tools import (
            render_sketch,
            verify_geometry,
        )

        tools.append(scratch_build)
        tools.append(scratch_commit)
        tools.append(cleanup_nodes)
        tools.append(task_graph_status)
        tools.append(render_sketch)
        tools.append(verify_geometry)
    graph = build_agent(
        tools=tools,
        checkpointer=checkpointer,
        context_schema=RuntimeToolContext,
    )
    return AgentRunner(graph)
