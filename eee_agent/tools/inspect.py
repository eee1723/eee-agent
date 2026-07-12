"""Inspection / verification tools: cook, geometry stats, validate, export.

These power the observe-and-correct loop. The agent should cook + read stats +
validate after every modeling step, and only finish once validation passes.

Cook errors (especially VEX) are extracted CLEANLY via cook_and_diagnose(): after
a failed cook, node.errors() holds the actionable message (e.g. "Call to undefined
function 'foo'. (1,7:19)" or "No matching function ... Candidates are: ..."), not
the giant rpyc remote traceback. This lets the agent fix the offending VEX line
instead of deleting and retrying.
"""
from __future__ import annotations

import math
import os
from typing import List, Optional

from langchain_core.tools import tool

from eee_agent.bridge import hou_client, serialize
from eee_agent.config import resolve_path


def cook_and_diagnose(node) -> tuple[bool, str, str]:
    """Cook a node and return (ok, errors_str, warnings_str) with CLEAN messages.

    On a VEX/Houdini cook failure, node.errors() contains the actionable error
    (function name + line:col, or matching-function candidates). We surface that
    instead of the rpyc-wrapped OperationFailed traceback.
    """
    try:
        node.cook(True)
    except Exception:
        # VEX/Houdini errors still surface via node.errors() after a failed cook.
        pass
    try:
        errs = [str(e) for e in node.errors()]
    except Exception:
        errs = []
    try:
        warns = [str(w) for w in node.warnings()]
    except Exception:
        warns = []
    return (len(errs) == 0), " | ".join(errs), " | ".join(warns)


@tool
def cook_node(node_path: str, force: bool = True) -> dict:
    """Cook a node and return CLEAN cook errors/warnings.

    Always call this (or geometry_stats) after changing a graph. On VEX errors the
    returned ``errors`` string names the function and line:col — fix that line in
    place; do NOT delete and rebuild.
    """
    def _fn(hou):
        node = hou.node(node_path)
        if node is None:
            raise RuntimeError(f"node not found: {node_path}")
        ok, errs, warns = cook_and_diagnose(node)
        return {"cooked": ok, "errors": errs, "warnings": warns}

    res, err = hou_client.run(_fn)
    if err is not None:
        return {"ok": False, "error": err}
    return {"ok": res["cooked"], **res}


@tool
def geometry_stats(node_path: str) -> dict:
    """Cook and return geometry statistics for a SOP node, or the cook error.

    Returns point/prim counts, bounding box (min/max/size), and attribute lists.
    If the node fails to cook (e.g. bad VEX), returns the clean error instead —
    read it and fix the graph line in place.
    """
    def _fn(hou):
        node = hou.node(node_path)
        if node is None:
            raise RuntimeError(f"node not found: {node_path}")
        ok, errs, warns = cook_and_diagnose(node)
        if not ok:
            return {"cooked": False, "errors": errs, "warnings": warns}
        geo = node.geometry()
        if geo is None:
            raise RuntimeError("node has no geometry (not a SOP?)")
        return {"cooked": True, **serialize.geometry_summary(geo)}

    res, err = hou_client.run(_fn)
    if err is not None:
        return {"ok": False, "error": err}
    if not res.get("cooked"):
        return {"ok": False, "errors": res.get("errors", ""), "warnings": res.get("warnings", "")}
    return {"ok": True, **res}


@tool
def validate_geometry(
    node_path: str,
    min_points: int = 1,
    min_prims: int = 0,
    bbox_min: Optional[List[float]] = None,
    bbox_max: Optional[List[float]] = None,
    required_groups: Optional[List[str]] = None,
) -> dict:
    """Validate a node's cooked geometry against simple rules. Returns issues list.

    Checks: cook succeeds, non-empty (>= min_points / min_prims), finite coords,
    bbox within optional [bbox_min, bbox_max] per-axis, named groups present.
    Cook errors are included as issues so you can fix them in place.
    """
    def _fn(hou):
        node = hou.node(node_path)
        if node is None:
            raise RuntimeError(f"node not found: {node_path}")
        ok, errs, warns = cook_and_diagnose(node)
        issues: List[str] = []
        if not ok:
            issues.append(f"cook error: {errs}")
            return {"ok": False, "issues": issues, "stats": None}
        geo = node.geometry()
        summary = serialize.geometry_summary(geo)

        if summary["points"] < min_points:
            issues.append(f"too few points: {summary['points']} < {min_points}")
        if summary["prims"] < min_prims:
            issues.append(f"too few prims: {summary['prims']} < {min_prims}")

        bb = summary["bbox"]
        if bb is None:
            issues.append("no bbox (empty geometry?)")
        else:
            flat = bb["min"] + bb["max"]
            if not all(math.isfinite(v) for v in flat):
                issues.append("non-finite bbox")
            for ax, axis_name in enumerate("xyz"):
                if bbox_min is not None and bb["min"][ax] < bbox_min[ax] - 1e-6:
                    issues.append(f"{axis_name} min {bb['min'][ax]:.2f} < {bbox_min[ax]}")
                if bbox_max is not None and bb["max"][ax] > bbox_max[ax] + 1e-6:
                    issues.append(f"{axis_name} max {bb['max'][ax]:.2f} > {bbox_max[ax]}")

        if required_groups:
            try:
                present = {str(g.name()) for g in geo.primGroups()}
                present |= {str(g.name()) for g in geo.pointGroups()}
            except Exception:
                present = set()
            for rg in required_groups:
                if rg not in present:
                    issues.append(f"missing group: {rg}")

        return {"ok": len(issues) == 0, "issues": issues, "stats": summary}

    res, err = hou_client.run(_fn)
    if err is not None:
        return {"ok": False, "error": err}
    return res


@tool
def export_geometry(node_path: str, file_path: str) -> dict:
    """Export a node's cooked geometry to ``file_path``.

    Format is inferred from the extension (.obj, .bgeo/.bgeo.sc, .usd/.usda, .ply).
    Implemented via hou.Geometry.saveToFile (verified available in Houdini 21).
    """
    def _fn(hou):
        node = hou.node(node_path)
        if node is None:
            raise RuntimeError(f"node not found: {node_path}")
        ok, errs, warns = cook_and_diagnose(node)
        if not ok:
            return {"exported": False, "errors": errs}
        full = resolve_path(file_path)
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        node.geometry().saveToFile(full)
        return {"exported": True, "path": full}

    res, err = hou_client.run(_fn)
    if err is not None:
        return {"ok": False, "error": err}
    if not res.get("exported"):
        return {"ok": False, "errors": res.get("errors", "")}
    return {"ok": True, **res}
