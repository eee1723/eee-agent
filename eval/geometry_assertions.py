"""Pure geometry assertions for the eval framework — no Houdini needed.

Works on .obj files the agent exports (parses vertex/face counts + bbox) and on
the dicts returned by the geometry_stats tool. Testable standalone, so the eval
logic can be unit-tested without a running bridge.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional


def parse_obj(path: str) -> Optional[Dict[str, Any]]:
    """Parse a Wavefront .obj: return {verts, faces, bbox:{min,max,size}} or None."""
    verts = 0
    faces = 0
    mins = [math.inf, math.inf, math.inf]
    maxs = [-math.inf, -math.inf, -math.inf]
    found_v = False
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("v "):
                    parts = line.split()
                    if len(parts) >= 4:
                        try:
                            x, y, z = float(parts[1]), float(parts[2]), float(parts[3])
                        except ValueError:
                            continue
                        verts += 1
                        found_v = True
                        if x < mins[0]: mins[0] = x
                        if y < mins[1]: mins[1] = y
                        if z < mins[2]: mins[2] = z
                        if x > maxs[0]: maxs[0] = x
                        if y > maxs[1]: maxs[1] = y
                        if z > maxs[2]: maxs[2] = z
                elif line.startswith("f "):
                    faces += 1
    except FileNotFoundError:
        return None
    if not found_v:
        return {"verts": verts, "faces": faces, "bbox": None}
    return {
        "verts": verts,
        "faces": faces,
        "bbox": {"min": mins, "max": maxs,
                 "size": [maxs[i] - mins[i] for i in range(3)]},
    }


def from_bridge_stats(s: Dict[str, Any]) -> Dict[str, Any]:
    """Normalize a geometry_stats tool dict into {verts, faces, bbox}."""
    bb = s.get("bbox")
    bbox = None
    if bb:
        mn, mx = bb.get("min"), bb.get("max")
        bbox = {"min": list(mn), "max": list(mx),
                "size": [mx[i] - mn[i] for i in range(3)]}
    return {"verts": s.get("points", 0), "faces": s.get("prims", 0), "bbox": bbox}


def evaluate(stats: Optional[Dict[str, Any]], expected: Dict[str, Any]) -> Dict[str, Any]:
    """Evaluate parsed stats against expected ranges. Returns {ok, issues}.

    Expected keys (all optional): min_verts, max_verts, min_faces, max_faces,
    bbox_min [x,y,z] (lower envelope), bbox_max [x,y,z] (upper envelope).
    """
    issues: List[str] = []
    if stats is None:
        return {"ok": False, "issues": ["no stats (parse failed / file missing)"]}

    v, f = stats.get("verts", 0), stats.get("faces", 0)
    if "min_verts" in expected and v < expected["min_verts"]:
        issues.append(f"verts {v} < min {expected['min_verts']}")
    if "max_verts" in expected and v > expected["max_verts"]:
        issues.append(f"verts {v} > max {expected['max_verts']}")
    if "min_faces" in expected and f < expected["min_faces"]:
        issues.append(f"faces {f} < min {expected['min_faces']}")
    if "max_faces" in expected and f > expected["max_faces"]:
        issues.append(f"faces {f} > max {expected['max_faces']}")

    bb = stats.get("bbox")
    if bb is None:
        issues.append("no bbox (empty geometry?)")
    else:
        mn, mx = bb["min"], bb["max"]
        if not all(math.isfinite(v) for v in mn + mx):
            issues.append("non-finite bbox")
        if "bbox_min" in expected:
            for ax, name in enumerate("xyz"):
                if mn[ax] < expected["bbox_min"][ax] - 1e-6:
                    issues.append(f"{name} min {mn[ax]:.2f} < {expected['bbox_min'][ax]}")
        if "bbox_max" in expected:
            for ax, name in enumerate("xyz"):
                if mx[ax] > expected["bbox_max"][ax] + 1e-6:
                    issues.append(f"{name} max {mx[ax]:.2f} > {expected['bbox_max'][ax]}")

    return {"ok": len(issues) == 0, "issues": issues}


def check_file(path: str, expected: Dict[str, Any]) -> Dict[str, Any]:
    """Convenience: parse an .obj and evaluate. Returns {ok, issues, stats}."""
    stats = parse_obj(path)
    res = evaluate(stats, expected)
    res["stats"] = stats
    return res
