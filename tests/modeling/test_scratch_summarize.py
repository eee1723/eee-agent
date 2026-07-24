"""Tests for scratch_build result summarization (M2 fix).

M2: a scratch_build that applied some ops but produced per-op cook errors used
to report ok=True, masking a partial failure. The summary now reports ok=False
when errors is non-empty so the model does not proceed on a half-applied graph.
"""
from __future__ import annotations

from eee_agent.houdini_bridge.scratch import ScratchGeometry, ScratchResult
from eee_agent.modeling.scratch_coordinator import ScratchCoordinator

_GEO = ScratchGeometry(
    point_count=8, prim_count=6, vertex_count=24,
    bbox_min=(0.0, 0.0, 0.0), bbox_max=(2.0, 2.0, 2.0),
)


def _result(*, errors=()):
    return ScratchResult(
        sandbox_root="/obj/box1",
        applied_ops=3,
        output_node="/obj/box1/out",
        errors=tuple(errors),
        geometry=_GEO,
    )


def test_clean_build_reports_ok_true():
    summary = ScratchCoordinator._summarize(_result(errors=()))
    assert summary["ok"] is True
    assert summary["errors"] == []


def test_partial_failure_reports_ok_false():
    """M2: per-op errors must flip ok to False."""
    summary = ScratchCoordinator._summarize(
        _result(errors=["set_parm failed: parm 'foo' not found"])
    )
    assert summary["ok"] is False
    assert summary["errors"] == ["set_parm failed: parm 'foo' not found"]
    # The recovery evidence is still present.
    assert summary["applied_ops"] == 3


def test_multiple_errors_reported_ok_false():
    summary = ScratchCoordinator._summarize(
        _result(errors=["err one", "err two"])
    )
    assert summary["ok"] is False
    assert len(summary["errors"]) == 2
