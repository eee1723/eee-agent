"""Workflow status middleware — injects the current work-container structure into
the system prompt every model call so the agent always knows where the build
stands (components, anchor edges, exposed params).

OFF by default (opt in with ``EEE_WORKFLOW_STATUS=true``). Appending a work_status()
snapshot to the system message EVERY turn mutates the prompt, which **breaks prompt
caching** (DeepSeek prefix cache / Anthropic cache both require a byte-stable system
prompt) and re-sends growing status text. The agent has the `work_status` tool to
re-orient on demand instead, so the default is off. Gracefully no-ops when no work
container exists yet (e.g. before ensure_work_container) or the bridge is down.
"""
from __future__ import annotations

import asyncio
import os
from typing import Any, Callable

from typing_extensions import override

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ModelRequest,
    ModelResponse,
    ResponseT,
)
from langchain_core.messages import SystemMessage


def is_enabled() -> bool:
    """Default OFF; opt in with EEE_WORKFLOW_STATUS=true. See module docstring."""
    return os.getenv("EEE_WORKFLOW_STATUS", "false").strip().lower() == "true"


def _format_status(status: dict) -> str:
    if not status.get("ok"):
        return ""
    lines = ["\n[Current Work Container Status]"]
    lines.append(f"work: {status.get('work')}")
    parms = status.get("parms", [])
    if parms:
        lines.append("params: " + ", ".join(f"{p['name']}={p.get('value')}" for p in parms))
    else:
        lines.append("params: (none exposed yet)")
    for c in status.get("components", []):
        lines.append(f"  - {c['path'].split('/')[-1]}: out_geo={'yes' if c.get('out_geo') else 'no'} "
                     f"out_anchors={'yes' if c.get('out_anchors') else 'no'} {c.get('comment','')}")
    edges = status.get("anchor_edges", [])
    if edges:
        lines.append("anchor edges: " + "; ".join(
            f"{e[0].split('/')[-1]}->{e[1].split('/')[-1]}" for e in edges))
    else:
        lines.append("anchor edges: (none yet)")
    return "\n".join(lines)


class WorkflowStatusMiddleware(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    """Appends a concise work_status() snapshot to the system message each turn."""

    @override
    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        block = self._status_block()
        if not block:
            return handler(request)
        new_sys = self._append(request.system_message, block)
        return handler(request.override(system_message=new_sys))

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Any],
    ) -> ModelResponse[ResponseT]:
        # stdio/astream path invokes the async hook; work_status() is a sync rpyc
        # call, so offload it to a thread to avoid blocking the event loop.
        block = await asyncio.to_thread(self._status_block)
        if not block:
            return await handler(request)
        new_sys = self._append(request.system_message, block)
        return await handler(request.override(system_message=new_sys))

    @staticmethod
    def _status_block() -> str:
        try:
            from eee_agent.tools.procedural import work_status
            return _format_status(work_status.invoke({}))
        except Exception:
            return ""  # no work container / bridge down -> skip silently

    @staticmethod
    def _append(system_message, block: str) -> SystemMessage:
        if system_message is None:
            return SystemMessage(content=block)
        content = system_message.content
        if isinstance(content, str):
            return SystemMessage(content=content + block)
        return SystemMessage(content=[*content, {"type": "text", "text": block}])
