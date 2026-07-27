from __future__ import annotations

from eee_agent.modeling.catalog import (
    houdini_21_minimal_catalog,
    houdini_21_minimal_quality_profile,
)


def test_houdini_21_minimal_catalog_contains_only_verified_safe_types() -> None:
    catalog = houdini_21_minimal_catalog()
    by_type = catalog.by_type
    assert set(by_type) == {
        "boolean2", "box", "copytopoints2", "copyxform", "fuse2", "geo", "grid",
        "line", "merge", "normal", "null", "polyextrude2", "resample", "sphere",
        "subdivide", "sweep2", "torus", "xform"
    }
    assert "transform" not in by_type
    assert by_type["geo"].can_parent_nodes is True
    assert by_type["box"].parameters_by_name["sizex"].default_value == 1.0
    assert by_type["grid"].parameters_by_name["rows"].default_value == 10
    assert by_type["merge"].max_inputs == 64
    assert by_type["xform"].parameters_by_name["sx"].default_value == 1.0
    assert by_type["null"].parameters_by_name["copyinput"].default_value == 1
    assert by_type["normal"].parameters_by_name["cuspangle"].default_value == 60.0
    assert by_type["subdivide"].parameters_by_name["iterations"].default_value == 1
    assert by_type["polyextrude2"].create_type == "polyextrude::2.0"
    assert by_type["fuse2"].create_type == "fuse::2.0"
    assert by_type["copytopoints2"].create_type == "copytopoints::2.0"
    assert by_type["sweep2"].create_type == "sweep::2.0"
    assert by_type["boolean2"].create_type == "boolean::2.0"
    # Entries probe-verified against live Houdini 21.0.440 on 2026-07-24.
    assert by_type["torus"].parameters_by_name["orient"].default_value == 1
    assert by_type["torus"].parameters_by_name["rady"].default_value == 0.5
    assert by_type["sphere"].parameters_by_name["rows"].default_value == 13
    assert by_type["copyxform"].parameters_by_name["ncy"].default_value == 2
    assert by_type["sweep2"].parameters_by_name["surfaceshape"].default_value == 0
    assert by_type["sweep2"].parameters_by_name["radius"].default_value == 0.1
    assert by_type["sweep2"].parameters_by_name["endcaptype"].default_value == 0


def test_houdini_21_minimal_quality_profile_is_deterministic() -> None:
    profile = houdini_21_minimal_quality_profile()
    assert profile.profile_id == "houdini_21_minimal_v1"
    assert profile.max_repairs_per_stage == 2
    assert profile.allow_vex_source is False
    assert profile == houdini_21_minimal_quality_profile()
