"""Deterministic HTML-sketch gate for procedural/parametric modeling runs.

The project skills describe the design-first HTML route, but skills live in
the Knowledge Graph and are not automatically inserted into every model call.
A model can therefore jump directly from a broad asset request to
``scratch_build``.  This middleware makes the critical boundary executable:

* a procedural/parametric request must successfully call ``render_sketch``;
* a later Human message must explicitly approve that rendered sketch; and
* only then may ``scratch_build`` or ``scratch_commit`` execute.

The decision is reconstructed solely from checkpointed messages, so it is
restart-safe and keeps no process-local workflow state.
"""
from __future__ import annotations

import json
import os
from collections.abc import Callable, Sequence
from typing import Any

from langchain.agents.middleware.types import AgentMiddleware, AgentState, ContextT, ResponseT
from langchain_core.messages import HumanMessage, ToolMessage
from typing_extensions import override


_GUARDED_TOOLS = frozenset({"scratch_build", "scratch_commit"})
_WORKFLOW_MARKERS = (
    "程序化",
    "参数化",
    "procedural",
    "parametric",
)
_APPROVAL_MARKERS = (
    "批准",
    "通过",
    "按这个",
    "可以构建",
    "开始构建",
    "继续构建",
    "草图可以",
    "sketch approved",
    "approve",
)
_SHORT_APPROVALS = frozenset(
    {"可以", "继续", "可以继续", "好的", "好", "ok", "okay"}
)


def is_enabled() -> bool:
    return os.getenv("EEE_HTML_WORKFLOW_GUARD", "true").strip().lower() != "false"


def _message_text(message: Any) -> str:
    content = getattr(message, "content", "")
    if isinstance(content, str):
        return content
    if isinstance(content, Sequence):
        parts: list[str] = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and isinstance(item.get("text"), str):
                parts.append(item["text"])
        return " ".join(parts)
    return ""


def _render_succeeded(message: Any) -> bool:
    if not isinstance(message, ToolMessage) or message.name != "render_sketch":
        return False
    content = _message_text(message)
    try:
        payload = json.loads(content)
    except (TypeError, json.JSONDecodeError):
        return False
    return isinstance(payload, dict) and payload.get("ok") is True


def _workflow_state(messages: Sequence[Any]) -> tuple[bool, bool, bool]:
    """Return ``(required, rendered, approved_after_render)``."""
    trigger_index: int | None = None
    for index, message in enumerate(messages):
        if not isinstance(message, HumanMessage):
            continue
        text = _message_text(message).lower()
        if any(marker in text for marker in _WORKFLOW_MARKERS):
            trigger_index = index
            break
    if trigger_index is None:
        return False, False, False

    render_index: int | None = None
    for index in range(trigger_index + 1, len(messages)):
        if _render_succeeded(messages[index]):
            render_index = index
    if render_index is None:
        return True, False, False

    approved = False
    for message in messages[render_index + 1 :]:
        if not isinstance(message, HumanMessage):
            continue
        text = _message_text(message).strip().lower()
        if text in _SHORT_APPROVALS or any(
            marker in text for marker in _APPROVAL_MARKERS
        ):
            approved = True
            break
    return True, True, approved


def _tool_name(request: Any) -> str:
    call = getattr(request, "tool_call", None)
    if isinstance(call, dict):
        return str(call.get("name") or "")
    return str(getattr(call, "name", "") or "")


def _tool_call_id(request: Any) -> str:
    call = getattr(request, "tool_call", None)
    if isinstance(call, dict):
        return str(call.get("id") or "")
    return str(getattr(call, "id", "") or "")


def _guard_error(request: Any, code: str, message: str) -> ToolMessage:
    return ToolMessage(
        content=json.dumps(
            {"ok": False, "code": code, "message": message},
            ensure_ascii=False,
            separators=(",", ":"),
        ),
        name=_tool_name(request),
        tool_call_id=_tool_call_id(request),
    )


def _blocked_result(request: Any) -> ToolMessage | None:
    name = _tool_name(request)
    if name not in _GUARDED_TOOLS:
        return None
    state = getattr(request, "state", None)
    messages = state.get("messages", ()) if isinstance(state, dict) else ()
    required, rendered, approved = _workflow_state(messages)
    if not required:
        return None
    if not rendered:
        return _guard_error(
            request,
            "workflow.html_sketch_required",
            "程序化/参数化资产必须先调用 render_sketch 生成 HTML 草图，"
            "向用户报告 HTML/PNG 路径并停止等待审核；当前禁止写入 Houdini 沙箱。",
        )
    if not approved:
        return _guard_error(
            request,
            "workflow.sketch_approval_required",
            "HTML 草图已经生成，但尚无草图之后的用户批准消息。"
            "请停止并等待用户明确批准后再调用 Houdini 写工具。",
        )
    return None


class HtmlWorkflowGuardMiddleware(
    AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]
):
    """Block Houdini writes that bypass the HTML sketch and approval gates."""

    @override
    def wrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> Any:
        blocked = _blocked_result(request) if is_enabled() else None
        return blocked if blocked is not None else handler(request)

    @override
    async def awrap_tool_call(
        self,
        request: Any,
        handler: Callable[[Any], Any],
    ) -> Any:
        blocked = _blocked_result(request) if is_enabled() else None
        return blocked if blocked is not None else await handler(request)


__all__ = [
    "HtmlWorkflowGuardMiddleware",
    "_blocked_result",
    "_workflow_state",
    "is_enabled",
]
