from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from collections.abc import Mapping as MappingABC
from dataclasses import dataclass

from langchain_core.messages import AIMessage, AIMessageChunk, ToolMessage
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph

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
from eee_agent.runtime.models import RetentionClass
from eee_agent.tools.registry import read_only_tools

# Tool-result preview cap, matching the existing CLI preview behavior.
TOOL_RESULT_PREVIEW_CHARS = 600


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


class AgentRunner:
    """Provider-neutral streaming adapter over a compiled Deep Agents graph.

    Owns no database, no WebSocket, no checkpoint lifecycle. It maps Foundation
    provider-neutral events (from ``normalize_message_chunk``) and ToolMessage
    results to Runtime ``RunnerEvent``s, accumulates the final response/usage,
    and emits exactly one terminal ``RunnerCompleted`` on success. Exceptions and
    cancellation propagate to the service boundary; this layer never synthesizes
    ``model.failed`` or an ``AgentError``.
    """

    def __init__(self, graph: CompiledStateGraph) -> None:
        self._graph = graph

    async def stream(
        self,
        *,
        session_id: str,
        user_input: str,
    ) -> AsyncIterator[RunnerEvent | RunnerCompleted]:
        text_deltas: list[str] = []
        final_message: AIMessage | None = None
        usage = {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0}

        stream = self._graph.astream(
            {"messages": [{"role": "user", "content": user_input}]},
            config={
                "configurable": {"thread_id": session_id},
                "recursion_limit": recursion_limit(),
            },
            stream_mode=["messages", "updates"],
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
                                yield RunnerEvent(
                                    event_type="model.usage_updated",
                                    payload={
                                        "input_tokens": event.input_tokens,
                                        "output_tokens": event.output_tokens,
                                        "total_tokens": event.total_tokens,
                                    },
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
        finally:
            # Close the underlying stream on every exit path (normal exhaustion,
            # graph exception, cancellation, early consumer close). Cleanup is
            # best-effort and must never mask the in-flight exception.
            aclose = getattr(stream, "aclose", None)
            if aclose is not None:
                try:
                    await aclose()
                except BaseException:
                    pass

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


def build_agent_runner(checkpointer: BaseCheckpointSaver) -> AgentRunner:
    """Construct a real read-only AgentRunner over a fresh compiled graph.

    The factory only builds the graph/runner; it does not open or own the
    checkpointer. Task 10's RuntimeService supplies the live checkpointer.
    """
    return AgentRunner(
        build_agent(
            tools=read_only_tools(),
            checkpointer=checkpointer,
        )
    )
