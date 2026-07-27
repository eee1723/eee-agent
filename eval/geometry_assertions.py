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
    return {
        "verts": s.get("points", 0),
        # The production Secure Bridge uses ``primitives``. Keep ``prims`` as
        # a compatibility fallback for older eval fixtures and adapters.
        "faces": s.get("primitives", s.get("prims", 0)),
        "bbox": bbox,
    }


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


def mesh_component_count(path: str) -> Optional[int]:
    """Count geometric connected components of a Wavefront .obj mesh.

    Parses ``v``/``f`` lines and unions vertex indices shared by faces
    (union-find). Vertices not referenced by any face count as singleton
    components. Returns None when the file is missing or contains no
    vertices; otherwise the component count (>= 1). Used by the
    "disconnected / detached parts" assertion.
    """
    nverts = 0
    faces: List[List[int]] = []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if line.startswith("v "):
                    parts = line.split()
                    if len(parts) >= 4:
                        try:
                            float(parts[1]); float(parts[2]); float(parts[3])
                        except ValueError:
                            continue
                        nverts += 1
                elif line.startswith("f "):
                    idx: List[int] = []
                    for tok in line.split()[1:]:
                        try:
                            i = int(tok.split("/")[0])
                        except ValueError:
                            continue
                        if i < 0:  # negative indices are relative to the end
                            i = nverts + 1 + i
                        if 1 <= i <= nverts:
                            idx.append(i - 1)
                    if idx:
                        faces.append(idx)
    except FileNotFoundError:
        return None
    if nverts == 0:
        return None

    parent = list(range(nverts))

    def find(a: int) -> int:
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    for face in faces:
        root = face[0]
        for other in face[1:]:
            ra, rb = find(root), find(other)
            if ra != rb:
                parent[ra] = rb

    return len({find(i) for i in range(nverts)})


def evaluate_parts(parts: Dict[str, Dict[str, Any]],
                   expected: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """Per-part assertions over {part_name: stats} (same stats shape as
    ``evaluate``). ``expected`` maps part_name -> {"required": bool
    (default True), plus any key ``evaluate`` supports}.

    Returns {"ok": bool, "issues": [...]}. Every issue is prefixed with the
    part name (e.g. "leg_fl: verts 0 < min 8" or "leg_fl: missing") so the
    failing part is directly locatable by the agent.
    """
    issues: List[str] = []
    for name, spec in expected.items():
        spec = spec or {}
        required = spec.get("required", True)
        stats = parts.get(name)
        if stats is None:
            if required:
                issues.append(f"{name}: missing")
            continue
        sub_spec = {k: v for k, v in spec.items() if k != "required"}
        sub = evaluate(stats, sub_spec)
        issues.extend(f"{name}: {msg}" for msg in sub["issues"])
    return {"ok": len(issues) == 0, "issues": issues}


def evaluate_color(attr: Optional[Dict[str, Any]], expected_rgb,
                   tol: float = 0.05) -> Dict[str, Any]:
    """Deterministic part of the color/material assertion.

    ``attr`` is the Cd attribute stats of one part's geometry:
    {"mean": [r, g, b], "present": bool}. Compares each channel of the mean
    against ``expected_rgb`` within ``tol``. Returns {"ok", "issues"}; when
    ``attr`` is None or ``present`` is False the single issue is
    "color attribute missing".
    """
    if attr is None or not attr.get("present"):
        return {"ok": False, "issues": ["color attribute missing"]}
    mean = attr.get("mean")
    if mean is None or len(mean) < 3:
        return {"ok": False, "issues": ["color attribute missing mean"]}
    issues: List[str] = []
    exp = list(expected_rgb)
    for i, ch in enumerate("rgb"):
        diff = abs(float(mean[i]) - float(exp[i]))
        if diff > tol:
            issues.append(
                f"{ch} mean {float(mean[i]):.3f} vs expected "
                f"{float(exp[i]):.3f} (|diff| {diff:.3f} > tol {tol})"
            )
    return {"ok": len(issues) == 0, "issues": issues}
