"""Guard against silently-dropped invalid tool calls.

Root cause this guards (2026-07-24 incident): a provider streamed a
``scratch_build`` tool call whose accumulated ``arguments_delta`` JSON was
invalid (e.g. ``{"value":POINTS_GRID}`` — a bareword where a JSON value was
expected). LangChain's ``AIMessage.init_tool_calls`` validator silently demotes
any call whose args fail to parse into ``msg.invalid_tool_calls`` (the parse
exception is swallowed, no error is raised, no ``ToolMessage`` is synthesized).

The langchain agent routing edge then sees ``len(last_ai_message.tool_calls) ==
0`` and exits the loop to END — so the run finalized on the model's mid-task
progress text with five of seven todos still pending. The whole codebase had
zero references to ``invalid_tool_calls``; the demotion was invisible.

This middleware mirrors Pi's "validation-failed → error tool result → retry"
defense (and deepagents' own ``PatchToolCallsMiddleware`` content format), but
runs in ``after_model`` — i.e. on the SAME turn the invalid call was emitted,
before the routing edge decides to exit. ``PatchToolCallsMiddleware`` only runs
in ``before_agent`` (next turn), which never comes for a one-shot run that
would otherwise exit to Completed.

Mechanism: when the last AIMessage carries ``invalid_tool_calls`` with no
matching ``ToolMessage``, promote each invalid call to a well-formed (empty-args)
``tool_calls`` entry and append a paired error ``ToolMessage`` describing the
parse failure and the original args fragment. The routing edge then sees
non-empty ``tool_calls`` all already answered → routes back to the model node,
which receives the error and can self-correct. A per-call repair budget
(``EEE_TOOL_CALL_REPAIR_LIMIT``, default 3) stops a model that keeps emitting
the same malformed call from looping forever; once exhausted the call is left
invalid and the turn ends normally (matching the pre-fix behavior, but now
deliberately and observably).
"""
from __future__ import annotations

import logging
import os
from typing import Any

from langchain.agents.middleware import AgentMiddleware, AgentState
from langchain_core.messages import AIMessage, AnyMessage, RemoveMessage, ToolMessage
from langgraph.graph.message import REMOVE_ALL_MESSAGES
from langgraph.runtime import Runtime
from typing_extensions import override

_log = logging.getLogger("eee_agent.tool_call_guard")

# Bounded so an error ToolMessage never approaches the 256 KiB event payload cap
# (events.py:_MAX_PAYLOAD_BYTES). The original args fragment is diagnostic only.
_MAX_ARG_SNIPPET_CHARS = 2000


def is_enabled() -> bool:
    return os.getenv("EEE_TOOL_CALL_GUARD", "true").strip().lower() != "false"


def _repair_limit() -> int:
    try:
        return int(os.getenv("EEE_TOOL_CALL_REPAIR_LIMIT", "3"))
    except ValueError:
        return 3


def _last_ai_message(messages: list[AnyMessage]) -> AIMessage | None:
    for msg in reversed(messages):
        if isinstance(msg, AIMessage):
            return msg
    return None


def _snippet(text: str | None) -> str:
    if not text:
        return ""
    return text if len(text) <= _MAX_ARG_SNIPPET_CHARS else text[:_MAX_ARG_SNIPPET_CHARS] + "…"


def _error_content(name: str, call_id: str | None, error: str | None, args: str | None) -> str:
    """Build the error text fed back to the model. Tells it exactly what broke
    and shows the raw args fragment so it can fix the malformed token."""
    parts = [
        f"Tool call {name or 'unknown'} (id {call_id or 'unknown'}) could not be "
        f"executed: its arguments were malformed JSON and could not be parsed."
    ]
    if error:
        parts.append(f"Parse error: {error}")
    snippet = _snippet(args)
    if snippet:
        parts.append(f"Raw arguments received (fix the malformed token and re-issue): {snippet}")
    parts.append(
        "Re-issue the tool call with valid JSON arguments. Common causes: a bareword "
        "where a string/number is expected, an unquoted value, or a truncated argument "
        "stream. Ensure every value is a literal JSON scalar/array/object."
    )
    return "\n".join(parts)


class InvalidToolCallGuardMiddleware(AgentMiddleware[AgentState[Any], Any, Any]):
    """Promote ``invalid_tool_calls`` to answered error tool calls so the agent
    loop retries instead of exiting on a malformed call.

    See module docstring for the incident this prevents and the routing
    semantics it relies on (``after_model`` runs before the model→tools
    conditional edge, so the synthesized messages are visible to the edge's
    ``tool_calls`` / pending-call inspection).
    """

    @staticmethod
    def _maybe_patch(messages: list[AnyMessage]) -> list[AnyMessage] | None:
        """Return a rebuilt message list if a patch is needed, else None.

        A patch is needed when the last AIMessage has invalid_tool_calls whose
        ids are not yet answered by a ToolMessage AND that have not exhausted
        their per-id repair budget (tracked via a sentinel in the message list).
        Returns the full rebuilt list (RemoveMessage-all + originals + repairs),
        or None to leave state untouched.
        """
        last_ai = _last_ai_message(messages)
        if last_ai is None:
            return None
        invalid = list(getattr(last_ai, "invalid_tool_calls", None) or [])
        if not invalid:
            return None

        # Per-call repair budget: each turn this middleware synthesizes one
        # error ToolMessage per invalid call. Once a call id has been answered
        # >= repair_limit times (counted across the whole history — every prior
        # repair reply persists as a ToolMessage with that id), stop patching
        # it: the model is not converging, so let the turn end (deliberately,
        # observably) rather than loop forever.
        repair_counts: dict[str | None, int] = {}
        for m in messages:
            if isinstance(m, ToolMessage):
                cid = getattr(m, "tool_call_id", None)
                if cid is not None:
                    repair_counts[cid] = repair_counts.get(cid, 0) + 1
        limit = _repair_limit()

        to_repair = [ic for ic in invalid if repair_counts.get(ic.get("id"), 0) < limit]
        if not to_repair:
            # Every invalid call has exhausted its budget. Nothing to
            # synthesize; let the turn end normally.
            return None

        # Rebuild: clear all, re-add originals, then append (a) a rewritten
        # AIMessage carrying the repaired calls as real tool_calls, and (b) a
        # paired error ToolMessage per repaired call. The rewritten AIMessage
        # replaces the original so the routing edge sees non-empty tool_calls.
        rebuilt: list[AnyMessage] = [RemoveMessage(id=REMOVE_ALL_MESSAGES)]
        for m in messages:
            if m is last_ai:
                # Rewrite: carry over text/reasoning content + original good
                # tool_calls + the promoted invalid calls (empty-args) so the
                # edge sees them as dispatched.
                promoted = [
                    {
                        "name": ic.get("name") or "unknown",
                        "args": {},
                        "id": ic.get("id") or "",
                        "type": "tool_call",
                    }
                    for ic in to_repair
                ]
                rewritten = AIMessage(
                    content=last_ai.content,
                    tool_calls=list(last_ai.tool_calls) + promoted,
                    invalid_tool_calls=[
                        ic for ic in invalid if ic.get("id") not in {c.get("id") for c in to_repair}
                    ],
                    usage_metadata=getattr(last_ai, "usage_metadata", None),
                    response_metadata=getattr(last_ai, "response_metadata", None) or {},
                    id=getattr(last_ai, "id", None),
                )
                rebuilt.append(rewritten)
            else:
                rebuilt.append(m)
        # Paired error replies, AFTER the rewritten AIMessage, so the routing
        # edge sees every promoted tool_call as already answered → routes back
        # to the model node (edge step 6) instead of exiting (step 3).
        for ic in to_repair:
            cid = ic.get("id") or ""
            rebuilt.append(
                ToolMessage(
                    content=_error_content(
                        ic.get("name") or "unknown",
                        ic.get("id"),
                        ic.get("error"),
                        ic.get("args"),
                    ),
                    name=ic.get("name") or "unknown",
                    tool_call_id=cid,
                )
            )
        _log.warning(
            "tool_call_guard: promoted %d invalid tool call(s) to error replies "
            "for model self-correction (repair budget %d, %d at/over limit)",
            len(to_repair),
            limit,
            len(invalid) - len(to_repair),
        )
        return rebuilt

    @override
    def after_model(self, state: AgentState[Any], runtime: Runtime[Any]) -> dict[str, Any] | None:  # noqa: ARG002
        if not is_enabled():
            return None
        messages = state.get("messages") or []
        patched = self._maybe_patch(messages)
        if patched is None:
            return None
        return {"messages": patched}

    @override
    async def aafter_model(self, state: AgentState[Any], runtime: Runtime[Any]) -> dict[str, Any] | None:  # noqa: ARG002
        # Identical to the sync path; kept non-trivial so langgraph wires the
        # async after_model node (the factory keys off the overridden method).
        if not is_enabled():
            return None
        messages = state.get("messages") or []
        patched = self._maybe_patch(messages)
        if patched is None:
            return None
        return {"messages": patched}
