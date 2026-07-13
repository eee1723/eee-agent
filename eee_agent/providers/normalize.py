from __future__ import annotations

import json
from collections.abc import Mapping

from langchain_core.messages import AIMessageChunk

from eee_agent.providers.events import (
    ProviderEvent,
    ReasoningDelta,
    TextDelta,
    ToolCallArgumentsDelta,
    ToolCallStarted,
    UsageUpdated,
)


def normalize_message_chunk(chunk: AIMessageChunk) -> tuple[ProviderEvent, ...]:
    events: list[ProviderEvent] = []
    additional = chunk.additional_kwargs or {}
    legacy_reasoning = additional.get("reasoning_content")
    if legacy_reasoning:
        events.append(ReasoningDelta(str(legacy_reasoning)))

    if isinstance(chunk.content, str):
        if chunk.content:
            events.append(TextDelta(chunk.content))
    else:
        for block in chunk.content:
            if not isinstance(block, Mapping):
                continue
            block_type = block.get("type")
            if block_type in {"thinking", "reasoning"}:
                text = block.get("thinking") or block.get("reasoning") or block.get("text")
                if text:
                    events.append(ReasoningDelta(str(text)))
            elif block_type == "text" and block.get("text"):
                events.append(TextDelta(str(block["text"])))

    for tool_chunk in chunk.tool_call_chunks or []:
        call_id = str(tool_chunk.get("id") or "")
        index = int(tool_chunk.get("index") or 0)
        name = str(tool_chunk.get("name") or "")
        if name:
            events.append(ToolCallStarted(call_id, name, index))
        arguments = tool_chunk.get("args")
        if isinstance(arguments, Mapping):
            arguments = json.dumps(arguments, separators=(",", ":"))
        if arguments:
            events.append(ToolCallArgumentsDelta(call_id, str(arguments), index))

    usage = chunk.usage_metadata
    if usage:
        events.append(
            UsageUpdated(
                int(usage.get("input_tokens", 0)),
                int(usage.get("output_tokens", 0)),
                int(usage.get("total_tokens", 0)),
            )
        )
    return tuple(events)
