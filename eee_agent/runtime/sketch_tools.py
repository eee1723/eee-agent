"""Pipeline tools: sketch rendering and geometry verification.

Follows the ``agent_tools.py`` seam exactly: bounded model-facing input,
capability reached only through the injected ``RuntimeToolContext``, and
bounded plain-dict results that never raise into the graph.
"""
from __future__ import annotations

import math
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from langchain.tools import ToolRuntime, tool

from eee_agent.runtime.agent_context import RuntimeToolContext

_MAX_HTML_CHARS = 128 * 1024
_MAX_NAME_CHARS = 64
_MAX_NODE_PATH_CHARS = 512
_MAX_ISSUES = 32

# Whitelisted expectation keys, mirrored from eval.geometry_assertions.
_EXPECT_INT_KEYS = frozenset({"min_verts", "max_verts", "min_faces", "max_faces"})
_EXPECT_BBOX_KEYS = frozenset({"bbox_min", "bbox_max"})
_MAX_EXPECT_INT = 10**9


def _error(code: str, message: str) -> dict[str, object]:
    return {"ok": False, "code": code, "message": message}


def _geometry_assertions():
    """Import the eval geometry assertion helpers, tolerating launch mode.

    The ``eval`` package lives at the repository root, which is on sys.path
    for pytest and ``python -m`` launches from the repo but NOT for direct
    script launches (e.g. the provider acceptance journey). Fall back to
    making the root importable relative to this module instead of relying on
    how the runtime process happened to be started.
    """
    try:
        from eval.geometry_assertions import evaluate, from_bridge_stats

        return evaluate, from_bridge_stats
    except ImportError:
        pass
    import sys

    root = Path(__file__).resolve().parents[2]
    if (root / "eval" / "geometry_assertions.py").is_file():
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))
        try:
            from eval.geometry_assertions import evaluate, from_bridge_stats

            return evaluate, from_bridge_stats
        except ImportError:
            pass
    return None


def _context(runtime: ToolRuntime) -> RuntimeToolContext | None:
    context = getattr(runtime, "context", None)
    return context if type(context) is RuntimeToolContext else None


@tool
async def render_sketch(
    html_content: str,
    sketch_name: str,
    runtime: ToolRuntime,
) -> dict[str, object]:
    """Render a Three.js/HTML sketch to a PNG for user review.

    Writes the bounded HTML under the runtime sketches directory and renders
    it with a local headless browser. Returns the image path on success, or
    a structured error (browser missing / timeout / render failure).
    """
    if (
        type(html_content) is not str
        or not html_content
        or len(html_content) > _MAX_HTML_CHARS
        or type(sketch_name) is not str
        or not sketch_name
        or len(sketch_name) > _MAX_NAME_CHARS
    ):
        return _error("runtime.tool_input_invalid", "render_sketch input is invalid.")
    context = _context(runtime)
    if context is None or context.sketch is None:
        return _error(
            "bridge.unavailable",
            "A trusted sketch render provider is unavailable.",
        )
    try:
        result = await context.sketch.render_sketch(
            html_content=html_content, sketch_name=sketch_name
        )
    except Exception:
        return _error(
            "bridge.unavailable",
            "The trusted sketch render provider returned an error.",
        )
    if not isinstance(result, dict) or type(result.get("ok")) is not bool:
        return _error(
            "bridge.unavailable",
            "The trusted sketch render provider returned a malformed result.",
        )
    return dict(result)


def _valid_expect(expect: Any) -> dict[str, Any] | None:
    """Whitelist-validate expectation keys for verify_geometry."""
    if type(expect) is not dict or not expect or len(expect) > 6:
        return None
    cleaned: dict[str, Any] = {}
    for key, value in expect.items():
        if key in _EXPECT_INT_KEYS:
            if type(value) is not int or not 0 <= value <= _MAX_EXPECT_INT:
                return None
            cleaned[key] = value
        elif key in _EXPECT_BBOX_KEYS:
            if (
                type(value) is not list
                or len(value) != 3
                or any(
                    type(v) not in (int, float) or not math.isfinite(v) for v in value
                )
            ):
                return None
            cleaned[key] = [float(v) for v in value]
        else:
            return None
    return cleaned


@tool
async def verify_geometry(
    node_path: str,
    expect: dict[str, Any],
    runtime: ToolRuntime,
) -> dict[str, object]:
    """Check geometry stats of a node against expectation ranges.

    Expect keys (all optional): min_verts, max_verts, min_faces, max_faces,
    bbox_min [x,y,z], bbox_max [x,y,z]. Returns {ok, issues, stats} — feed a
    non-empty issues list back into the fix loop before committing.
    """
    if (
        type(node_path) is not str
        or not node_path
        or len(node_path) > _MAX_NODE_PATH_CHARS
        or not node_path.startswith("/")
    ):
        return _error("runtime.tool_input_invalid", "node_path is invalid.")
    cleaned_expect = _valid_expect(expect)
    if cleaned_expect is None:
        return _error(
            "runtime.tool_input_invalid",
            "expect must use only min_verts/max_verts/min_faces/max_faces "
            "(ints) and bbox_min/bbox_max ([x,y,z] finite numbers).",
        )
    context = _context(runtime)
    if context is None:
        return _error(
            "bridge.unavailable",
            "A trusted read-only provider is unavailable.",
        )
    try:
        raw = await context.read_only.geometry_stats(node_path)
    except Exception:
        return _error(
            "bridge.unavailable",
            "The trusted read-only provider returned an error.",
        )
    if not isinstance(raw, dict):
        return _error(
            "bridge.unavailable",
            "The read-only provider returned malformed geometry stats.",
        )
    if raw.get("ok") is False:
        return _error(
            "bridge.unavailable",
            "The read-only provider could not read geometry stats.",
        )
    # BridgeReadOnlyProvider returns a bounded envelope with the actual
    # Houdini counters under ``geometry_stats``. Retain the historical flat
    # shape for compatibility with lightweight providers and older fixtures.
    bridge_stats = raw.get("geometry_stats", raw)
    if not isinstance(bridge_stats, Mapping):
        return _error(
            "bridge.unavailable",
            "The read-only provider returned malformed geometry stats.",
        )

    assertions = _geometry_assertions()
    if assertions is None:
        return _error(
            "verify.backend_missing",
            "The geometry assertion library (eval package) is not importable "
            "from this runtime environment.",
        )
    evaluate, from_bridge_stats = assertions

    stats = from_bridge_stats(dict(bridge_stats))
    verdict = evaluate(stats, cleaned_expect)
    issues = [str(issue)[:256] for issue in verdict.get("issues", [])][:_MAX_ISSUES]
    return {
        "ok": bool(verdict.get("ok")),
        "issues": issues,
        "stats": {
            "verts": stats.get("verts", 0),
            "faces": stats.get("faces", 0),
            "bbox_size": (stats.get("bbox") or {}).get("size"),
        },
    }


__all__ = ["render_sketch", "verify_geometry"]
