"""Node-graph tools: create / connect / set parms / find / delete."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from langchain_core.tools import tool

from eee_agent.bridge import hou_client, serialize


@tool
def create_node(
    parent_path: str,
    node_type: str,
    name: str,
    connect_from: Optional[str] = None,
) -> dict:
    """Create a native Houdini SOP/object node.

    Args:
        parent_path: Parent node path, e.g. "/obj" or "/obj/geo1".
        node_type: Houdini node type id, e.g. "geo", "box", "attribwrangle",
            "copytopoints", "boolean", "grid", "transform", "merge", "file".
        name: Instance name (unique under parent).
        connect_from: Optional path of a node to wire into input 0 of the new node.

    Returns the new node's info (path/name/type/inputs). Prefer native SOP nodes
    for modeling; use set_vex to add a VEX wrangle.
    """
    def _fn(hou):
        parent = hou.node(parent_path)
        if parent is None:
            raise RuntimeError(f"parent not found: {parent_path}")
        node = parent.createNode(node_type, name)
        if connect_from:
            src = hou.node(connect_from)
            if src is None:
                raise RuntimeError(f"connect_from not found: {connect_from}")
            node.setInput(0, src)
        return serialize.node_info(node)

    res, err = hou_client.run(_fn)
    if err is not None:
        return {"ok": False, "error": err}
    return {"ok": True, "node": res}


@tool
def connect_nodes(node_path: str, input_index: int, source_path: str) -> dict:
    """Wire ``source_path`` into input ``input_index`` (0-based) of ``node_path``."""
    def _fn(hou):
        node = hou.node(node_path)
        src = hou.node(source_path)
        if node is None:
            raise RuntimeError(f"node not found: {node_path}")
        if src is None:
            raise RuntimeError(f"source not found: {source_path}")
        node.setInput(int(input_index), src)
        return serialize.node_info(node)

    res, err = hou_client.run(_fn)
    if err is not None:
        return {"ok": False, "error": err}
    return {"ok": True, "node": res}


@tool
def set_parms(node_path: str, parms: Dict[str, Any]) -> dict:
    """Set multiple parameters on a node.

    ``parms`` maps a parm name to its value. A scalar (int/float/str) sets a
    single-channel parm via ``parm``; a list/tuple sets a multi-channel parm via
    ``parmTuple`` (e.g. {"size": [2, 2, 2]} or {"t": [0, 1, 0]}). String values
    that start with "=" or contain expressions are set as-is (Houdini will
    evaluate them).

    Returns per-parm success/failure.
    """
    def _fn(hou):
        node = hou.node(node_path)
        if node is None:
            raise RuntimeError(f"node not found: {node_path}")
        results: Dict[str, Any] = {}
        for pname, value in parms.items():
            try:
                if isinstance(value, (list, tuple)):
                    node.parmTuple(pname).set(tuple(value))
                else:
                    node.parm(pname).set(value)
                results[pname] = "ok"
            except Exception as e:  # per-parm isolation
                results[pname] = f"error: {type(e).__name__}: {e}"
        return {"set": results}

    res, err = hou_client.run(_fn)
    if err is not None:
        return {"ok": False, "error": err}
    return {"ok": True, **res}


@tool
def find_nodes(pattern: str, parent_path: str = "/obj") -> dict:
    """Find child nodes of ``parent_path`` matching a glob ``pattern`` (e.g. "box*").

    If ``pattern`` looks like an absolute path, looks that exact node up instead.
    """
    def _fn(hou):
        if pattern.startswith("/"):
            n = hou.node(pattern)
            return {"nodes": [serialize.node_info(n)] if n is not None else []}
        parent = hou.node(parent_path)
        if parent is None:
            raise RuntimeError(f"parent not found: {parent_path}")
        matches = list(parent.glob(pattern))
        return {"nodes": [serialize.node_info(n) for n in matches]}

    res, err = hou_client.run(_fn)
    if err is not None:
        return {"ok": False, "error": err}
    return {"ok": True, **res}


@tool
def delete_node(node_path: str) -> dict:
    """Delete (destroy) a node by path."""
    def _fn(hou):
        node = hou.node(node_path)
        if node is None:
            raise RuntimeError(f"node not found: {node_path}")
        node.destroy()
        return {"deleted": node_path}

    res, err = hou_client.run(_fn)
    if err is not None:
        return {"ok": False, "error": err}
    return {"ok": True, **res}


# parmTemplate types that carry no user-facing value — skip them in listings.
_SKIP_PARM_TYPES = {"Folder", "FolderSet", "Separator", "Label"}


@tool
def describe_node_type(node_type: str) -> dict:
    """List the parameters of a Houdini SOP node type by creating a temp node inside
    a throwaway geo container, reading its parms, then deleting everything.

    CALL THIS whenever you are unsure what parms a node type has — never guess parm
    names or values. Returns each parm's name, type, and (for menu parms) the
    allowed values. Some nodes (polyextrude::2.0, boolean) have 60-130 parms; you
    cannot guess them — introspect first. Works for SOP types (box, grid, blast,
    polyextrude::2.0, boolean, copytopoints, merge, ...).

    Examples: describe_node_type("polyextrude::2.0"), describe_node_type("blast").
    """
    def _fn(hou):
        obj = hou.node("/obj")
        if obj is None:
            raise RuntimeError("/obj not found")
        container = obj.createNode("geo", "eee_describe_tmp_geo")
        try:
            tmp = container.createNode(node_type, "eee_describe_tmp")
            parms = []
            for p in tmp.parms():
                try:
                    ptype = str(p.parmTemplate().type()).replace("parmTemplateType.", "")
                except Exception:
                    ptype = "?"
                if ptype in _SKIP_PARM_TYPES:
                    continue
                entry = {"name": str(p.name()), "type": ptype}
                try:
                    items = list(p.menuItems())
                    if items:
                        entry["menu"] = {str(i): str(lab)
                                         for i, lab in zip(items, p.menuLabels())}
                except Exception:
                    pass
                parms.append(entry)
            return {"type": node_type, "parm_count": len(tmp.parms()), "parms": parms}
        finally:
            container.destroy()

    res, err = hou_client.run(_fn)
    if err is not None:
        return {"ok": False, "error": err}
    return {"ok": True, **res}
