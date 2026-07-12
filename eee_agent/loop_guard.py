"""Deterministic loop guardrail — steers the agent out of action thrash without
relying on the model to self-police (it doesn't, per the trace: set_vex x6 +
delete_node x6 on anchors).

External/deterministic by design (the research-recommended pattern): a sliding
window over recent MUTATIVE tool-call signatures extracted from the conversation
history each model call (checkpoint-safe — no instance state needed). When the
same (tool, target) repeats past a threshold, append an escalating directive to the
system message; on further recurrence, a hard "stop now" directive.

A signature collapses a call to (tool, node, parm) so legitimate work isn't flagged:
set_parms on the SAME node but DIFFERENT parms are distinct calls; set_vex / create /
delete on the SAME target are the retry/thrash we want to catch.

Tunables (env): EEE_LOOP_GUARD=true|false, EEE_LOOP_REPEAT (soft, default 3),
EEE_LOOP_HARD (hard stop, default 5), EEE_LOOP_WINDOW (default 16).
"""
from __future__ import annotations

import os
from typing import Any, Callable, List, Optional, Tuple

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

# Only mutative/retry-prone tools are watched. Read-backs (work_status, geometry_stats)
# are handled by context_trim; calling those often is not a loop.
MUTATIVE = frozenset({
    "delete_node", "create_node", "set_vex", "set_parms", "set_expression",
    "cook_node", "connect_nodes",
})


def is_enabled() -> bool:
    return os.getenv("EEE_LOOP_GUARD", "true").strip().lower() != "false"


def _int(env: str, default: int) -> int:
    try:
        return int(os.getenv(env, str(default)))
    except ValueError:
        return default


def _sig(name, args) -> Optional[Tuple[str, str, str]]:
    """Collapse a tool call to (tool, node, parm) so retries on the same target
    are recognized while distinct legitimate calls are not."""
    if name not in MUTATIVE:
        return None
    args = args or {}
    if not isinstance(args, dict):
        return (str(name), str(args)[:60], "")
    node = (args.get("node_path") or args.get("component_path")
            or args.get("parent_path") or args.get("name") or "")
    parm = args.get("parm") or ""
    return (str(name), str(node), str(parm))


def _recent_calls(messages: List[Any], limit: int) -> List[Tuple[str, str, str]]:
    out: List[Tuple[str, str, str]] = []
    for m in messages:
        for tc in (getattr(m, "tool_calls", None) or []):
            if isinstance(tc, dict):
                nm, ar = tc.get("name"), tc.get("args")
            else:
                nm, ar = getattr(tc, "name", None), getattr(tc, "args", None)
            sg = _sig(nm, ar)
            if sg:
                out.append(sg)
    return out[-limit:]


def _append_system(request: ModelRequest[ContextT], text: str) -> ModelRequest[ContextT]:
    sm = request.system_message
    if sm is None:
        return request.override(system_message=SystemMessage(content=text))
    content = sm.content
    if isinstance(content, str):
        return request.override(system_message=SystemMessage(content=content + "\n\n" + text))
    return request.override(system_message=SystemMessage(
        content=[*content, {"type": "text", "text": text}]))


class LoopGuardMiddleware(AgentMiddleware[AgentState[ResponseT], ContextT, ResponseT]):
    """Watch for repeated mutative tool calls and escalate."""

    @staticmethod
    def _directive(calls: List[Tuple[str, str, str]]) -> Optional[str]:
        if not calls:
            return None
        last = calls[-1]
        n = sum(1 for c in calls if c == last)
        soft, hard = _int("EEE_LOOP_REPEAT", 3), _int("EEE_LOOP_HARD", 5)
        tool, node, parm = last
        what = f"{tool}({node}{(' ' + parm) if parm else ''})"
        if n >= hard:
            return (f"[LOOP GUARD] You have called {what} {n} times. You MUST stop now: "
                    "call save_hip and print your final summary. Do NOT call any more tools.")
        if n >= soft:
            return (f"[LOOP GUARD] You've called {what} {n} times and it isn't working. "
                    "STOP retrying it. Change approach (a different SOP, simplify the graph, "
                    "or re-read the actual error and fix that line) — or, if genuinely "
                    "blocked, save_hip and STOP with a clear summary.")
        return None

    def _maybe_steer(self, request: ModelRequest[ContextT]) -> ModelRequest[ContextT]:
        d = self._directive(_recent_calls(request.messages, _int("EEE_LOOP_WINDOW", 16)))
        return _append_system(request, d) if d else request

    @override
    def wrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], ModelResponse[ResponseT]],
    ) -> ModelResponse[ResponseT]:
        return handler(self._maybe_steer(request))

    @override
    async def awrap_model_call(
        self,
        request: ModelRequest[ContextT],
        handler: Callable[[ModelRequest[ContextT]], Any],
    ) -> ModelResponse[ResponseT]:
        return await handler(self._maybe_steer(request))
