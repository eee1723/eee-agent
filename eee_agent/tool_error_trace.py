"""Mark OTel/Phoenix spans ERROR when a tool returns an error result.

Why: our tools return ``{"ok": false, "error": "..."}`` instead of raising, so the
OpenInference instrumentation records every tool span as OK — Phoenix shows
``errorCount = 0`` even when cooks/parms actually failed (a real set_parms TypeError
was invisible in the chair-build trace). This middleware closes that gap.

Best-effort and fully guarded: it marks whatever span is recording during the tool
call (the OpenInference tool span when one is active). It never raises and is a
no-op when tracing is off (EEE_TRACING != phoenix) or no span is recording, so it
cannot destabilize the tool path. (Note: errors embedded *inside* an ok:true result
— e.g. ``{"ok": true, "set": {"ty": "error: ..."}}`` — are a per-tool bug, not
caught here; those tools should return ok:false.)
"""
from __future__ import annotations

import json
import os
from typing import Any, Callable

from typing_extensions import override

from langchain.agents.middleware.types import (
    AgentMiddleware,
    AgentState,
    ContextT,
    ResponseT,
)
from langchain_core.messages import ToolMessage


def _tracing_on() -> bool:
    return os.getenv("EEE_TRACING", "").strip().lower() == "phoenix"


def _is_error_result(result: Any) -> bool:
    """Our tools signal failure with {"ok": false, ...} as the ToolMessage content."""
    content = getattr(result, "content", None)
    if not isinstance(content, str) or not content:
        return False
    low = content.lower()
    return ('"ok": false' in low) or ('"ok":false' in low)


def _extract_error(result: Any) -> str:
    content = getattr(result, "content", "") or ""
    try:
        data = json.loads(content)
        if isinstance(data, dict) and data.get("ok") is False:
            return str(data.get("error", ""))[:200]
    except Exception:
        pass
    return ""


class ToolErrorTraceMiddleware(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    """After each tool call, if the result is an error, flag the active span."""

    @staticmethod
    def _mark(result: Any) -> Any:
        if not _tracing_on() or not _is_error_result(result):
            return result
        try:
            from opentelemetry import trace
            from opentelemetry.trace import Status, StatusCode

            span = trace.get_current_span()
            if span is not None and span.is_recording():
                span.set_attribute("eee.tool_error", True)
                msg = _extract_error(result)
                if msg:
                    span.set_attribute("eee.tool_error_msg", msg)
                span.set_status(Status(StatusCode.ERROR, "tool returned ok:false"))
        except Exception:
            pass  # observability must never break the tool path
        return result

    @override
    def wrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> Any:
        result = handler(request)
        self._mark(result)
        return result

    @override
    async def awrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> Any:
        result = await handler(request)
        self._mark(result)
        return result
