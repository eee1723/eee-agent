"""Task 19-A: deterministic capture framing unit tests (pure, offline).

Covers the pure bbox -> framing evaluation and the at-most-two-adjustments
decision logic: acceptance band (margins, longest-axis ratio, center offset),
recenter/zoom adjustments, adjustment budget exhaustion, degenerate bboxes,
union boxes, DTO strictness, and bit-determinism (same inputs, same plan).
"""

from __future__ import annotations

import pytest

from eee_agent.modeling.framing import (
    BoundingBox,
    CameraFraming,
    FramingError,
    FramingTolerance,
    camera_basis,
    compute_framing,
)


def _box(
    mn: tuple[float, float, float], mx: tuple[float, float, float]
) -> BoundingBox:
    return BoundingBox(mn[0], mn[1], mn[2], mx[0], mx[1], mx[2])


# --------------------------------------------------------------------------
# acceptance band
# --------------------------------------------------------------------------


def test_simple_box_frames_within_band() -> None:
    plan = compute_framing(_box((0, 0, 0), (2, 1, 1)), frame_width=640, frame_height=480)
    evaluation = plan.evaluation
    assert evaluation.accepted is True
    assert evaluation.margin_left >= 0.06
    assert evaluation.margin_right >= 0.06
    assert evaluation.margin_bottom >= 0.06
    assert evaluation.margin_top >= 0.06
    assert 0.72 <= evaluation.longest_axis_ratio <= 0.84
    assert evaluation.center_offset <= 0.03
    assert 0 <= plan.adjustments_used <= 2


def test_centered_unit_box_needs_at_most_one_adjustment() -> None:
    plan = compute_framing(
        _box((-0.5, -0.5, -0.5), (0.5, 0.5, 0.5)),
        frame_width=640,
        frame_height=480,
    )
    # The initial mid-band solve lands inside the band; at most a recenter.
    assert plan.adjustments_used <= 1
    assert plan.evaluation.center_offset <= 0.03


def test_far_offset_geometry_frames_within_band() -> None:
    plan = compute_framing(_box((10, 20, 30), (12, 22, 33)), frame_width=640, frame_height=480)
    assert plan.evaluation.accepted is True
    assert plan.evaluation.center_offset <= 0.03


def test_extreme_aspect_ratios_frame_within_band() -> None:
    for mn, mx in (
        ((0.0, 0.0, 0.0), (50.0, 0.5, 0.5)),
        ((0.0, 0.0, 0.0), (0.2, 10.0, 0.2)),
        ((0.0, 0.0, 0.0), (0.01, 0.01, 8.0)),
        ((-3.0, -0.001, -3.0), (3.0, 0.001, 3.0)),
    ):
        plan = compute_framing(_box(mn, mx), frame_width=640, frame_height=480)
        assert plan.evaluation.accepted is True
        assert 0.72 <= plan.evaluation.longest_axis_ratio <= 0.84


def test_tiny_and_huge_geometry_frames_within_band() -> None:
    for mn, mx in (
        ((0.0, 0.0, 0.0), (1e-3, 1e-3, 1e-3)),
        ((-1e4, -1e4, -1e4), (1e4, 1e4, 1e4)),
    ):
        plan = compute_framing(_box(mn, mx), frame_width=640, frame_height=480)
        assert plan.evaluation.accepted is True


def test_preflight_and_output_aspect_match_gives_same_report() -> None:
    box = _box((0, 0, 0), (2, 1, 1))
    preflight = compute_framing(box, frame_width=640, frame_height=480)
    full = compute_framing(box, frame_width=1280, frame_height=960)
    assert preflight.evaluation == full.evaluation
    assert preflight.camera == full.camera


def test_determinism_bit_identical_repeated_runs() -> None:
    box = _box((1.25, -3.5, 0.75), (4.5, 2.0, 9.25))
    first = compute_framing(box, frame_width=640, frame_height=480)
    second = compute_framing(box, frame_width=640, frame_height=480)
    assert first == second
    assert first.camera.position == second.camera.position


# --------------------------------------------------------------------------
# adjustments
# --------------------------------------------------------------------------


def test_off_axis_box_uses_recenter_adjustment() -> None:
    # A box far from the view-ray center projects off-center before any
    # adjustment; the accepted plan recenters it within the budget.
    plan = compute_framing(_box((0, 0, 0), (2, 1, 1)), frame_width=640, frame_height=480)
    assert plan.adjustments_used >= 1
    assert plan.evaluation.center_offset <= 0.03


def test_adjustment_budget_exhaustion_fails_closed() -> None:
    # The off-axis box needs a recenter; a zero-adjustment budget must raise
    # instead of returning an unaccepted plan.
    with pytest.raises(FramingError):
        compute_framing(
            _box((0, 0, 0), (2, 1, 1)),
            frame_width=640,
            frame_height=480,
            max_adjustments=0,
        )


def test_impossible_tolerance_exhausts_budget() -> None:
    # A tolerance band that no placement can satisfy (margins wider than the
    # longest-axis band allows) must exhaust the budget, never coerce a pass.
    tolerance = FramingTolerance(
        margin_min=0.20,
        longest_axis_min=0.80,
        longest_axis_max=0.90,
        center_offset_max=0.24,
    )
    with pytest.raises(FramingError):
        compute_framing(
            _box((0, 0, 0), (2, 1, 1)),
            frame_width=640,
            frame_height=480,
            tolerance=tolerance,
        )


def test_single_adjustment_budget_fits_recenter() -> None:
    # The off-axis box always needs exactly one recenter under the canonical
    # settings (perspective projection skews the initial rect off-center), so
    # a one-adjustment budget succeeds while a zero budget fails closed.
    box = _box((0, 0, 0), (2, 1, 1))
    plan = compute_framing(box, frame_width=640, frame_height=480, max_adjustments=1)
    assert plan.adjustments_used == 1
    assert plan.evaluation.accepted is True


# --------------------------------------------------------------------------
# degenerate input + DTO strictness
# --------------------------------------------------------------------------


def test_degenerate_bbox_raises() -> None:
    with pytest.raises(FramingError):
        compute_framing(_box((1, 1, 1), (1, 1, 1)), frame_width=640, frame_height=480)


def test_bbox_validation_is_strict() -> None:
    with pytest.raises(ValueError):
        BoundingBox(0, 0, 0, -1, 1, 1)  # min > max
    with pytest.raises(ValueError):
        BoundingBox(0, 0, 0, float("nan"), 1, 1)
    with pytest.raises(TypeError):
        BoundingBox(0, 0, 0, True, 1, 1)  # type: ignore[arg-type]
    box = BoundingBox(0, 0, 0, 2, 1, 1)
    assert box.center == (1.0, 0.5, 0.5)
    assert box.size == (2.0, 1.0, 1.0)
    assert len(box.corners) == 8


def test_union_combines_boxes_in_order() -> None:
    union = BoundingBox.union(
        [_box((0, 0, 0), (1, 1, 1)), _box((-2, 0.5, 3), (0, 2, 4))]
    )
    assert (union.min_x, union.min_y, union.min_z) == (-2.0, 0.0, 0.0)
    assert (union.max_x, union.max_y, union.max_z) == (1.0, 2.0, 4.0)
    with pytest.raises(ValueError):
        BoundingBox.union([])
    with pytest.raises(TypeError):
        BoundingBox.union([_box((0, 0, 0), (1, 1, 1)), "not-a-box"])  # type: ignore[list-item]


def test_union_of_disjoint_boxes_frames_within_band() -> None:
    union = BoundingBox.union(
        [_box((0, 0, 0), (1, 1, 1)), _box((4, 1, 2), (5, 2, 3))]
    )
    plan = compute_framing(union, frame_width=640, frame_height=480)
    assert plan.evaluation.accepted is True


def test_frame_and_tolerance_validation_is_strict() -> None:
    with pytest.raises(ValueError):
        compute_framing(_box((0, 0, 0), (1, 1, 1)), frame_width=8, frame_height=480)
    with pytest.raises(ValueError):
        compute_framing(
            _box((0, 0, 0), (1, 1, 1)),
            frame_width=640,
            frame_height=480,
            max_adjustments=3,
        )
    with pytest.raises(TypeError):
        compute_framing("not-a-box", frame_width=640, frame_height=480)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        FramingTolerance(margin_min=0.5)
    with pytest.raises(ValueError):
        FramingTolerance(longest_axis_min=0.4)
    with pytest.raises(ValueError):
        FramingTolerance(longest_axis_min=0.72, longest_axis_max=1.0)
    with pytest.raises(ValueError):
        FramingTolerance(center_offset_max=0.0)


def test_camera_basis_is_orthonormal_and_forward_looking() -> None:
    right, up, forward = camera_basis((4.0, 3.0, 5.0), (1.0, 0.5, 0.5))
    for vec in (right, up, forward):
        assert abs(sum(c * c for c in vec) - 1.0) < 1e-9
    dot = sum(a * b for a, b in zip(right, forward))
    assert abs(dot) < 1e-9
    # forward points from position toward look_at.
    direction = tuple(
        b - a for a, b in zip((4.0, 3.0, 5.0), (1.0, 0.5, 0.5))
    )
    norm = sum(c * c for c in direction) ** 0.5
    for a, b in zip(forward, direction):
        assert abs(a - b / norm) < 1e-9


def test_camera_placement_stays_outside_geometry() -> None:
    box = _box((0, 0, 0), (2, 1, 1))
    plan = compute_framing(box, frame_width=640, frame_height=480)
    distance = sum(
        (a - b) ** 2 for a, b in zip(plan.camera.position, box.center)
    ) ** 0.5
    assert distance > box.bounding_radius
    assert plan.camera.focal_length_mm == 50.0
    assert plan.camera.sensor_width_mm == 36.0


def test_camera_framing_dto_is_strict() -> None:
    with pytest.raises(ValueError):
        CameraFraming(position=(0, 0, 0), look_at=(0, 0, 0))
    with pytest.raises(ValueError):
        CameraFraming(position=(0, 0, 0), look_at=(0, 0, 1), focal_length_mm=-1)
    with pytest.raises(TypeError):
        CameraFraming(position=(0, 0, "x"), look_at=(0, 0, 1))  # type: ignore[arg-type]
