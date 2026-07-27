from __future__ import annotations

import asyncio
from copy import deepcopy

from eee_agent.runtime.modeling_brief_tools import prepare_modeling_brief


def _brief() -> dict[str, object]:
    return {
        "schema_version": 1,
        "brief_key": "road_bicycle",
        "title": "Procedural road bicycle",
        "asset_family": "bicycle",
        "original_request": "做一个程序化自行车",
        "units": "m",
        "up_axis": "+Y",
        "forward_axis": "+X",
        "detail_level": "standard",
        "components": [
            {
                "name": "wheels",
                "count": 2,
                "required": True,
                "details": ["rims", "hubs", "spokes"],
            },
            {
                "name": "cockpit",
                "count": 1,
                "required": True,
                "details": ["handlebar", "stem", "grips"],
            },
        ],
        "dimensions": {
            "wheel_radius": 0.34,
            "wheelbase": 1.1,
            "handlebar_width": 0.44,
        },
        "orientation_rules": [
            "Wheel planes are XY and wheel axles are parallel to Z.",
            "The handlebar is parallel to Z.",
        ],
        "constraints": [
            "Paired parts mirror across Z=0.",
            "Frame tubes are constructed from named endpoints.",
        ],
        "acceptance": [
            "The front wheel center is ahead of the rear wheel center.",
            "Both wheel centers share Y and Z coordinates.",
        ],
        "views": ["three_quarter", "side", "front"],
        "assumptions": ["Use realistic road-bicycle proportions."],
        "clarification_questions": [],
    }


def _run(brief: dict[str, object]) -> dict[str, object]:
    return asyncio.run(prepare_modeling_brief.coroutine(brief))


def test_ready_brief_is_canonical_and_digest_stable() -> None:
    first = _run(_brief())
    second_brief = _brief()
    second_brief["dimensions"] = {
        "handlebar_width": 0.44,
        "wheelbase": 1.1,
        "wheel_radius": 0.34,
    }
    second = _run(second_brief)

    assert first["ok"] is True
    assert first["ready"] is True
    assert first["brief_digest"] == second["brief_digest"]
    assert first["brief"]["coordinate_frame"] == {
        "up_axis": "+Y",
        "forward_axis": "+X",
        "left_axis": "+Z",
    }


def test_at_most_three_questions_are_returned_as_one_not_ready_batch() -> None:
    brief = _brief()
    brief["components"] = []
    brief["orientation_rules"] = []
    brief["constraints"] = []
    brief["acceptance"] = []
    brief["views"] = []
    brief["clarification_questions"] = [
        "需要完整传动系统还是简化链轮？",
        "细节等级选择 standard 还是 high？",
        "是否包含刹车和线缆？",
    ]

    result = _run(brief)

    assert result["ok"] is True
    assert result["ready"] is False
    assert result["question_count"] == 3
    assert len(result["questions"]) == 3
    assert "brief_digest" not in result


def test_more_than_three_questions_are_rejected() -> None:
    brief = _brief()
    brief["clarification_questions"] = ["q1", "q2", "q3", "q4"]

    result = _run(brief)

    assert result["ok"] is False
    assert result["code"] == "workflow.modeling_brief_invalid"


def test_parallel_up_and_forward_axes_are_rejected() -> None:
    brief = _brief()
    brief["forward_axis"] = "-Y"

    result = _run(brief)

    assert result["ok"] is False
    assert "perpendicular" in result["message"]


def test_ready_brief_requires_components_rules_constraints_acceptance_and_views() -> None:
    for field in (
        "components",
        "orientation_rules",
        "constraints",
        "acceptance",
        "views",
    ):
        brief = deepcopy(_brief())
        brief[field] = []
        result = _run(brief)
        assert result["ok"] is False, field
        assert field in result["message"], field


def test_duplicate_component_names_are_rejected() -> None:
    brief = _brief()
    brief["components"] = [
        {
            "name": "wheel",
            "count": 1,
            "required": True,
            "details": [],
        },
        {
            "name": "wheel",
            "count": 1,
            "required": True,
            "details": [],
        },
    ]

    result = _run(brief)

    assert result["ok"] is False
    assert "duplicate name" in result["message"]
