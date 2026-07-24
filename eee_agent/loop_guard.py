"""Deterministic loop guardrail — steers the agent out of action thrash without
relying on the model to self-police.

External/deterministic by design (the research-recommended pattern): a sliding
window over recent MUTATIVE tool-call signatures extracted from the conversation
history each model call (checkpoint-safe — no instance state needed). When the
same (tool, target) repeats past a threshold, append an escalating directive to the
system message; on further recurrence, a hard "stop now" directive.

A signature collapses a call to (tool, target, detail) so legitimate work isn't
flagged: scratch_build calls with DIFFERENT operation lists are distinct calls
(build → observe → adjust iteration); the identical operation list repeated on
the same sandbox, or scratch_commit retried against the same target, is the
retry/thrash we want to catch.

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

# Only mutative/retry-prone tools are watched. Read-backs (scene_status,
# query_scene, geometry_stats) are handled by context_trim; calling those
# often is not a loop.
MUTATIVE = frozenset({"scratch_build", "scratch_commit"})


def is_enabled() -> bool:
    return os.getenv("EEE_LOOP_GUARD", "true").strip().lower() != "false"


def _int(env: str, default: int) -> int:
    try:
        return int(os.getenv(env, str(default)))
    except ValueError:
        return default


def _ops_signature(ops: object) -> str:
    """Collapse a scratch_build operations list to a stable, bounded signature."""
    if not isinstance(ops, list):
        return str(ops)[:60]
    parts: list[str] = []
    for op in ops:
        if isinstance(op, dict):
            parts.append(
                "{}:{}".format(op.get("kind"), op.get("node_name") or op.get("source") or "")
            )
        else:
            parts.append(str(op)[:20])
    return ",".join(parts)[:120]


def _sig(name, args) -> Optional[Tuple[str, str, str]]:
    """Collapse a tool call to (tool, target, detail) so retries on the same
    target are recognized while distinct legitimate calls are not."""
    if name not in MUTATIVE:
        return None
    args = args or {}
    if not isinstance(args, dict):
        return (str(name), str(args)[:60], "")
    if name == "scratch_build":
        return (name, _ops_signature(args.get("operations")), "")
    # scratch_commit: the target path is the retry identity.
    target = "{}/{}".format(args.get("target_parent_path") or "", args.get("target_name") or "")
    return (name, target, "")


def _iter_call_signatures(message: Any) -> List[Tuple[str, str, str]]:
    """Yield (tool, target, detail) signatures for every mutative call on a
    message — both well-formed ``tool_calls`` AND ``invalid_tool_calls``.

    M3: invalid calls (malformed-args calls LangChain demoted) were previously
    invisible here, so a model retry-storm of the SAME malformed scratch_build
    never tripped the guard. An invalid call's ``args`` is a raw string (not a
    dict); ``_sig`` already collapses that to a bounded string signature, so the
    repeated-malformed-call case now registers like any other retry."""
    out: List[Tuple[str, str, str]] = []
    for attr in ("tool_calls", "invalid_tool_calls"):
        for tc in (getattr(message, attr, None) or []):
            if isinstance(tc, dict):
                nm, ar = tc.get("name"), tc.get("args")
            else:
                nm, ar = getattr(tc, "name", None), getattr(tc, "args", None)
            sg = _sig(nm, ar)
            if sg:
                out.append(sg)
    return out


def _recent_calls(messages: List[Any], limit: int) -> List[Tuple[str, str, str]]:
    out: List[Tuple[str, str, str]] = []
    for m in messages:
        out.extend(_iter_call_signatures(m))
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
                    "print your final summary. Do NOT call any more tools.")
        if n >= soft:
            return (f"[LOOP GUARD] You've called {what} {n} times and it isn't working. "
                    "STOP retrying it. Change approach (different operations, simplify "
                    "the graph, or re-read the actual error and fix that) — or, if "
                    "genuinely blocked, STOP with a clear summary of what you tried.")
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
