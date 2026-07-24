"""Sandbox verify gates for the scratch+verify+commit workflow (Phase 2).

Ported from Pi (``Edini/python3.11libs/edini``) and adapted to run inside the
Houdini process as part of the ``scratch.commit`` bridge operation. These gates
protect the REAL scene: a sandbox build is free exploration, but before its
nodes are promoted into the real scene they must pass hard quality gates.

The four gates run in order; ANY hard failure refuses the commit and the
sandbox is preserved (so the agent can fix and re-commit):

1. **bake gate** — every prim with a ``@component_id`` must carry a non-zero
   ``@edini_world_axis`` (the deterministic construction axis). A zero vector
   or missing attribute means the build skipped the bake.
2. **structure gate** — refuses monolithic assets (>= 3 distinct components,
   all geometry from a single Python SOP, no modular assembly nodes).
3. **orientation gate** — compares baked world axes against declared expected
   axes using pure-Python PCA math (ported in ``eee_agent.modeling.
   orientation_math``); only warns on PCA divergence (the bake is authoritative).
4. **health gate** — orphan_points / open_curves are hard failures;
   degenerate / nonmanifold / open_boundary / coincident are advisory.

This module imports ``hou`` only inside functions (so the module is importable
without Houdini for struct/dataclass tests). The math is imported from the
hou-free ``eee_agent.modeling.orientation_math``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from eee_agent.modeling.orientation_math import (
    AXIS_VECTORS,
    KIND_EIGEN_RANK,
    axis_angle_between,
    compute_covariance,
    dominant_axis_name,
    flip_to_hemisphere,
    jacobi_eigen_3x3,
)

# --------------------------------------------------------------------------
# severity tiers (two-tier: blocking vs advisory, ported from geometry_inspect)
# --------------------------------------------------------------------------

HEALTH_BLOCKING_CHECKS = ("orphan_points", "open_curves")
HEALTH_ADVISORY_CHECKS = (
    "degenerate_prims",
    "nonmanifold_edges",
    "open_boundary_edges",
    "coincident_points",
)

# Modular assembly node types (presence of any means the asset decomposes
# properly instead of stuffing everything into one Python SOP). Ported from
# harness._MODULAR_NODE_TYPES.
MODULAR_NODE_TYPES = frozenset({
    "copytopoints", "copytopoints::2.0", "copy", "copystamp",
    "sweep", "sweep::2.0", "skin", "rails",
    "foreach::count", "foreach::piece", "foreach", "foreach_begin",
    "xformpieces", "transformpieces", "instanceto",
    "boolean", "boolean::2.0", "polyextrude", "polyextrude::2.0",
    "pack", "unpack",
})



# --------------------------------------------------------------------------
# Gate result types
# --------------------------------------------------------------------------


def _gate_result(
    name: str,
    *,
    passed: bool,
    hard: bool,
    detail: Mapping[str, Any] | None = None,
    reason: str = "",
) -> dict[str, Any]:
    """Build one gate result dict.

    ``hard=True`` means a failure of this gate refuses the commit.
    ``hard=False`` means advisory (failure is recorded but never blocks).
    """
    return {
        "gate": name,
        "passed": bool(passed),
        "hard": bool(hard),
        "reason": reason,
        "detail": dict(detail) if detail is not None else {},
    }


# --------------------------------------------------------------------------
# Gate 1: bake gate
# --------------------------------------------------------------------------


def verify_world_axes_baked(out_node: Any) -> dict[str, Any]:
    """G1: confirm every prim on the output carries a non-zero
    ``edini_world_axis`` (the deterministic construction axis).

    The zero vector (0,0,0) is the ``addAttrib`` default and the sentinel for
    "not baked" — a legitimate axis always has unit length after rotation, so a
    zero can never be a real construction axis.

    SKIPPED (passes vacuously) when there are no ``@component_id`` prims —
    simple single-piece assets have no construction axes to bake.
    """
    try:
        geo = out_node.geometry()
    except Exception:
        return _gate_result(
            "bake", passed=False, hard=True,
            reason="could not read geometry",
        )
    if geo is None:
        return _gate_result(
            "bake", passed=False, hard=True, reason="geometry is None"
        )

    comp_attr = _find_prim_attrib(geo, "component_id")
    if comp_attr is None:
        # No component_id prims → nothing to bake. Pass vacuously.
        return _gate_result(
            "bake", passed=True, hard=True,
            detail={"skipped": "no component_id prims"},
        )

    # Collect distinct component_ids so we only check prims that have one.
    missing_cids: set[str] = set()
    has_any_cid = False
    for prim in geo.prims():
        cid = _string_attrib(prim, "component_id")
        if not cid:
            continue
        has_any_cid = True
        axis = _read_axis_attrib(prim, "edini_world_axis")
        if axis is None or axis == (0.0, 0.0, 0.0):
            missing_cids.add(cid)

    if not has_any_cid:
        return _gate_result(
            "bake", passed=True, hard=True,
            detail={"skipped": "no component_id prims"},
        )

    if missing_cids:
        return _gate_result(
            "bake", passed=False, hard=True,
            reason=(
                f"{len(missing_cids)} component(s) missing a non-zero "
                f"edini_world_axis: {sorted(missing_cids)[:10]}"
            ),
            detail={"missing_components": sorted(missing_cids)},
        )
    return _gate_result("bake", passed=True, hard=True)


# --------------------------------------------------------------------------
# Gate 2: structure gate
# --------------------------------------------------------------------------


def check_modular_structure(root: Any) -> dict[str, Any]:
    """G2: detect monolithic procedural assets that violate modular decomposition.

    Monolithic (a failure) when: >= 3 distinct component_ids AND all geometry
    originates from a single Python SOP AND there are NO modular assembly nodes
    (copytopoints/sweep/foreach/boolean/polyextrude).

    Known limitation: this walks the whole sandbox container
    (``root.allSubChildren()``). A scratch sandbox is reused across several
    scratch_exec calls, so abandoned intermediate nodes the agent left behind
    are counted too, which can inflate component counts and bias the
    monolithic heuristic. Ideally the scope would be the output node's input
    dependency subgraph; that requires a HOM dependency walk and is deferred.
    """
    try:
        children = [
            c for c in root.allSubChildren() if hasattr(c, "geometry")
        ]
    except Exception:
        children = []

    python_sops = []
    modular_nodes = []
    type_counts: dict[str, int] = {}

    for child in children:
        tname = _node_type_name(child)
        tcomp = _node_type_components(child)
        type_counts[tcomp] = type_counts.get(tcomp, 0) + 1
        if tname == "python":
            python_sops.append(child)
        if any(tcomp == m or tcomp.startswith(m + "::") for m in MODULAR_NODE_TYPES):
            modular_nodes.append(child)

    component_sources: dict[str, set[str]] = {}
    all_cids: set[str] = set()
    for child in children:
        cids = _geometry_component_ids(child)
        if cids:
            component_sources[child.path()] = cids
            all_cids |= cids

    python_paths = {p.path() for p in python_sops}
    cids_from_python: set[str] = set()
    for src_path, cids in component_sources.items():
        if src_path in python_paths:
            cids_from_python |= cids
    all_cids_from_one_python = (
        len(python_sops) >= 1
        and len(cids_from_python) >= 3
        and len(all_cids - cids_from_python) == 0
    )
    no_modular = len(modular_nodes) == 0

    max_python_lines = max(
        (_python_sop_code_line_count(p) for p in python_sops), default=0
    )

    detail: dict[str, Any] = {
        "python_sop_count": len(python_sops),
        "python_sop_max_lines": max_python_lines,
        "modular_node_count": len(modular_nodes),
        "modular_node_types": [_node_type_name(n) for n in modular_nodes],
        "distinct_component_ids": len(all_cids),
        "component_source_count": len(component_sources),
    }

    if len(all_cids) >= 3 and all_cids_from_one_python and no_modular:
        return _gate_result(
            "structure", passed=False, hard=True,
            reason=(
                f"Monolithic asset: {len(all_cids)} components all from a single "
                f"Python SOP with no modular assembly nodes. Decompose into "
                f"separate generators connected via Copy-to-Points/Sweep."
            ),
            detail=detail,
        )
    if max_python_lines > 200 and len(all_cids) >= 3 and no_modular:
        return _gate_result(
            "structure", passed=False, hard=True,
            reason=(
                f"Single Python SOP of {max_python_lines} lines producing a "
                f"{len(all_cids)}-component asset with no modular assembly nodes."
            ),
            detail=detail,
        )
    return _gate_result("structure", passed=True, hard=True, detail=detail)


# --------------------------------------------------------------------------
# Gate 3: orientation gate
# --------------------------------------------------------------------------


def verify_orientation(
    out_node: Any,
    checks: list[Mapping[str, Any]],
) -> dict[str, Any]:
    """G3: verify component orientations against declared expected axes.

    Each check dict: ``{component_id, kind, expected_axis, tolerance_deg,
    signed}``. The kind selects which eigenvector to compare (radial/planar =
    smallest eigenvalue; elongated = largest). The axis comes from the baked
    ``edini_world_axis`` attribute (authoritative) or a per-check
    ``construction_axis`` override. PCA is only a warning crosscheck.

    SKIPPED (passes vacuously) when there are no ``@component_id`` prims or no
    checks are provided.
    """
    if not checks:
        return _gate_result(
            "orientation", passed=True, hard=True,
            detail={"skipped": "no orientation checks requested"},
        )

    try:
        geo = out_node.geometry()
    except Exception:
        return _gate_result(
            "orientation", passed=False, hard=True,
            reason="could not read geometry",
        )
    if geo is None:
        return _gate_result(
            "orientation", passed=False, hard=True, reason="geometry is None"
        )

    comp_attr = _find_prim_attrib(geo, "component_id")
    if comp_attr is None:
        return _gate_result(
            "orientation", passed=True, hard=True,
            detail={"skipped": "no component_id prims"},
        )

    results: list[dict[str, Any]] = []
    passed_count = 0
    failed_count = 0

    for chk in checks:
        cid = chk.get("component_id")
        kind = str(chk.get("kind", "radial")).lower()
        expected_axis = str(chk.get("expected_axis", "Y")).upper()
        tol_deg = float(chk.get("tolerance_deg", 15.0))
        signed_kind = bool(chk.get("signed", False))

        entry: dict[str, Any] = {
            "component_id": cid,
            "kind": kind,
            "expected_axis": expected_axis,
            "tolerance_deg": tol_deg,
            "passed": False,
        }

        if kind not in KIND_EIGEN_RANK:
            entry["error"] = f"Unknown kind: {kind}"
            results.append(entry)
            failed_count += 1
            continue
        if expected_axis not in AXIS_VECTORS:
            entry["error"] = f"Invalid expected_axis: {expected_axis}"
            results.append(entry)
            failed_count += 1
            continue

        comp_prims = [
            p for p in geo.prims()
            if _string_attrib(p, "component_id") == cid
        ]
        if not comp_prims:
            entry["error"] = f"No prims with component_id={cid!r}"
            results.append(entry)
            failed_count += 1
            continue

        # Gather points for the optional PCA crosscheck.
        seen_pts: set[int] = set()
        pts: list[tuple[float, float, float]] = []
        for prim in comp_prims:
            for vtx in prim.vertices():
                pt = vtx.point()
                pid = pt.number()
                if pid in seen_pts:
                    continue
                seen_pts.add(pid)
                pos = pt.position()
                pts.append((float(pos[0]), float(pos[1]), float(pos[2])))

        # Read the authoritative axis: per-check override, else baked attr.
        construction_vec: tuple[float, float, float] | None = None
        override_axis = chk.get("construction_axis")
        if override_axis is not None and override_axis in AXIS_VECTORS:
            construction_vec = AXIS_VECTORS[override_axis]
        if construction_vec is None:
            construction_vec = _read_axis_attrib(comp_prims[0], "edini_world_axis")

        if construction_vec is None:
            entry.update({
                "method": "no_axis",
                "point_count": len(pts),
                "error": (
                    f"{cid} has no valid edini_world_axis prim attribute. "
                    "Bake it onto the prim before commit (note: the current "
                    "scratch catalog has no attribute-writing node, so "
                    "catalog-built assets cannot carry component axes yet)."
                ),
            })
            results.append(entry)
            failed_count += 1
            continue

        detected_vec = construction_vec
        expected_vec = AXIS_VECTORS[expected_axis]
        if not signed_kind:
            detected_vec = flip_to_hemisphere(detected_vec, expected_vec)
        detected_axis = dominant_axis_name(detected_vec)
        angle_deg, _ = axis_angle_between(detected_vec, expected_vec, signed=signed_kind)
        passed_check = angle_deg <= tol_deg

        entry.update({
            "method": "construction",
            "point_count": len(pts),
            "detected_axis": detected_axis,
            "angle_error_deg": round(angle_deg, 2),
            "passed": passed_check,
        })

        # Optional PCA crosscheck (warning-only). The PCA estimate is flipped
        # to the expected_axis hemisphere (the independent ground truth), NOT to
        # detected_vec — flipping to the value under test would mask an overall
        # sign error in the baked axis (e.g. Y baked as -Y) by forcing PCA onto
        # the wrong hemisphere and shrinking the reported divergence.
        if len(pts) >= 4:
            try:
                cov, _ = compute_covariance(pts)
                _, vecs = jacobi_eigen_3x3(cov)
                pca_vec = vecs[KIND_EIGEN_RANK[kind]]
                if pca_vec == (0.0, 0.0, 0.0):
                    # Degenerate covariance (collinear/coplanar points): the
                    # PCA estimate is not meaningful. Report it rather than let
                    # a zero vector read as a spurious axis downstream.
                    entry["pca_crosscheck"] = {"pca_axis": "degenerate"}
                else:
                    if not signed_kind:
                        pca_vec = flip_to_hemisphere(pca_vec, expected_vec)
                    pca_angle, _ = axis_angle_between(pca_vec, detected_vec, signed=False)
                    entry["pca_crosscheck"] = {
                        "pca_axis": dominant_axis_name(pca_vec),
                        "divergence_deg": round(pca_angle, 2),
                    }
                    if pca_angle > 2.0 * tol_deg:
                        entry["pca_crosscheck"]["warning"] = (
                            f"Declared construction axis ({detected_axis}) diverges "
                            f"from PCA estimate ({dominant_axis_name(pca_vec)}) by "
                            f"{round(pca_angle, 1)} deg."
                        )
            except Exception:
                pass

        results.append(entry)
        if passed_check:
            passed_count += 1
        else:
            failed_count += 1

    overall_passed = failed_count == 0
    return _gate_result(
        "orientation", passed=overall_passed, hard=True,
        reason="" if overall_passed else f"{failed_count} orientation check(s) failed",
        detail={
            "passed": passed_count,
            "failed": failed_count,
            "total": len(checks),
            "checks": results,
        },
    )


# --------------------------------------------------------------------------
# Gate 4: health gate
# --------------------------------------------------------------------------


def inspect_geometry_health(
    out_node: Any,
    *,
    degenerate_area_eps: float = 1e-7,
    coincident_eps: float = 1e-6,
    max_report: int = 20,
) -> dict[str, Any]:
    """G4: run structural health checks on the cooked geometry.

    Two-tier severity (ported from Pi): orphan_points/open_curves are BLOCKING;
    degenerate/nonmanifold/open_boundary/coincident are ADVISORY. Returns a gate
    result whose ``passed`` is True only if every BLOCKING check passes.
    """
    try:
        geo = out_node.geometry()
    except Exception:
        return _gate_result(
            "health", passed=False, hard=True,
            reason="could not read geometry",
        )
    if geo is None:
        return _gate_result(
            "health", passed=False, hard=True, reason="geometry is None"
        )

    points = geo.points()
    prims = geo.prims()
    n_points = len(points)
    n_prims = len(prims)

    # orphan points: not referenced by any prim.
    referenced: set[int] = set()
    for prim in prims:
        try:
            for vtx in prim.vertices():
                referenced.add(vtx.point().number())
        except Exception:
            continue
    orphan = [p.number() for p in points if p.number() not in referenced]

    # open curves: open curve/polyline primitives.
    open_curves: list[int] = []
    for prim in prims:
        try:
            type_name = prim.type().name().lower()
            is_curve = ("curve" in type_name) or (type_name == "polyline")
            if is_curve and hasattr(prim, "isClosed") and not prim.isClosed():
                open_curves.append(prim.number())
        except Exception:
            continue

    # degenerate prims: zero-area polygons.
    degenerate: list[int] = []
    for prim in prims:
        try:
            type_name = prim.type().name().lower()
            if "poly" not in type_name:
                continue
            verts = prim.vertices()
            if len(verts) < 3:
                degenerate.append(prim.number())
                continue
            area = None
            try:
                area = float(prim.intrinsicValue("measuredarea"))
            except Exception:
                area = _shoelace_fan_area(verts)
            if area is not None and area < degenerate_area_eps:
                degenerate.append(prim.number())
        except Exception:
            continue

    # edge valence: open boundary (1) and non-manifold (3+).
    edge_counts: dict[tuple[int, int], int] = {}
    for prim in prims:
        try:
            verts = prim.vertices()
            n = len(verts)
            if n < 2:
                continue
            for i in range(n):
                a = verts[i].point().number()
                b = verts[(i + 1) % n].point().number()
                if a == b:
                    continue
                key = _edge_key(a, b)
                edge_counts[key] = edge_counts.get(key, 0) + 1
        except Exception:
            continue
    open_boundary = [list(k) for k, c in edge_counts.items() if c == 1]
    nonmanifold = [list(k) for k, c in edge_counts.items() if c >= 3]

    # coincident points (skip for large point counts).
    coincident_pairs: list[list[int]] = []
    if n_points <= 4000:
        positions = [(p.number(), p.position()) for p in points]
        eps2 = coincident_eps * coincident_eps
        for i in range(len(positions)):
            na, pa = positions[i]
            for j in range(i + 1, len(positions)):
                nb, pb = positions[j]
                dx = pa[0] - pb[0]
                dy = pa[1] - pb[1]
                dz = pa[2] - pb[2]
                if dx * dx + dy * dy + dz * dz < eps2:
                    coincident_pairs.append([na, nb])
                    if len(coincident_pairs) >= max_report:
                        break
            if len(coincident_pairs) >= max_report:
                break

    checks = {
        "orphan_points": {"count": len(orphan), "passed": len(orphan) == 0,
                           "severity": "blocking"},
        "open_curves": {"count": len(open_curves), "passed": len(open_curves) == 0,
                        "severity": "blocking"},
        "degenerate_prims": {"count": len(degenerate), "passed": len(degenerate) == 0,
                             "severity": "advisory"},
        "nonmanifold_edges": {"count": len(nonmanifold), "passed": len(nonmanifold) == 0,
                              "severity": "advisory"},
        "open_boundary_edges": {"count": len(open_boundary),
                                "passed": len(open_boundary) == 0,
                                "severity": "advisory"},
        "coincident_points": {"count": len(coincident_pairs),
                              "passed": len(coincident_pairs) == 0,
                              "severity": "advisory"},
    }

    hard_failed = any(
        not checks[c]["passed"] for c in HEALTH_BLOCKING_CHECKS if c in checks
    )
    soft_total = sum(
        checks[c]["count"] for c in HEALTH_ADVISORY_CHECKS if c in checks
    )

    detail = {
        "point_count": n_points,
        "prim_count": n_prims,
        "blocking_checks": list(HEALTH_BLOCKING_CHECKS),
        "advisory_checks": list(HEALTH_ADVISORY_CHECKS),
        "hard_errors_count": sum(
            checks[c]["count"] for c in HEALTH_BLOCKING_CHECKS
            if c in checks and not checks[c]["passed"]
        ),
        "soft_warnings_count": soft_total,
        "checks": checks,
    }
    reason = "" if not hard_failed else (
        "blocking health checks failed: " +
        ", ".join(c for c in HEALTH_BLOCKING_CHECKS if not checks[c]["passed"])
    )
    return _gate_result("health", passed=not hard_failed, hard=True, reason=reason, detail=detail)


# --------------------------------------------------------------------------
# Orchestrator: run all four gates
# --------------------------------------------------------------------------


def run_verify_gates(
    sandbox_root: Any,
    output_node: Any,
    *,
    orientation_checks: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Run all four gates against the sandbox output node.

    Returns ``{passed, gates: [...], hard_failures: [...]}``. ``passed`` is True
    only if every HARD gate passed. Advisory gate failures are recorded but do
    not block.
    """
    bake = verify_world_axes_baked(output_node)
    structure = check_modular_structure(sandbox_root)
    orientation = verify_orientation(
        output_node, list(orientation_checks) if orientation_checks else []
    )
    health = inspect_geometry_health(output_node)

    gates = [bake, structure, orientation, health]
    hard_failures = [g for g in gates if g["hard"] and not g["passed"]]
    return {
        "passed": len(hard_failures) == 0,
        "gates": gates,
        "hard_failures": hard_failures,
    }


# --------------------------------------------------------------------------
# hou-free helper functions (pure logic, testable without Houdini)
# --------------------------------------------------------------------------


def _edge_key(a: int, b: int) -> tuple[int, int]:
    return (a, b) if a <= b else (b, a)


def _shoelace_fan_area(verts: Any) -> float:
    """Compute polygon area via a triangle fan (fallback when intrinsicValue
    is unavailable)."""
    try:
        if len(verts) < 3:
            return 0.0
        p0 = verts[0].point().position()
        total = 0.0
        for k in range(1, len(verts) - 1):
            p1 = verts[k].point().position()
            p2 = verts[k + 1].point().position()
            e01 = (p1[0] - p0[0], p1[1] - p0[1], p1[2] - p0[2])
            e02 = (p2[0] - p0[0], p2[1] - p0[1], p2[2] - p0[2])
            cross = (
                e01[1] * e02[2] - e01[2] * e02[1],
                e01[2] * e02[0] - e01[0] * e02[2],
                e01[0] * e02[1] - e01[1] * e02[0],
            )
            mag = (cross[0] ** 2 + cross[1] ** 2 + cross[2] ** 2) ** 0.5
            total += 0.5 * mag
        return total
    except Exception:
        return 0.0


def _node_type_name(node: Any) -> str:
    try:
        return node.type().name()
    except Exception:
        return ""


def _node_type_components(node: Any) -> str:
    try:
        return node.type().name().lower()
    except Exception:
        return ""


def _python_sop_code_line_count(node: Any) -> int:
    try:
        if node.type().name() != "python":
            return 0
        code = node.evalParm("python") or ""
        return code.count("\n") + 1
    except Exception:
        return 0


def _geometry_component_ids(node: Any) -> set[str]:
    ids: set[str] = set()
    try:
        geo = node.geometry()
        if geo is None:
            return ids
        attr = _find_prim_attrib(geo, "component_id")
        if attr is None:
            return ids
        for prim in geo.prims():
            v = _string_attrib(prim, "component_id")
            if v:
                ids.add(v)
    except Exception:
        pass
    return ids


def _find_prim_attrib(geo: Any, name: str) -> Any:
    try:
        return geo.findPrimAttrib(name)
    except Exception:
        return None


def _string_attrib(prim: Any, name: str) -> str:
    try:
        return str(prim.stringAttribValue(name))
    except Exception:
        return ""


def _read_axis_attrib(prim: Any, name: str) -> tuple[float, float, float] | None:
    """Read a 3-float axis attribute, tolerating floatList or list returns."""
    try:
        raw = prim.floatListAttribValue(name)
    except Exception:
        try:
            raw = prim.attribValue(name)
        except Exception:
            return None
    if raw is None:
        return None
    try:
        if len(raw) < 3:
            return None
        return (float(raw[0]), float(raw[1]), float(raw[2]))
    except (TypeError, ValueError):
        return None


__all__ = [
    "HEALTH_ADVISORY_CHECKS",
    "HEALTH_BLOCKING_CHECKS",
    "MODULAR_NODE_TYPES",
    "check_modular_structure",
    "inspect_geometry_health",
    "run_verify_gates",
    "verify_orientation",
    "verify_world_axes_baked",
]
