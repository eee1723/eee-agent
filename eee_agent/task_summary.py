"""Inject a compact per-run task graph summary before each async model call."""

from __future__ import annotations

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

from eee_agent.runtime.agent_context import RuntimeToolContext
from eee_agent.runtime.task_graph import TaskGraphToolContext


def is_enabled() -> bool:
    return os.getenv("EEE_TASK_SUMMARY", "true").strip().lower() != "false"


def _append_system(request: ModelRequest[ContextT], summary: str) -> ModelRequest[ContextT]:
    message = request.system_message
    if message is None:
        return request.override(system_message=SystemMessage(content=summary))
    content = message.content
    if isinstance(content, str):
        content = content + "\n\n" + summary
    else:
        content = [*content, {"type": "text", "text": summary}]
    return request.override(system_message=SystemMessage(content=content))


class TaskSummaryMiddleware(
    AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]
):
    """Append current task state without allowing persistence failures to break calls."""

    @staticmethod
    def _task_graph(request: ModelRequest[ContextT]) -> TaskGraphToolContext | None:
        runtime = getattr(request, "runtime", None)
        context = getattr(runtime, "context", None)
        if type(context) is not RuntimeToolContext:
            return None
        task_graph = getattr(context, "task_graph", None)
        return task_graph if type(task_graph) is TaskGraphToolContext else None

    async def _inject(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        task_graph = self._task_graph(request)
        if task_graph is None:
            return request
        try:
            summary = await task_graph.store.render_summary(task_graph.run_id)
        except Exception:  # noqa: BLE001
            return request
        return _append_system(request, summary) if summary else request

    @override
    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        return handler(request)

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Any],
    ) -> ModelResponse[ResponseT]:
        return await handler(await self._inject(request))
