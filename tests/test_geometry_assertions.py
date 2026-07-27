"""Offline unit tests for eval/geometry_assertions P3 verifier helpers.

Covers the union-find component counter, per-part assertions, and the
deterministic color check — no Houdini required.
"""
from __future__ import annotations

from pathlib import Path

from eval.geometry_assertions import (
    evaluate_color,
    evaluate_parts,
    mesh_component_count,
)


def _write_obj(path: Path, verts, faces) -> Path:
    lines = [f"v {x} {y} {z}" for x, y, z in verts]
    lines += ["f " + " ".join(str(i) for i in face) for face in faces]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


_QUAD = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)]
_FAR_TRI = [(10, 0, 0), (11, 0, 0), (10, 1, 0)]


def test_component_count_connected_mesh_is_one(tmp_path: Path) -> None:
    # Two triangles sharing an edge -> one component.
    p = _write_obj(tmp_path / "one.obj", _QUAD, [(1, 2, 3), (1, 3, 4)])
    assert mesh_component_count(str(p)) == 1


def test_component_count_disjoint_meshes(tmp_path: Path) -> None:
    p = _write_obj(
        tmp_path / "two.obj", _QUAD + _FAR_TRI, [(1, 2, 3), (1, 3, 4), (5, 6, 7)]
    )
    assert mesh_component_count(str(p)) == 2


def test_component_count_missing_file_returns_none(tmp_path: Path) -> None:
    assert mesh_component_count(str(tmp_path / "nope.obj")) is None


def test_component_count_empty_file_returns_none(tmp_path: Path) -> None:
    p = tmp_path / "empty.obj"
    p.write_text("# nothing here\n", encoding="utf-8")
    assert mesh_component_count(str(p)) is None


def test_evaluate_parts_all_ok() -> None:
    parts = {
        "leg_fl": {"verts": 8, "faces": 6,
                   "bbox": {"min": [0, 0, 0], "max": [1, 2, 1], "size": [1, 2, 1]}},
    }
    expected = {"leg_fl": {"min_verts": 8, "bbox_max": [1.1, 2.1, 1.1]}}
    res = evaluate_parts(parts, expected)
    assert res["ok"] is True
    assert res["issues"] == []


def test_evaluate_parts_missing_required_part_is_locatable() -> None:
    res = evaluate_parts({}, {"leg_fl": {"min_verts": 8}})
    assert res["ok"] is False
    assert res["issues"] == ["leg_fl: missing"]


def test_evaluate_parts_size_out_of_bounds_prefixed_with_part() -> None:
    parts = {
        "leg_fl": {"verts": 8, "faces": 6,
                   "bbox": {"min": [0, 0, 0], "max": [1, 4, 1], "size": [1, 4, 1]}},
    }
    res = evaluate_parts(parts, {"leg_fl": {"bbox_max": [1.1, 2.1, 1.1]}})
    assert res["ok"] is False
    assert len(res["issues"]) == 1
    assert res["issues"][0].startswith("leg_fl: ")
    assert "y max" in res["issues"][0]


def test_evaluate_parts_optional_part_missing_is_not_an_issue() -> None:
    res = evaluate_parts({}, {"deco": {"required": False, "min_verts": 8}})
    assert res["ok"] is True
    assert res["issues"] == []


def test_evaluate_color_within_tolerance() -> None:
    attr = {"present": True, "mean": [0.21, 0.59, 0.2]}
    res = evaluate_color(attr, (0.2, 0.6, 0.2))
    assert res["ok"] is True
    assert res["issues"] == []


def test_evaluate_color_outside_tolerance_reports_channel() -> None:
    attr = {"present": True, "mean": [1.0, 0.6, 0.2]}
    res = evaluate_color(attr, (0.2, 0.6, 0.2))
    assert res["ok"] is False
    assert len(res["issues"]) == 1
    assert res["issues"][0].startswith("r mean")


def test_evaluate_color_custom_tolerance() -> None:
    attr = {"present": True, "mean": [0.3, 0.6, 0.2]}
    assert evaluate_color(attr, (0.2, 0.6, 0.2), tol=0.2)["ok"] is True
    assert evaluate_color(attr, (0.2, 0.6, 0.2), tol=0.05)["ok"] is False


def test_evaluate_color_missing_attr() -> None:
    assert evaluate_color(None, (0.2, 0.6, 0.2)) == {
        "ok": False, "issues": ["color attribute missing"],
    }
    assert evaluate_color({"present": False, "mean": None}, (1, 1, 1)) == {
        "ok": False, "issues": ["color attribute missing"],
    }
