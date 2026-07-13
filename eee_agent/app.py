"""Assemble the Deep Agents Houdini agent.

Foundation keeps the existing tool surface for compatibility but explicitly
disables Deep Agents' auto-added general-purpose subagent. Capability-specific
subagents will be registered later with bounded tools and structured outputs.
"""
from __future__ import annotations

import os

from deepagents import create_deep_agent
from langgraph.graph.state import CompiledStateGraph

from eee_agent.harness import configure_deepagents_harness
from eee_agent.model import build_model
from eee_agent.system_prompt import build_system_prompt
from eee_agent.tools.registry import all_tools


def build_agent() -> CompiledStateGraph:
    # Instrument LangChain/LangGraph for Phoenix tracing if EEE_TRACING=phoenix.
    from eee_agent.tracing import setup_tracing
    setup_tracing()
    model = build_model()
    middleware = []
    try:
        from eee_agent import context_trim
        if context_trim.is_enabled():
            middleware.append(context_trim.TrimReadbacksMiddleware())
            print("[eee] read-back trimming middleware enabled", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[eee] read-back trimming disabled: {e}", flush=True)
    try:
        from eee_agent import loop_guard
        if loop_guard.is_enabled():
            middleware.append(loop_guard.LoopGuardMiddleware())
            print("[eee] loop guard middleware enabled", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[eee] loop guard disabled: {e}", flush=True)
    try:
        from eee_agent import tool_error_trace
        middleware.append(tool_error_trace.ToolErrorTraceMiddleware())
        print("[eee] tool-error tracing middleware enabled", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[eee] tool-error tracing disabled: {e}", flush=True)
    try:
        from eee_agent import context_store
        if context_store.is_enabled():
            middleware.append(context_store.build_middleware(model))
            print("[eee] ContextSeek memory enabled (scope="
                  f"{context_store.SCOPE})", flush=True)
    except Exception as e:  # noqa: BLE001 — memory is optional, never block the agent
        print(f"[eee] ContextSeek disabled: {e}", flush=True)
    try:
        from eee_agent import workflow_middleware
        if workflow_middleware.is_enabled():
            middleware.append(workflow_middleware.WorkflowStatusMiddleware())
            print("[eee] workflow status middleware enabled", flush=True)
    except Exception as e:  # noqa: BLE001
        print(f"[eee] workflow status middleware disabled: {e}", flush=True)
    # On-demand compaction: a `compact_conversation` tool so the agent can shed
    # history between components/tasks. Shares a StateBackend with the agent. The
    # built-in auto-summarization's 85%-of-window trigger is ineffective for us
    # (DeepSeek has no max_input_tokens profile -> deepagents falls back to 170k,
    # never hit at our scale), so this on-demand tool + read-back trimming carry
    # the load. Opt out with EEE_COMPACT_TOOL=false.
    backend = None
    if os.getenv("EEE_COMPACT_TOOL", "true").strip().lower() == "true":
        try:
            from deepagents.backends import StateBackend
            from deepagents.middleware import create_summarization_tool_middleware
            backend = StateBackend()
            middleware.append(create_summarization_tool_middleware(model, backend))
            print("[eee] compact_conversation tool enabled", flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[eee] compact_conversation tool disabled: {e}", flush=True)
            backend = None
    kwargs = dict(model=model, tools=all_tools(),
                  system_prompt=build_system_prompt(), middleware=middleware)
    if backend is not None:
        kwargs["backend"] = backend
    configure_deepagents_harness()
    return create_deep_agent(**kwargs)
