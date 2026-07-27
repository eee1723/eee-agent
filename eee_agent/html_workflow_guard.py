"""Deterministic HTML-sketch gate for procedural/parametric modeling runs.

The project skills describe the design-first HTML route, but skills live in
the Knowledge Graph and are not automatically inserted into every model call.
A model can therefore jump directly from a broad asset request to
``scratch_build``.  This middleware makes the critical boundary executable:

* a procedural/parametric request must compile a ready modeling brief;
* ``render_sketch`` must carry the exact digest of that brief;
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


_GUARDED_TOOLS = frozenset({"render_sketch", "scratch_build", "scratch_commit"})
_WORKFLOW_MARKERS = (
    "程序化",
    "参数化",
    "建模",
    "模型",
    "procedural",
    "parametric",
    "modeling",
    "3d model",
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


def _tool_payload(message: Any, name: str) -> dict[str, Any] | None:
    if not isinstance(message, ToolMessage) or message.name != name:
        return None
    try:
        payload = json.loads(_message_text(message))
    except (TypeError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _ready_brief(message: Any) -> tuple[str, dict[str, Any]] | None:
    payload = _tool_payload(message, "prepare_modeling_brief")
    if (
        payload is None
        or payload.get("ok") is not True
        or payload.get("ready") is not True
    ):
        return None
    digest = payload.get("brief_digest")
    brief = payload.get("brief")
    if (
        type(digest) is not str
        or len(digest) != 64
        or not isinstance(brief, dict)
    ):
        return None
    return digest, brief


def _workflow_trigger_index(messages: Sequence[Any]) -> int | None:
    trigger_index: int | None = None
    for index, message in enumerate(messages):
        if not isinstance(message, HumanMessage):
            continue
        text = _message_text(message).lower()
        if any(marker in text for marker in _WORKFLOW_MARKERS):
            trigger_index = index
            break
    return trigger_index


def _latest_ready_brief_after_latest_human(
    messages: Sequence[Any], trigger_index: int
) -> tuple[str, dict[str, Any]] | None:
    latest_human_index = trigger_index
    latest_ready: tuple[int, str, dict[str, Any]] | None = None
    for index in range(trigger_index + 1, len(messages)):
        message = messages[index]
        if isinstance(message, HumanMessage):
            latest_human_index = index
        ready = _ready_brief(message)
        if ready is not None:
            latest_ready = (index, ready[0], ready[1])
    if latest_ready is None or latest_ready[0] <= latest_human_index:
        return None
    return latest_ready[1], latest_ready[2]


def _workflow_state(messages: Sequence[Any]) -> tuple[bool, bool, bool, bool]:
    """Return ``(required, brief_ready, rendered, approved_after_render)``."""
    trigger_index = _workflow_trigger_index(messages)
    if trigger_index is None:
        return False, False, False, False

    brief_ready = False
    latest_human_index = trigger_index
    latest_brief_index: int | None = None
    latest_brief_digest: str | None = None
    render_index: int | None = None
    for index in range(trigger_index + 1, len(messages)):
        message = messages[index]
        if isinstance(message, HumanMessage):
            latest_human_index = index
        ready = _ready_brief(message)
        if ready is not None:
            brief_ready = True
            latest_brief_index = index
            latest_brief_digest = ready[0]
        if not _render_succeeded(message):
            continue
        payload = _tool_payload(message, "render_sketch")
        render_digest = payload.get("brief_digest") if payload else None
        if (
            latest_brief_index is not None
            and latest_brief_index > latest_human_index
            and render_digest == latest_brief_digest
        ):
            render_index = index
    if render_index is None:
        return True, brief_ready, False, False

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
    return True, brief_ready, True, approved


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
    required, brief_ready, rendered, approved = _workflow_state(messages)
    if not required:
        return None
    if name == "render_sketch":
        trigger_index = _workflow_trigger_index(messages)
        ready = (
            _latest_ready_brief_after_latest_human(messages, trigger_index)
            if trigger_index is not None
            else None
        )
        if ready is None:
            return _guard_error(
                request,
                "workflow.modeling_brief_required",
                "生成程序化/参数化草图前必须先调用 prepare_modeling_brief。"
                "只有组件范围或细节等级存在实质歧义时，才可一次性提出最多三个问题；"
                "否则采用合理默认值并生成可验收的建模简报。",
            )
        call = getattr(request, "tool_call", None)
        args = call.get("args") if isinstance(call, dict) else None
        supplied_digest = args.get("brief_digest") if isinstance(args, dict) else None
        if supplied_digest != ready[0]:
            return _guard_error(
                request,
                "workflow.modeling_brief_mismatch",
                "render_sketch.brief_digest 必须与最近一次已就绪建模简报的"
                " brief_digest 完全一致。",
            )
        return None
    if not brief_ready:
        return _guard_error(
            request,
            "workflow.modeling_brief_required",
            "程序化/参数化资产必须先完成 prepare_modeling_brief，"
            "再生成与该简报摘要绑定的 HTML 草图。",
        )
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
