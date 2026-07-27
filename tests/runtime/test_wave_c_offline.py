from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from eee_agent.houdini_bridge.scratch import ScratchCommitRequest, ScratchParmDeclaration
from eval.run_eval import build_report, load_cases
from tests.runtime.param_scan_houdini_smoke import scaled_envelope


def _parm(**kwargs):
    return ScratchParmDeclaration(
        binding=(("node", "wheel/wheel_ctrl"), ("parm", "sizex")),
        default=1.0, min=0.5, max=1.5, **kwargs,
    )


def test_manifest_fail_closed_and_round_trips() -> None:
    item = _parm(name="wheel_radius", tab="Wheel", unit="m")
    request = ScratchCommitRequest.build(
        request_id="req1", deadline_ms=1000, scene_epoch=1, sandbox_id="run1",
        target_parent_path="/obj", target_name="bike", parameters=[item],
    )
    assert ScratchCommitRequest.from_dict(request.to_dict()).parameters == (item,)
    with pytest.raises(ValueError):
        _parm(name="bad-name")
    with pytest.raises(ValueError):
        ScratchParmDeclaration(
            name="derived", classification="derived",
            binding=(("node", "wheel/wheel_ctrl"), ("parm", "sizex")),
            default=1.0,
        )


def test_nested_refs_and_scan_envelope() -> None:
    with pytest.raises(ValueError):
        ScratchCommitRequest.build(
            request_id="req1", deadline_ms=1000, scene_epoch=1, sandbox_id="run1",
            target_parent_path="/obj", target_name="bike",
            annotations={"../escape": "bad"},
        )
    lower, upper = scaled_envelope([1.0, 2.0, 3.0], 0.5, 1.0, 1.5)
    assert lower == [0.5, 1.0, 1.5]
    assert upper == [1.5, 3.0, 4.5]


def test_case_library_has_five_tagged_cases() -> None:
    cases = load_cases()
    names = {case["name"] for case in cases}
    assert {"chair", "desk", "shelf", "parametric hut"} <= names
    assert any("parametric" in case.get("tags", []) for case in cases)
    report = build_report(
        [{"name": "chair", "ok": True, "stats": {"verts": 8}, "issues": []}],
        cases,
    )
    assert report["metrics"]["translation_success_rate"] == 1.0
    assert report["metrics"]["avg_repair_rounds"] is None
    assert "not_run" in " ".join(report["notes"])
