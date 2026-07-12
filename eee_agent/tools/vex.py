"""VEX injection tool: add an attribwrangle wired into the graph."""
from __future__ import annotations

from typing import Optional

from langchain_core.tools import tool

from eee_agent.bridge import hou_client, serialize
from eee_agent.tools.inspect import cook_and_diagnose

# Verified against Houdini 21.0.440 attribwrangle "class" parm menu items.
_RUN_OVER_VALUES = {"detail", "primitive", "point", "vertex", "number"}


@tool
def set_vex(
    input_node_path: str,
    snippet: str,
    run_over: str = "point",
    group: Optional[str] = None,
    name: str = "wrangle",
) -> dict:
    """Add an Attribute Wrangle (VEX) node fed by ``input_node_path``.

    This is the preferred way to do per-point/prim/detail geometry work — keep
    heavy logic in VEX, not Python. The wrangle is created under the input node's
    parent and wired to its input 0.

    Args:
        input_node_path: Node whose geometry the wrangle processes.
        snippet: The VEX code (contents of the wrangle's "snippet" parm).
        run_over: One of detail, primitive, point, vertex, number (default point).
        group: Optional group pattern (e.g. "@group_window" or "0-99").
        name: Instance name for the new wrangle node.

    Returns the new wrangle node's info.
    """
    run_over = (run_over or "point").lower()
    if run_over not in _RUN_OVER_VALUES:
        return {"ok": False,
                "error": f"run_over must be one of {sorted(_RUN_OVER_VALUES)}"}

    def _fn(hou):
        src = hou.node(input_node_path)
        if src is None:
            raise RuntimeError(f"input node not found: {input_node_path}")
        parent = src.parent()
        wr = parent.createNode("attribwrangle", name)
        wr.setInput(0, src)
        wr.parm("snippet").set(snippet)
        wr.parm("class").set(run_over)
        if group:
            wr.parm("group").set(group)
        # Auto-cook so the agent learns VEX compile errors in THIS call and can
        # fix the offending line in place instead of deleting and retrying.
        ok, errs, warns = cook_and_diagnose(wr)
        info = serialize.node_info(wr)
        info["cooked"] = ok
        info["vex_errors"] = errs
        info["warnings"] = warns
        return info

    res, err = hou_client.run(_fn)
    if err is not None:
        return {"ok": False, "error": err}
    return {"ok": True, "node": res}
