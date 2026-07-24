"""Phase 2: sandbox verify gate tests.

Drives the four gates (bake / structure / orientation / health) in
``houdini_side.scratch_verify`` against hou-free fake geometry/prim/node
objects. No ``hou``, no live Houdini process — the gates read from fakes that
mimic the HOM surface the gates touch (``geometry()``, ``prims()``,
``points()``, ``vertices()``, ``findPrimAttrib``, ``stringAttribValue``,
``floatListAttribValue``, ``type().name()``, ``allSubChildren``).

Covers: each gate's pass path, each gate's hard-fail path, the advisory-vs-
blocking health distinction, the modular-structure heuristic, and the
orchestrator's pass/fail aggregation.
"""

from __future__ import annotations

import pytest

from houdini_side.scratch_verify import (
    HEALTH_ADVISORY_CHECKS,
    HEALTH_BLOCKING_CHECKS,
    MODULAR_NODE_TYPES,
    check_modular_structure,
    inspect_geometry_health,
    run_verify_gates,
    verify_orientation,
    verify_world_axes_baked,
)


# --------------------------------------------------------------------------
# fake HOM geometry objects
# --------------------------------------------------------------------------


class _Vec:
    def __init__(self, x: float, y: float, z: float) -> None:
        self._v = (x, y, z)

    def __getitem__(self, i: int) -> float:
        return self._v[i]


class _Point:
    def __init__(self, number: int, pos: tuple[float, float, float]) -> None:
        self._number = number
        self._pos = _Vec(*pos)

    def number(self) -> int:
        return self._number

    def position(self) -> _Vec:
        return self._pos


class _Vertex:
    def __init__(self, point: _Point) -> None:
        self._point = point

    def point(self) -> _Point:
        return self._point


class _PrimType:
    def __init__(self, name: str) -> None:
        self._name = name

    def name(self) -> str:
        return self._name


class _Prim:
    """A fake polygon primitive with verts + prim attributes."""

    def __init__(
        self,
        number: int,
        verts: list[_Point],
        *,
        type_name: str = "poly",
        component_id: str = "",
        world_axis: tuple[float, float, float] | None = None,
        area: float | None = None,
        is_closed: bool = True,
    ) -> None:
        self._number = number
        self._verts = [_Vertex(p) for p in verts]
        self._type = _PrimType(type_name)
        self._component_id = component_id
        self._world_axis = world_axis
        self._area = area
        self._is_closed = is_closed
        self._attrs: dict[str, object] = {}
        if component_id:
            self._attrs["component_id"] = component_id
        if world_axis is not None:
            self._attrs["edini_world_axis"] = list(world_axis)

    def number(self) -> int:
        return self._number

    def type(self) -> _PrimType:
        return self._type

    def vertices(self) -> list[_Vertex]:
        return self._verts

    def isClosed(self) -> bool:
        return self._is_closed

    def stringAttribValue(self, name: str) -> str:
        v = self._attrs.get(name)
        return str(v) if v is not None else ""

    def floatListAttribValue(self, name: str) -> list[float]:
        v = self._attrs.get(name)
        if v is None:
            raise AttributeError(name)
        return list(v)

    def attribValue(self, name: str) -> object:
        return self._attrs.get(name)

    def intrinsicValue(self, name: str) -> float:
        if name == "measuredarea":
            if self._area is None:
                raise AttributeError(name)
            return self._area
        raise AttributeError(name)


class _Attrib:
    def __init__(self, name: str) -> None:
        self._name = name


class _BBox:
    pass


class _Geometry:
    def __init__(
        self,
        points: list[_Point],
        prims: list[_Prim],
        *,
        attribs: set[str] | None = None,
    ) -> None:
        self._points = points
        self._prims = prims
        self._attribs = attribs if attribs is not None else set()

    def points(self) -> list[_Point]:
        return self._points

    def prims(self) -> list[_Prim]:
        return self._prims

    def pointCount(self) -> int:
        return len(self._points)

    def primCount(self) -> int:
        return len(self._prims)

    def intrinsicValue(self, name: str) -> int:
        if name == "vertexcount":
            return sum(len(p.vertices()) for p in self._prims)
        raise AttributeError(name)

    def boundingBox(self) -> _BBox:
        return _BBox()

    def findPrimAttrib(self, name: str) -> _Attrib | None:
        return _Attrib(name) if name in self._attribs else None


class _NodeType:
    def __init__(self, name: str) -> None:
        self._name = name

    def name(self) -> str:
        return self._name


class _Node:
    """A fake network node for the structure gate."""

    def __init__(
        self,
        path: str,
        type_name: str,
        *,
        children: list["_Node"] | None = None,
        code: str = "",
    ) -> None:
        self._path = path
        self._type = _NodeType(type_name)
        self._children = children or []
        self._code = code
        self._geo: _Geometry | None = None

    def path(self) -> str:
        return self._path

    def type(self) -> _NodeType:
        return self._type

    def allSubChildren(self) -> list["_Node"]:
        out: list[_Node] = []
        for c in self._children:
            out.append(c)
            out.extend(c.allSubChildren())
        return out

    def evalParm(self, name: str) -> str:
        if name == "python":
            return self._code
        return ""

    def set_geometry(self, geo: _Geometry) -> None:
        self._geo = geo

    def geometry(self) -> _Geometry | None:
        return self._geo


class _OutputNode:
    """A fake output node (has geometry but is not a network container)."""

    def __init__(self, geo: _Geometry | None) -> None:
        self._geo = geo

    def geometry(self) -> _Geometry | None:
        return self._geo


# --------------------------------------------------------------------------
# geometry builders for common test shapes
# --------------------------------------------------------------------------


def _quad_points(cx: float = 0.0, size: float = 1.0) -> list[_Point]:
    """Four points forming a unit quad (non-degenerate)."""
    h = size / 2.0
    return [
        _Point(0, (cx - h, -h, 0.0)),
        _Point(1, (cx + h, -h, 0.0)),
        _Point(2, (cx + h, h, 0.0)),
        _Point(3, (cx - h, h, 0.0)),
    ]


def _quad_prim(number: int, pts: list[_Point], *, area: float = 1.0, **kwargs) -> _Prim:
    return _Prim(number, pts, type_name="poly", area=area, **kwargs)


# ==========================================================================
# Bake gate
# ==========================================================================


class TestBakeGate:
    def test_no_component_id_passes_vacuously(self) -> None:
        pts = _quad_points()
        prim = _quad_prim(0, pts)
        geo = _Geometry(pts, [prim], attribs=set())
        result = verify_world_axes_baked(_OutputNode(geo))
        assert result["passed"] is True
        assert result["hard"] is True
        assert result["detail"]["skipped"]

    def test_all_components_baked_passes(self) -> None:
        pts = _quad_points()
        prim = _quad_prim(0, pts, component_id="top", world_axis=(0.0, 1.0, 0.0))
        geo = _Geometry(pts, [prim], attribs={"component_id", "edini_world_axis"})
        result = verify_world_axes_baked(_OutputNode(geo))
        assert result["passed"] is True

    def test_zero_axis_fails(self) -> None:
        pts = _quad_points()
        prim = _quad_prim(0, pts, component_id="top", world_axis=(0.0, 0.0, 0.0))
        geo = _Geometry(pts, [prim], attribs={"component_id", "edini_world_axis"})
        result = verify_world_axes_baked(_OutputNode(geo))
        assert result["passed"] is False
        assert result["hard"] is True
        assert "top" in result["detail"]["missing_components"]

    def test_missing_axis_attr_fails(self) -> None:
        pts = _quad_points()
        # component_id present but no edini_world_axis attribute
        prim = _quad_prim(0, pts, component_id="top")
        geo = _Geometry(pts, [prim], attribs={"component_id"})
        result = verify_world_axes_baked(_OutputNode(geo))
        assert result["passed"] is False
        assert "top" in result["detail"]["missing_components"]

    def test_none_geometry_fails(self) -> None:
        result = verify_world_axes_baked(_OutputNode(None))
        assert result["passed"] is False
        assert "None" in result["reason"]


# ==========================================================================
# Structure gate
# ==========================================================================


class TestStructureGate:
    def test_simple_single_component_passes(self) -> None:
        # One box SOP, one component → not monolithic.
        box = _Node("/obj/sb/box1", "box")
        box.set_geometry(_Geometry([], [], attribs={"component_id"}))
        root = _Node("/obj/eee_scratch_run1", "geo", children=[box])
        result = check_modular_structure(root)
        assert result["passed"] is True

    def test_modular_multi_component_passes(self) -> None:
        # 3 components but assembled via copytopoints → modular.
        gen = _Node("/obj/sb/gen", "python")
        ctp = _Node("/obj/sb/copytopoints1", "copytopoints::2.0")
        result = check_modular_structure(
            _Node("/obj/sb", "geo", children=[gen, ctp])
        )
        assert result["passed"] is True

    def test_monolithic_single_python_fails(self) -> None:
        # 3 components, all from one big python SOP, no modular nodes.
        gen = _Node("/obj/sb/gen", "python")
        gen.set_geometry(_Geometry(
            [],
            [
                _Prim(0, [], component_id="wheel"),
                _Prim(1, [], component_id="saddle"),
                _Prim(2, [], component_id="frame"),
            ],
            attribs={"component_id"},
        ))
        root = _Node("/obj/sb", "geo", children=[gen])
        result = check_modular_structure(root)
        assert result["passed"] is False
        assert result["hard"] is True
        assert "Monolithic" in result["reason"]

    def test_no_children_passes(self) -> None:
        root = _Node("/obj/sb", "geo", children=[])
        result = check_modular_structure(root)
        assert result["passed"] is True


# ==========================================================================
# Orientation gate
# ==========================================================================


class TestOrientationGate:
    def test_no_checks_passes_vacuously(self) -> None:
        geo = _Geometry([], [], attribs={"component_id"})
        result = verify_orientation(_OutputNode(geo), [])
        assert result["passed"] is True
        assert result["detail"]["skipped"]

    def test_no_component_id_passes_vacuously(self) -> None:
        geo = _Geometry([], [], attribs=set())
        result = verify_orientation(_OutputNode(geo), [{"component_id": "x"}])
        assert result["passed"] is True
        assert result["detail"]["skipped"]

    def test_correct_axis_passes(self) -> None:
        pts = _quad_points()
        prim = _quad_prim(
            0, pts, component_id="wheel", world_axis=(0.0, 1.0, 0.0)
        )
        geo = _Geometry(pts, [prim], attribs={"component_id", "edini_world_axis"})
        checks = [{"component_id": "wheel", "kind": "radial", "expected_axis": "Y"}]
        result = verify_orientation(_OutputNode(geo), checks)
        assert result["passed"] is True
        assert result["detail"]["passed"] == 1
        assert result["detail"]["failed"] == 0

    def test_wrong_axis_fails(self) -> None:
        pts = _quad_points()
        # baked axis is X but we expect Y → fail.
        prim = _quad_prim(
            0, pts, component_id="wheel", world_axis=(1.0, 0.0, 0.0)
        )
        geo = _Geometry(pts, [prim], attribs={"component_id", "edini_world_axis"})
        checks = [{"component_id": "wheel", "kind": "radial", "expected_axis": "Y"}]
        result = verify_orientation(_OutputNode(geo), checks)
        assert result["passed"] is False
        assert result["detail"]["failed"] == 1

    def test_unsigned_opposite_axis_passes(self) -> None:
        pts = _quad_points()
        # baked -Y, expect Y, unsigned → treated as same line.
        prim = _quad_prim(
            0, pts, component_id="wheel", world_axis=(0.0, -1.0, 0.0)
        )
        geo = _Geometry(pts, [prim], attribs={"component_id", "edini_world_axis"})
        checks = [{"component_id": "wheel", "kind": "radial", "expected_axis": "Y", "signed": False}]
        result = verify_orientation(_OutputNode(geo), checks)
        assert result["passed"] is True

    def test_missing_axis_fails(self) -> None:
        pts = _quad_points()
        prim = _quad_prim(0, pts, component_id="wheel")
        geo = _Geometry(pts, [prim], attribs={"component_id"})
        checks = [{"component_id": "wheel", "kind": "radial", "expected_axis": "Y"}]
        result = verify_orientation(_OutputNode(geo), checks)
        assert result["passed"] is False

    def test_override_axis_supersedes_baked(self) -> None:
        pts = _quad_points()
        # baked axis is X, but override says Z, expect Z → pass.
        prim = _quad_prim(
            0, pts, component_id="wheel", world_axis=(1.0, 0.0, 0.0)
        )
        geo = _Geometry(pts, [prim], attribs={"component_id", "edini_world_axis"})
        checks = [{"component_id": "wheel", "kind": "radial",
                   "expected_axis": "Z", "construction_axis": "Z"}]
        result = verify_orientation(_OutputNode(geo), checks)
        assert result["passed"] is True

    def test_unknown_kind_fails(self) -> None:
        pts = _quad_points()
        prim = _quad_prim(
            0, pts, component_id="wheel", world_axis=(0.0, 1.0, 0.0)
        )
        geo = _Geometry(pts, [prim], attribs={"component_id", "edini_world_axis"})
        checks = [{"component_id": "wheel", "kind": "bogus", "expected_axis": "Y"}]
        result = verify_orientation(_OutputNode(geo), checks)
        assert result["passed"] is False


# ==========================================================================
# Health gate
# ==========================================================================


class TestHealthGate:
    def test_clean_geometry_passes(self) -> None:
        pts = _quad_points()
        prim = _quad_prim(0, pts, area=1.0)
        geo = _Geometry(pts, [prim])
        result = inspect_geometry_health(_OutputNode(geo))
        assert result["passed"] is True
        assert result["detail"]["hard_errors_count"] == 0

    def test_orphan_points_hard_fails(self) -> None:
        pts = _quad_points() + [_Point(4, (10.0, 10.0, 10.0))]  # unreferenced
        prim = _quad_prim(0, pts[:4], area=1.0)
        geo = _Geometry(pts, [prim])
        result = inspect_geometry_health(_OutputNode(geo))
        assert result["passed"] is False
        assert result["hard"] is True
        assert result["detail"]["checks"]["orphan_points"]["count"] == 1
        assert result["detail"]["checks"]["orphan_points"]["severity"] == "blocking"

    def test_open_curve_hard_fails(self) -> None:
        pts = [_Point(0, (0, 0, 0)), _Point(1, (1, 0, 0))]
        curve = _Prim(0, pts, type_name="curve", is_closed=False)
        geo = _Geometry(pts, [curve])
        result = inspect_geometry_health(_OutputNode(geo))
        assert result["passed"] is False
        assert result["detail"]["checks"]["open_curves"]["count"] == 1
        assert result["detail"]["checks"]["open_curves"]["severity"] == "blocking"

    def test_degenerate_prim_advisory_does_not_block(self) -> None:
        pts = _quad_points()
        # area ~0 → degenerate (advisory, not blocking)
        prim = _quad_prim(0, pts, area=0.0)
        geo = _Geometry(pts, [prim])
        result = inspect_geometry_health(_OutputNode(geo))
        # degenerate is advisory → passed stays True
        assert result["passed"] is True
        assert result["detail"]["checks"]["degenerate_prims"]["count"] == 1
        assert result["detail"]["checks"]["degenerate_prims"]["severity"] == "advisory"

    def test_nonmanifold_advisory_does_not_block(self) -> None:
        # 3 polygons sharing one edge → nonmanifold (valence 3).
        p0 = _Point(0, (0, 0, 0))
        p1 = _Point(1, (1, 0, 0))
        p2 = _Point(2, (0, 1, 0))
        p3 = _Point(3, (0, -1, 0))
        pts = [p0, p1, p2, p3]
        # Each quad uses edge p0-p1; three of them → valence 3 on that edge.
        prim_a = _quad_prim(0, [p0, p1, p2, _Point(4, (1, 1, 0))], area=1.0)
        prim_b = _quad_prim(1, [p0, p1, p3, _Point(5, (1, -1, 0))], area=1.0)
        prim_c = _quad_prim(2, [p0, p1, _Point(6, (0.5, 0, 1)), _Point(7, (0.5, 0, -1))], area=1.0)
        all_pts = pts + [_Point(4, (1, 1, 0)), _Point(5, (1, -1, 0)),
                         _Point(6, (0.5, 0, 1)), _Point(7, (0.5, 0, -1))]
        geo = _Geometry(all_pts, [prim_a, prim_b, prim_c])
        result = inspect_geometry_health(_OutputNode(geo))
        assert result["passed"] is True  # nonmanifold is advisory
        assert result["detail"]["checks"]["nonmanifold_edges"]["count"] >= 1

    def test_none_geometry_fails(self) -> None:
        result = inspect_geometry_health(_OutputNode(None))
        assert result["passed"] is False


# ==========================================================================
# Orchestrator
# ==========================================================================


class TestRunVerifyGates:
    def test_all_pass_when_geometry_clean_and_simple(self) -> None:
        pts = _quad_points()
        prim = _quad_prim(0, pts, area=1.0)
        geo = _Geometry(pts, [prim])
        sandbox_root = _Node("/obj/eee_scratch_run1", "geo", children=[])
        result = run_verify_gates(sandbox_root, _OutputNode(geo))
        assert result["passed"] is True
        assert len(result["hard_failures"]) == 0
        assert [g["gate"] for g in result["gates"]] == [
            "bake", "structure", "orientation", "health"
        ]

    def test_hard_health_failure_blocks_overall(self) -> None:
        pts = _quad_points() + [_Point(4, (10, 10, 10))]  # orphan
        prim = _quad_prim(0, pts[:4], area=1.0)
        geo = _Geometry(pts, [prim])
        sandbox_root = _Node("/obj/eee_scratch_run1", "geo", children=[])
        result = run_verify_gates(sandbox_root, _OutputNode(geo))
        assert result["passed"] is False
        gate_names = {g["gate"] for g in result["hard_failures"]}
        assert "health" in gate_names

    def test_bake_failure_blocks_overall(self) -> None:
        pts = _quad_points()
        prim = _quad_prim(0, pts, component_id="top", world_axis=(0, 0, 0))
        geo = _Geometry(pts, [prim], attribs={"component_id", "edini_world_axis"})
        sandbox_root = _Node("/obj/eee_scratch_run1", "geo", children=[])
        result = run_verify_gates(sandbox_root, _OutputNode(geo))
        assert result["passed"] is False
        gate_names = {g["gate"] for g in result["hard_failures"]}
        assert "bake" in gate_names


# ==========================================================================
# Constants sanity
# ==========================================================================


class TestConstants:
    def test_blocking_checks_are_orphan_and_curves(self) -> None:
        assert set(HEALTH_BLOCKING_CHECKS) == {"orphan_points", "open_curves"}

    def test_advisory_checks_exclude_blocking(self) -> None:
        assert not (set(HEALTH_ADVISORY_CHECKS) & set(HEALTH_BLOCKING_CHECKS))

    def test_modular_types_includes_assembly_sops(self) -> None:
        for expected in ("copytopoints", "sweep", "boolean", "polyextrude", "foreach"):
            assert any(expected in t or t.startswith(expected + "::") for t in MODULAR_NODE_TYPES)
