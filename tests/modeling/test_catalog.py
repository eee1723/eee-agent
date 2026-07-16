from __future__ import annotations

from eee_agent.modeling.catalog import (
    houdini_21_minimal_catalog,
    houdini_21_minimal_quality_profile,
)


def test_houdini_21_minimal_catalog_contains_only_verified_safe_types() -> None:
    catalog = houdini_21_minimal_catalog()
    by_type = catalog.by_type
    assert set(by_type) == {"box", "grid", "merge", "null", "xform"}
    assert "transform" not in by_type
    assert by_type["box"].parameters_by_name["sizex"].default_value == 1.0
    assert by_type["grid"].parameters_by_name["rows"].default_value == 10
    assert by_type["merge"].max_inputs == 64
    assert by_type["xform"].parameters_by_name["sx"].default_value == 1.0
    assert by_type["null"].parameters_by_name["copyinput"].default_value == 1


def test_houdini_21_minimal_quality_profile_is_deterministic() -> None:
    profile = houdini_21_minimal_quality_profile()
    assert profile.profile_id == "houdini_21_minimal_v1"
    assert profile.max_repairs_per_stage == 2
    assert profile.allow_vex_source is False
    assert profile == houdini_21_minimal_quality_profile()
