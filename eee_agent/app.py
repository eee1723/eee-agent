"""Assemble the Deep Agents Runtime agent.

Every caller must provide an explicit, capability-scoped tool allowlist.  The
old Foundation registry is intentionally not a compatibility fallback: it
contains raw scene-write tools and must never be reachable from production
Runtime code.
"""
from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from typing import NotRequired, TypedDict

from deepagents import create_deep_agent
from deepagents.backends import StateBackend
from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.language_models import BaseChatModel
from langchain_core.tools import BaseTool
from langgraph.checkpoint.base import BaseCheckpointSaver
from langgraph.graph.state import CompiledStateGraph

from eee_agent.harness import configure_deepagents_harness
from eee_agent.model import build_model
from eee_agent.system_prompt import build_system_prompt


_log = logging.getLogger("eee_agent.app")

_Middleware = AgentMiddleware[AgentState[object], None, object]


class _DeepAgentKwargs(TypedDict):
    model: BaseChatModel
    tools: list[BaseTool]
    system_prompt: str
    middleware: list[_Middleware]
    backend: NotRequired[StateBackend]
    checkpointer: NotRequired[BaseCheckpointSaver]
    context_schema: NotRequired[type]


def build_agent(
    *,
    tools: Sequence[BaseTool] | None = None,
    checkpointer: BaseCheckpointSaver | None = None,
    context_schema: type | None = None,
) -> CompiledStateGraph:
    if tools is None:
        raise TypeError("build_agent requires an explicit secure tools allowlist")
    # Instrument LangChain/LangGraph for Phoenix tracing if EEE_TRACING=phoenix.
    from eee_agent.tracing import setup_tracing
    setup_tracing()
    model = build_model()
    middleware: list[_Middleware] = []
    # tool_call_guard runs first: it promotes silently-dropped invalid tool
    # calls into error replies so the model self-corrects instead of the run
    # exiting on a malformed call (2026-07-24 incident). Without it the whole
    # class of "provider streamed bad JSON args" failures end the run early.
    try:
        from eee_agent import tool_call_guard
        if tool_call_guard.is_enabled():
            middleware.append(tool_call_guard.InvalidToolCallGuardMiddleware())
            _log.info("tool-call guard middleware enabled")
    except Exception as e:  # noqa: BLE001
        _log.warning("tool-call guard disabled: %s", e, exc_info=True)
    try:
        from eee_agent import html_workflow_guard
        if html_workflow_guard.is_enabled():
            middleware.append(html_workflow_guard.HtmlWorkflowGuardMiddleware())
            _log.info("HTML workflow guard middleware enabled")
    except Exception as e:  # noqa: BLE001
        _log.warning("HTML workflow guard disabled: %s", e, exc_info=True)
    try:
        from eee_agent import context_trim
        if context_trim.is_enabled():
            middleware.append(context_trim.TrimReadbacksMiddleware())
            _log.info("read-back trimming middleware enabled")
    except Exception as e:  # noqa: BLE001
        _log.warning("read-back trimming disabled: %s", e, exc_info=True)
    try:
        from eee_agent import task_summary
        if task_summary.is_enabled():
            middleware.append(task_summary.TaskSummaryMiddleware())
            _log.info("task summary injection middleware enabled")
    except Exception as e:  # noqa: BLE001
        _log.warning("task summary injection disabled: %s", e, exc_info=True)
    try:
        from eee_agent import loop_guard
        if loop_guard.is_enabled():
            middleware.append(loop_guard.LoopGuardMiddleware())
            _log.info("loop guard middleware enabled")
    except Exception as e:  # noqa: BLE001
        _log.warning("loop guard disabled: %s", e, exc_info=True)
    try:
        from eee_agent import tool_error_trace
        middleware.append(tool_error_trace.ToolErrorTraceMiddleware())
        _log.info("tool-error tracing middleware enabled")
    except Exception as e:  # noqa: BLE001
        _log.warning("tool-error tracing disabled: %s", e, exc_info=True)
    try:
        from eee_agent import context_store
        if context_store.is_enabled():
            middleware.append(context_store.build_middleware(model))
            _log.info("ContextSeek memory enabled (scope=%s)", context_store.SCOPE)
    except Exception as e:  # noqa: BLE001 — memory is optional, never block the agent
        _log.warning("ContextSeek disabled: %s", e, exc_info=True)
    # On-demand compaction: a `compact_conversation` tool so the agent can shed
    # history between components/tasks. Shares a StateBackend with the agent. The
    # built-in auto-summarization's 85%-of-window trigger is ineffective for us
    # (DeepSeek has no max_input_tokens profile -> deepagents falls back to 170k,
    # never hit at our scale), so this on-demand tool + read-back trimming carry
    # the load. Opt out with EEE_COMPACT_TOOL=false.
    backend = None
    if os.getenv("EEE_COMPACT_TOOL", "true").strip().lower() == "true":
        try:
            from deepagents.middleware import create_summarization_tool_middleware
            backend = StateBackend()
            middleware.append(create_summarization_tool_middleware(model, backend))
            _log.info("compact_conversation tool enabled")
        except Exception as e:  # noqa: BLE001
            _log.warning("compact_conversation tool disabled: %s", e, exc_info=True)
            backend = None
    # Copy the caller sequence so later mutation cannot affect this graph.
    selected_tools = list(tools)
    kwargs = _DeepAgentKwargs(
        model=model,
        tools=selected_tools,
        system_prompt=build_system_prompt(),
        middleware=middleware,
    )
    if backend is not None:
        kwargs["backend"] = backend
    if checkpointer is not None:
        kwargs["checkpointer"] = checkpointer
    if context_schema is not None:
        kwargs["context_schema"] = context_schema
    configure_deepagents_harness()
    return create_deep_agent(**kwargs)
