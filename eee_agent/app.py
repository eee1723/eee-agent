"""Assemble the deepagents Houdini procedural-modeling agent.

create_deep_agent signature verified 2026-07-11 against the installed package:
  create_deep_agent(model=str|BaseChatModel, tools=..., *, system_prompt=...,
                    skills=list[str]|None, memory=list[str]|None, ...)
We pass model (object), tools, and system_prompt. skills=/memory= are available
but their loading depends on the deepagents backend path resolution; for v1 the
skill + AGENTS.md content is embedded into the system prompt (see
system_prompt.build_system_prompt) for maximum robustness. Migrate to native
skills=/memory= after empirically verifying backend path behavior.
"""
from __future__ import annotations

from deepagents import create_deep_agent
from langgraph.graph.state import CompiledStateGraph

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
    return create_deep_agent(
        model=model,
        tools=all_tools(),
        system_prompt=build_system_prompt(),
        middleware=middleware,
    )
