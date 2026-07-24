"""Secure Runtime read-only tools.

Tools in this module accept only model-safe values and call an injected
``ReadOnlyProvider``.  They never import or expose the legacy bridge/client.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Mapping
from typing import Any

from langchain.tools import ToolRuntime, tool

from eee_agent.runtime.agent_context import PlainData, RuntimeToolContext

_log = logging.getLogger("eee_agent.runtime.agent_tools")

_MAX_TOOL_RESULT_BYTES = 16 * 1024
_MAX_DEPTH = 8
_MAX_ITEMS = 64
_MAX_INT_DIGITS = 128
_MAX_INT_BITS = 426


def _error(code: str, message: str) -> dict[str, object]:
    return {"ok": False, "code": code, "message": message}


def _bounded_int(value: int) -> bool:
    """Accept only integers whose decimal form is safe to serialize."""
    if value.bit_length() > _MAX_INT_BITS:
        return False
    try:
        return len(str(abs(value))) <= _MAX_INT_DIGITS
    except (OverflowError, ValueError):
        return False


def _bounded(value: Any, *, depth: int = 0) -> PlainData:
    """Copy provider output into a bounded plain JSON-like value."""
    if depth > _MAX_DEPTH:
        return "[truncated]"
    if value is None or type(value) is bool:
        return value
    if type(value) is int:
        return value if _bounded_int(value) else "[truncated]"
    if type(value) is float:
        return value if math.isfinite(value) else "[truncated]"
    if type(value) is str:
        return value if len(value) <= 2048 else "[truncated]"
    if isinstance(value, Mapping):
        out: dict[str, PlainData] = {}
        try:
            items = value.items()
            for index, (key, item) in enumerate(items):
                if index >= _MAX_ITEMS:
                    break
                if not isinstance(key, str):
                    continue
                out[key[:128]] = _bounded(item, depth=depth + 1)
        except Exception:
            return "[truncated]"
        return out
    if isinstance(value, (list, tuple)):
        return [_bounded(item, depth=depth + 1) for item in value[:_MAX_ITEMS]]
    return "[unsupported]"


def _plain_value(
    value: Any,
    *,
    depth: int = 0,
    budget: dict[str, int] | None = None,
) -> bool:
    """Return whether provider output is plain, bounded, and finite.

    Validation itself must not recursively walk an attacker-controlled Mapping
    forever.  Keep independent item/byte budgets and fail closed on every
    custom iterator/accessor exception.
    """
    if budget is None:
        budget = {"items": _MAX_ITEMS, "bytes": _MAX_TOOL_RESULT_BYTES}
    if depth > _MAX_DEPTH:
        return True
    if budget["items"] <= 0:
        return False
    budget["items"] -= 1
    if value is None or type(value) is bool:
        return True
    if type(value) is int:
        if not _bounded_int(value):
            return False
        budget["bytes"] -= len(str(value).encode("ascii"))
        return budget["bytes"] >= 0
    if type(value) is float:
        if not math.isfinite(value):
            return False
        budget["bytes"] -= len(repr(value).encode("ascii"))
        return budget["bytes"] >= 0
    if type(value) is str:
        budget["bytes"] -= min(len(value), 2048)
        return budget["bytes"] >= 0
    if isinstance(value, Mapping):
        try:
            for index, (key, item) in enumerate(value.items()):
                if index >= _MAX_ITEMS:
                    return False
                if not isinstance(key, str):
                    return False
                if not _plain_value(item, depth=depth + 1, budget=budget):
                    return False
            return True
        except Exception:
            return False
    if isinstance(value, (list, tuple)):
        try:
            if len(value) > _MAX_ITEMS:
                return False
            return all(
                _plain_value(item, depth=depth + 1, budget=budget)
                for item in value
            )
        except Exception:
            return False
    return False


def _finish(value: Any) -> dict[str, object]:
    try:
        if not isinstance(value, Mapping):
            return _error(
                "bridge.unavailable",
                "The read-only provider returned no bounded result.",
            )
        if type(value.get("ok")) is not bool:
            return _error(
                "bridge.unavailable",
                "The read-only provider returned a malformed status.",
            )
        if not _plain_value(value):
            return _error(
                "bridge.unavailable",
                "The read-only provider returned unsupported data.",
            )
        result = _bounded(value)
        if not isinstance(result, dict):
            return _error(
                "bridge.unavailable",
                "The read-only provider returned no bounded result.",
            )
        if type(result.get("ok")) is not bool:
            return _error(
                "bridge.unavailable",
                "The read-only provider returned a malformed status.",
            )
    except Exception:
        return _error(
            "bridge.unavailable",
            "The read-only provider returned no bounded result.",
        )
    # Avoid carrying opaque objects and cap serialized-size by progressively
    # replacing large values with a deterministic marker.
    def serialized_bytes(candidate: dict[str, PlainData]) -> int | None:
        try:
            return len(
                json.dumps(
                    candidate,
                    ensure_ascii=False,
                    allow_nan=False,
                    separators=(",", ":"),
                ).encode("utf-8")
            )
        except (TypeError, ValueError, UnicodeEncodeError):
            return None

    result_bytes = serialized_bytes(result)
    if result_bytes is None:
        return _error(
            "bridge.unavailable",
            "The read-only provider returned unsupported data.",
        )
    while result_bytes > _MAX_TOOL_RESULT_BYTES:
        largest = max(
            ((key, repr(item)) for key, item in result.items()),
            key=lambda pair: len(pair[1]),
            default=None,
        )
        if largest is None or largest[1] == "'[truncated]'":
            break
        result[largest[0]] = "[truncated]"
        result_bytes = serialized_bytes(result)
        if result_bytes is None:
            return _error(
                "bridge.unavailable",
                "The read-only provider returned unsupported data.",
            )
    return result


def _context(runtime: ToolRuntime) -> RuntimeToolContext | None:
    context = getattr(runtime, "context", None)
    return context if type(context) is RuntimeToolContext else None


async def _call(runtime: ToolRuntime, method: str, *args: object) -> dict[str, object]:
    context = _context(runtime)
    if context is None:
        return _error(
            "bridge.unavailable",
            "A trusted read-only provider is unavailable.",
        )
    try:
        provider_method = getattr(context.read_only, method)
        result = await provider_method(*args)
    except Exception as exc:
        # M1: surface a specific code + the real cause instead of collapsing
        # every provider failure to the opaque bridge.unavailable. The model
        # needs to distinguish "retry a transient timeout" from "a programmer
        # error / bad data" to recover; logs get the structured code too.
        return _provider_error(exc)
    return _finish(result)


def _provider_error(exc: BaseException) -> dict[str, object]:
    """Classify a provider exception into a bounded error code + safe message.

    M1: previously every provider failure collapsed to the single opaque
    ``bridge.unavailable``. Now the code distinguishes timeout / unreachable /
    structured AgentException / other, so the model can decide whether to retry
    and the log carries the structured code. The model-facing message stays
    generic (the original exception text may contain sensitive detail and is
    NOT returned to the model); the real cause is logged via _log.exception."""
    import asyncio

    from eee_agent.core import AgentException

    # Record the real cause for operators regardless of classification.
    _log.exception("read-only provider call failed")

    if isinstance(exc, AgentException) and exc.error is not None:
        # A structured error already carries a model-safe message_for_user.
        err = exc.error
        return {"ok": False, "code": err.code, "message": err.message_for_user}
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return _error(
            "bridge.provider_timeout",
            "The trusted read-only provider timed out. This is usually "
            "transient — retry the call once.",
        )
    if _is_connection_error(exc):
        return _error(
            "bridge.provider_unreachable",
            "Could not reach the trusted read-only provider (connection "
            "error). This is usually transient — retry the call once.",
        )
    return _error(
        "bridge.provider_error",
        "The trusted read-only provider returned an error. Inspect the "
        "runtime log for detail; adjust the request if it was malformed.",
    )


def _is_connection_error(exc: BaseException) -> bool:
    """True for socket/connection-level failures (refused, reset, broken pipe)."""
    name = type(exc).__name__
    if name in {
        "ConnectionError",
        "ConnectionRefusedError",
        "ConnectionResetError",
        "ConnectionAbortedError",
        "BrokenPipeError",
    }:
        return True
    # OSError subtypes (e.g. socket.gaierror) carry connection-like text.
    if isinstance(exc, OSError):
        return True
    return False


def _valid_text(value: object, *, max_len: int = 512) -> str | None:
    if type(value) is not str or not value or len(value) > max_len:
        return None
    return value


@tool
async def scene_status(runtime: ToolRuntime) -> dict[str, object]:
    """Return bounded scene/bridge status without scene writes."""
    return await _call(runtime, "scene_status")


@tool
async def query_scene(
    node_paths: list[str],
    runtime: ToolRuntime,
) -> dict[str, object]:
    """Query scene nodes through the injected read-only provider."""
    if (
        type(node_paths) is not list
        or not node_paths
        or len(node_paths) > 64
        or any(
            _valid_text(path) is None or not path.startswith("/")
            for path in node_paths
        )
    ):
        return _error("runtime.tool_input_invalid", "node_paths is invalid.")
    return await _call(runtime, "query_scene", list(node_paths))


@tool
async def inspect_workspace(
    workspace_id: str,
    runtime: ToolRuntime,
) -> dict[str, object]:
    """Return bounded trusted workspace inspection data."""
    workspace_value = _valid_text(workspace_id, max_len=128)
    if workspace_value is None:
        return _error("runtime.tool_input_invalid", "workspace_id is invalid.")
    return await _call(runtime, "inspect_workspace", workspace_value)


@tool
async def geometry_stats(
    node_path: str,
    runtime: ToolRuntime,
) -> dict[str, object]:
    """Return bounded geometry statistics through the read-only provider."""
    node_value = _valid_text(node_path)
    if node_value is None or not node_value.startswith("/"):
        return _error("runtime.tool_input_invalid", "node_path is invalid.")
    return await _call(runtime, "geometry_stats", node_value)


@tool
async def work_status(
    workspace_id: str,
    runtime: ToolRuntime,
) -> dict[str, object]:
    """Return bounded procedural work status without mutating the scene."""
    workspace_value = _valid_text(workspace_id, max_len=128)
    if workspace_value is None:
        return _error("runtime.tool_input_invalid", "workspace_id is invalid.")
    return await _call(runtime, "work_status", workspace_value)


@tool
async def search_houdini_knowledge(
    query: str,
    runtime: ToolRuntime,
    limit: int = 5,
) -> dict[str, object]:
    """Search the bounded, read-only Houdini Knowledge Graph cache."""
    query_value = _valid_text(query, max_len=512)
    if query_value is None or type(limit) is not int or not 1 <= limit <= 10:
        return _error("runtime.tool_input_invalid", "knowledge search input is invalid.")
    # Knowledge results come from a trusted local cache (not the live Houdini
    # process), so they bypass the bridge plain-value/budget validator that the
    # scene tools need. KnowledgeRuntime.search already returns bounded plain
    # JSON-serializable data and its own ``kb_unavailable`` status when the
    # cache is missing or corrupt.
    context = _context(runtime)
    if context is None or context.knowledge is None:
        return _error(
            "bridge.unavailable",
            "A trusted knowledge provider is unavailable.",
        )
    try:
        return dict(context.knowledge.search(query_value, limit=limit))
    except Exception:
        return _error(
            "bridge.unavailable",
            "The trusted knowledge provider is unavailable.",
        )


@tool
async def get_houdini_knowledge(
    entity_id: str,
    runtime: ToolRuntime,
    max_body_bytes: int = 4_000,
) -> dict[str, object]:
    """Read one bounded Knowledge Graph entity by logical entity id."""
    entity_value = _valid_text(entity_id, max_len=256)
    if (
        entity_value is None
        or type(max_body_bytes) is not int
        or not 1 <= max_body_bytes <= 8_000
    ):
        return _error("runtime.tool_input_invalid", "knowledge get input is invalid.")
    context = _context(runtime)
    if context is None or context.knowledge is None:
        return _error(
            "bridge.unavailable",
            "A trusted knowledge provider is unavailable.",
        )
    try:
        return dict(
            context.knowledge.get(entity_value, max_body_bytes=max_body_bytes)
        )
    except Exception:
        return _error(
            "bridge.unavailable",
            "The trusted knowledge provider is unavailable.",
        )


def build_read_only_tools() -> list[Any]:
    """Return a fresh secure Runtime read-only tool allowlist."""
    return [
        scene_status,
        query_scene,
        inspect_workspace,
        geometry_stats,
        work_status,
        search_houdini_knowledge,
        get_houdini_knowledge,
    ]


__all__ = [
    "build_read_only_tools",
    "geometry_stats",
    "inspect_workspace",
    "query_scene",
    "scene_status",
    "work_status",
    "search_houdini_knowledge",
    "get_houdini_knowledge",
]
