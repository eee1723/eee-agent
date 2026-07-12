"""Composite tools: create + wire common multi-input SOPs in one call.

Python is only glue here — each tool creates native SOP node(s) and connects
inputs. No parm guessing: where a parm matters, the agent sets it afterwards via
set_parms (informed by describe_node_type). These exist to cut step count and
pre-wire correct input ordering (a common source of errors).
"""
from __future__ import annotations

from typing import List

from langchain_core.tools import tool

from eee_agent.bridge import hou_client, serialize


@tool
def merge_nodes(node_paths: List[str], parent_path: str, name: str = "merge") -> dict:
    """Create a Merge SOP and wire every node in ``node_paths`` into its inputs
    (in order). Use this to combine walls/floors/roof into one stream.

    All ``node_paths`` must share the same parent (the Merge is created there).
    """
    def _fn(hou):
        parent = hou.node(parent_path)
        if parent is None:
            raise RuntimeError(f"parent not found: {parent_path}")
        merge = parent.createNode("merge", name)
        for i, p in enumerate(node_paths):
            src = hou.node(p)
            if src is None:
                raise RuntimeError(f"input not found: {p}")
            merge.setInput(i, src)
        return serialize.node_info(merge)

    res, err = hou_client.run(_fn)
    if err is not None:
        return {"ok": False, "error": err}
    return {"ok": True, "node": res}


@tool
def copy_to_points(proto_path: str, points_path: str, name: str = "copytopoints") -> dict:
    """Create a Copy to Points SOP: copies ``proto_path`` geometry onto the points
    of ``points_path``. Input 0 = proto (what to copy), input 1 = target points.

    Use this for floor stacking, scattering windows, instancing — generate the
    target points (e.g. with a grid/add/scatter) first.
    """
    def _fn(hou):
        proto = hou.node(proto_path)
        pts = hou.node(points_path)
        if proto is None:
            raise RuntimeError(f"proto not found: {proto_path}")
        if pts is None:
            raise RuntimeError(f"points not found: {points_path}")
        if proto.parent().path() != pts.parent().path():
            raise RuntimeError("proto and points must share the same parent")
        node = proto.parent().createNode("copytopoints", name)
        node.setInput(0, proto)
        node.setInput(1, pts)
        return serialize.node_info(node)

    res, err = hou_client.run(_fn)
    if err is not None:
        return {"ok": False, "error": err}
    return {"ok": True, "node": res}
