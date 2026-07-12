"""Trim re-derivable read-back tool results from message history before each model
call, so context doesn't balloon on long builds.

Problem (from Phoenix traces): many small, mostly-redundant tool results accumulate
in history and are re-sent on every LLM call — the dominant token cost (1.8M tokens
for one chair; work_status alone was called 64x). deepagents' built-in offloading
only fires on a SINGLE result > 20k tokens, and summarization only at 85% of the
model window, so our pattern (many small, mostly-redundant read-backs) is not caught
by either. This middleware closes that gap.

Strategy: for each read-back tool (status/stats/introspection that the agent can
re-derive at any time), keep only the MOST RECENT result and stub older ones to a
one-line pointer. The structure is always re-derivable from Houdini, so old
read-backs are pure noise. Stubbing (not deleting) keeps the AI tool_call -> tool
response sequence valid for every provider.

Opt out with EEE_TRIM_READBACKS=false.
"""
from __future__ import annotations

import os
from typing import Any, Callable, List, Tuple

from typing_extensions import override

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)
from langchain_core.messages import ToolMessage
from langchain_core.messages import AnyMessage

# Tools whose results are re-derivable snapshots — keep only the latest, stub the rest.
READBACK_TOOLS = frozenset({
    "work_status", "geometry_stats", "anchor_graph",
    "describe_node_type", "hou_status", "validate_geometry",
})
STUB = "[older result omitted — re-call the tool to see the current state]"


def is_enabled() -> bool:
    """Default ON; set EEE_TRIM_READBACKS=false to disable."""
    return os.getenv("EEE_TRIM_READBACKS", "true").strip().lower() != "false"


def _stub(message: AnyMessage) -> AnyMessage:
    """Return a copy of a ToolMessage with its content replaced by the stub."""
    if not isinstance(message, ToolMessage):
        return message
    return ToolMessage(
        content=STUB,
        tool_call_id=getattr(message, "tool_call_id", "") or "",
        name=getattr(message, "name", None) or "",
    )


class TrimReadbacksMiddleware(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    """Before each model call, stub all-but-the-latest result for each read-back tool."""

    @staticmethod
    def _trim(messages: List[AnyMessage]) -> Tuple[List[AnyMessage], bool]:
        last_idx = {}
        for i, m in enumerate(messages):
            nm = getattr(m, "name", None)
            if isinstance(nm, str) and nm in READBACK_TOOLS:
                last_idx[nm] = i
        if not last_idx:
            return messages, False
        out = list(messages)
        changed = False
        for i, m in enumerate(out):
            nm = getattr(m, "name", None)
            if nm in READBACK_TOOLS and last_idx.get(nm) != i:
                out[i] = _stub(m)
                changed = True
        return out, changed

    @override
    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        msgs, changed = self._trim(request.messages)
        return handler(request.override(messages=msgs)) if changed else handler(request)

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Any],
    ) -> ModelResponse[ResponseT]:
        msgs, changed = self._trim(request.messages)
        if changed:
            request = request.override(messages=msgs)
        return await handler(request)
