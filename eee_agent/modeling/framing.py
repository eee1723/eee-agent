"""Deterministic geometry-bbox-driven capture framing (pure, offline-testable).

This module implements the Task 19-A framing preflight as a pure function: a
union geometry bounding box goes in, a camera placement plus a bounded
acceptance report comes out. No ``hou``, no rendering, no randomness, no wall
clock — the same inputs always produce bit-identical outputs, so the whole
decision logic is covered by offline unit tests and the HOM glue in the
executor stays a thin projection of this plan.

Algorithm (fixed order, fixed iteration counts):

1. The camera sits on the fixed front-right-above three-quarter view ray
   (:data:`_VIEW_OFFSET`) through the bbox center, with a fixed 50mm lens on
   a 36mm sensor (full-frame), looking at the bbox center.
2. A bisection solve (fixed 48 iterations) finds the distance that makes the
   longest projected bbox axis occupy the mid-band ratio of the frame.
3. Acceptance (all four edges at least ``margin_min``, longest axis within
   ``longest_axis_min``..``longest_axis_max``, center offset at most
   ``center_offset_max``) is evaluated by exact pinhole projection of the 8
   bbox corners.
4. If the first placement fails acceptance, at most ``max_adjustments``
   deterministic adjustments run in a fixed order: a recenter (parallel
   translation that lands the projected rect center on the frame center) or a
   zoom (a fresh distance solve, zooming out when a margin is short). Budget
   exhaustion raises :class:`FramingError`; a failure is never coerced into a
   passing report.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

# Fixed front-right-above three-quarter view offset (Houdini Y-up, front +Z).
_VIEW_OFFSET = (1.0, 0.8, 1.0)
_FOCAL_LENGTH_MM = 50.0
_SENSOR_WIDTH_MM = 36.0
_WORLD_UP = (0.0, 1.0, 0.0)

_SOLVE_ITERATIONS = 48
_EPS = 1e-9


class FramingError(ValueError):
    """Raised when no acceptable framing is reachable within the budget."""


# --------------------------------------------------------------------------
# small vector helpers (fixed evaluation order, float math only)
# --------------------------------------------------------------------------


def _dot(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def _sub(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def _add(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def _scale(a: tuple[float, float, float], k: float) -> tuple[float, float, float]:
    return (a[0] * k, a[1] * k, a[2] * k)


def _cross(a: tuple[float, float, float], b: tuple[float, float, float]) -> tuple[float, float, float]:
    return (
        a[1] * b[2] - a[2] * b[1],
        a[2] * b[0] - a[0] * b[2],
        a[0] * b[1] - a[1] * b[0],
    )


def _length(a: tuple[float, float, float]) -> float:
    return math.sqrt(_dot(a, a))


def _normalize(a: tuple[float, float, float]) -> tuple[float, float, float]:
    norm = _length(a)
    if norm <= _EPS:
        raise FramingError("cannot normalize a zero-length framing vector")
    return (a[0] / norm, a[1] / norm, a[2] / norm)


# --------------------------------------------------------------------------
# DTOs
# --------------------------------------------------------------------------


def _finite3(value: object, label: str) -> tuple[float, float, float]:
    if isinstance(value, str) or not isinstance(value, Sequence):
        raise TypeError(f"{label} must be a sequence of three finite numbers")
    items = tuple(value)
    if len(items) != 3:
        raise ValueError(f"{label} must contain exactly three numbers")
    out: list[float] = []
    for item in items:
        if type(item) is bool or type(item) not in (int, float):
            raise TypeError(f"{label} entries must be exact int or float")
        if not math.isfinite(item):
            raise ValueError(f"{label} entries must be finite")
        out.append(float(item))
    return (out[0], out[1], out[2])


@dataclass(frozen=True, slots=True)
class BoundingBox:
    """One finite axis-aligned geometry bounding box (min <= max per axis)."""

    min_x: float
    min_y: float
    min_z: float
    max_x: float
    max_y: float
    max_z: float

    def __post_init__(self) -> None:
        for label, value in (
            ("min_x", self.min_x),
            ("min_y", self.min_y),
            ("min_z", self.min_z),
            ("max_x", self.max_x),
            ("max_y", self.max_y),
            ("max_z", self.max_z),
        ):
            if type(value) is bool or type(value) not in (int, float):
                raise TypeError(f"BoundingBox.{label} must be an exact int or float")
            if not math.isfinite(value):
                raise ValueError(f"BoundingBox.{label} must be finite")
            object.__setattr__(self, label, float(value))
        if self.min_x > self.max_x or self.min_y > self.max_y or self.min_z > self.max_z:
            raise ValueError("BoundingBox minimums must not exceed maximums")

    @property
    def center(self) -> tuple[float, float, float]:
        return (
            0.5 * (self.min_x + self.max_x),
            0.5 * (self.min_y + self.max_y),
            0.5 * (self.min_z + self.max_z),
        )

    @property
    def size(self) -> tuple[float, float, float]:
        return (
            self.max_x - self.min_x,
            self.max_y - self.min_y,
            self.max_z - self.min_z,
        )

    @property
    def bounding_radius(self) -> float:
        return 0.5 * _length(self.size)

    @property
    def corners(self) -> tuple[tuple[float, float, float], ...]:
        """The 8 corners in one fixed order (deterministic projection)."""
        return (
            (self.min_x, self.min_y, self.min_z),
            (self.min_x, self.min_y, self.max_z),
            (self.min_x, self.max_y, self.min_z),
            (self.min_x, self.max_y, self.max_z),
            (self.max_x, self.min_y, self.min_z),
            (self.max_x, self.min_y, self.max_z),
            (self.max_x, self.max_y, self.min_z),
            (self.max_x, self.max_y, self.max_z),
        )

    @staticmethod
    def union(boxes: Sequence[BoundingBox]) -> BoundingBox:
        """Return the smallest box covering every input (1..64, fixed order)."""
        if isinstance(boxes, str) or not isinstance(boxes, Sequence):
            raise TypeError("boxes must be a sequence of BoundingBox values")
        items = tuple(boxes)
        if not items or len(items) > 64:
            raise ValueError("boxes must contain 1..64 BoundingBox values")
        if any(type(item) is not BoundingBox for item in items):
            raise TypeError("boxes must contain exact BoundingBox values")
        return BoundingBox(
            min_x=min(item.min_x for item in items),
            min_y=min(item.min_y for item in items),
            min_z=min(item.min_z for item in items),
            max_x=max(item.max_x for item in items),
            max_y=max(item.max_y for item in items),
            max_z=max(item.max_z for item in items),
        )


@dataclass(frozen=True, slots=True)
class FramingTolerance:
    """The acceptance band: 6% margins, 72%..84% longest axis, 3% offset."""

    margin_min: float = 0.06
    longest_axis_min: float = 0.72
    longest_axis_max: float = 0.84
    center_offset_max: float = 0.03

    def __post_init__(self) -> None:
        for label, value in (
            ("margin_min", self.margin_min),
            ("longest_axis_min", self.longest_axis_min),
            ("longest_axis_max", self.longest_axis_max),
            ("center_offset_max", self.center_offset_max),
        ):
            if type(value) is bool or type(value) not in (int, float):
                raise TypeError(f"FramingTolerance.{label} must be an exact int or float")
            if not math.isfinite(value):
                raise ValueError(f"FramingTolerance.{label} must be finite")
        if not 0.0 < self.margin_min < 0.25:
            raise ValueError("FramingTolerance.margin_min must be in (0, 0.25)")
        if not 0.5 <= self.longest_axis_min < self.longest_axis_max < 1.0:
            raise ValueError("FramingTolerance longest-axis bounds must be in [0.5, 1.0)")
        if not 0.0 < self.center_offset_max < 0.25:
            raise ValueError("FramingTolerance.center_offset_max must be in (0, 0.25)")


@dataclass(frozen=True, slots=True)
class CameraFraming:
    """One deterministic camera placement (fixed 50mm lens, 36mm sensor)."""

    position: tuple[float, float, float]
    look_at: tuple[float, float, float]
    focal_length_mm: float = _FOCAL_LENGTH_MM
    sensor_width_mm: float = _SENSOR_WIDTH_MM

    def __post_init__(self) -> None:
        object.__setattr__(self, "position", _finite3(self.position, "CameraFraming.position"))
        object.__setattr__(self, "look_at", _finite3(self.look_at, "CameraFraming.look_at"))
        for label, value in (
            ("focal_length_mm", self.focal_length_mm),
            ("sensor_width_mm", self.sensor_width_mm),
        ):
            if type(value) is bool or type(value) not in (int, float):
                raise TypeError(f"CameraFraming.{label} must be an exact int or float")
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"CameraFraming.{label} must be positive and finite")
            object.__setattr__(self, label, float(value))
        if _length(_sub(self.position, self.look_at)) <= _EPS:
            raise ValueError("CameraFraming position and look_at must differ")


@dataclass(frozen=True, slots=True)
class FramingEvaluation:
    """The measured framing margins/ratios of one placement."""

    margin_left: float
    margin_right: float
    margin_bottom: float
    margin_top: float
    longest_axis_ratio: float
    center_offset: float
    accepted: bool


@dataclass(frozen=True, slots=True)
class FramingPlan:
    """The accepted camera placement plus its bounded acceptance report."""

    camera: CameraFraming
    evaluation: FramingEvaluation
    adjustments_used: int

    def __post_init__(self) -> None:
        if type(self.camera) is not CameraFraming:
            raise TypeError("FramingPlan.camera must be an exact CameraFraming")
        if type(self.evaluation) is not FramingEvaluation:
            raise TypeError("FramingPlan.evaluation must be an exact FramingEvaluation")
        if type(self.adjustments_used) is not int or not 0 <= self.adjustments_used <= 2:
            raise ValueError("FramingPlan.adjustments_used must be in 0..2")
        if not self.evaluation.accepted:
            raise ValueError("FramingPlan requires an accepted evaluation")


# --------------------------------------------------------------------------
# camera basis + pinhole projection (deterministic)
# --------------------------------------------------------------------------


def camera_basis(
    position: tuple[float, float, float], look_at: tuple[float, float, float]
) -> tuple[tuple[float, float, float], tuple[float, float, float], tuple[float, float, float]]:
    """Return the (right, up, forward) orthonormal basis for HOM glue."""
    forward = _normalize(_sub(look_at, position))
    right = _normalize(_cross(forward, _WORLD_UP))
    up = _cross(right, forward)
    return right, up, forward


def _project_rect(
    bbox: BoundingBox,
    camera: CameraFraming,
    *,
    frame_width: int,
    frame_height: int,
) -> tuple[float, float, float, float]:
    """Project the 8 corners to normalized frame coords; return the rect."""
    right, up, forward = camera_basis(camera.position, camera.look_at)
    sensor_height = camera.sensor_width_mm * frame_height / frame_width
    min_sx = math.inf
    max_sx = -math.inf
    min_sy = math.inf
    max_sy = -math.inf
    for corner in bbox.corners:
        rel = _sub(corner, camera.position)
        depth = _dot(rel, forward)
        if depth <= _EPS:
            raise FramingError("capture camera must stay in front of the geometry")
        sx = 0.5 + (_dot(rel, right) / depth) * (
            camera.focal_length_mm / camera.sensor_width_mm
        )
        sy = 0.5 + (_dot(rel, up) / depth) * (
            camera.focal_length_mm / sensor_height
        )
        min_sx = min(min_sx, sx)
        max_sx = max(max_sx, sx)
        min_sy = min(min_sy, sy)
        max_sy = max(max_sy, sy)
    return min_sx, max_sx, min_sy, max_sy


def _evaluate(
    bbox: BoundingBox,
    camera: CameraFraming,
    *,
    frame_width: int,
    frame_height: int,
    tolerance: FramingTolerance,
) -> FramingEvaluation:
    min_sx, max_sx, min_sy, max_sy = _project_rect(
        bbox, camera, frame_width=frame_width, frame_height=frame_height
    )
    width = max_sx - min_sx
    height = max_sy - min_sy
    longest = max(width, height)
    center_x = 0.5 * (min_sx + max_sx)
    center_y = 0.5 * (min_sy + max_sy)
    offset = math.hypot(center_x - 0.5, center_y - 0.5)
    margins = (min_sx, 1.0 - max_sx, min_sy, 1.0 - max_sy)
    accepted = (
        all(margin >= tolerance.margin_min for margin in margins)
        and tolerance.longest_axis_min <= longest <= tolerance.longest_axis_max
        and offset <= tolerance.center_offset_max
    )
    return FramingEvaluation(
        margin_left=margins[0],
        margin_right=margins[1],
        margin_bottom=margins[2],
        margin_top=margins[3],
        longest_axis_ratio=longest,
        center_offset=offset,
        accepted=accepted,
    )


def _solve_distance(
    bbox: BoundingBox,
    look_at: tuple[float, float, float],
    *,
    frame_width: int,
    frame_height: int,
    target_ratio: float,
) -> float:
    """Bisect the view-ray distance so the longest axis hits ``target_ratio``."""
    radius = bbox.bounding_radius
    view = _normalize(_VIEW_OFFSET)
    lo = radius * (1.0 + 1e-6)
    hi = radius * 1.0e6

    def longest(distance: float) -> float:
        camera = CameraFraming(
            position=_add(look_at, _scale(view, distance)),
            look_at=look_at,
        )
        min_sx, max_sx, min_sy, max_sy = _project_rect(
            bbox, camera, frame_width=frame_width, frame_height=frame_height
        )
        return max(max_sx - min_sx, max_sy - min_sy)

    for _ in range(_SOLVE_ITERATIONS):
        mid = 0.5 * (lo + hi)
        if longest(mid) > target_ratio:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


# --------------------------------------------------------------------------
# public entrypoint
# --------------------------------------------------------------------------


def compute_framing(
    bbox: BoundingBox,
    *,
    frame_width: int,
    frame_height: int,
    tolerance: FramingTolerance | None = None,
    max_adjustments: int = 2,
) -> FramingPlan:
    """Compute the deterministic accepted framing for one geometry bbox.

    Raises :class:`FramingError` for a degenerate bbox or when acceptance is
    not reachable within ``max_adjustments`` deterministic adjustments.
    """
    if type(bbox) is not BoundingBox:
        raise TypeError("bbox must be an exact BoundingBox")
    for label, value in (("frame_width", frame_width), ("frame_height", frame_height)):
        if type(value) is not int or value < 16 or value > 4096:
            raise ValueError(f"{label} must be an int in 16..4096")
    if tolerance is None:
        tolerance = FramingTolerance()
    if type(tolerance) is not FramingTolerance:
        raise TypeError("tolerance must be an exact FramingTolerance or None")
    if type(max_adjustments) is not int or not 0 <= max_adjustments <= 2:
        raise ValueError("max_adjustments must be in 0..2")
    if bbox.bounding_radius <= _EPS:
        raise FramingError("cannot frame degenerate (empty) geometry")

    band_mid = 0.5 * (tolerance.longest_axis_min + tolerance.longest_axis_max)
    look_at = bbox.center
    view = _normalize(_VIEW_OFFSET)
    distance = _solve_distance(
        bbox,
        look_at,
        frame_width=frame_width,
        frame_height=frame_height,
        target_ratio=band_mid,
    )
    camera = CameraFraming(
        position=_add(look_at, _scale(view, distance)),
        look_at=look_at,
    )
    evaluation = _evaluate(
        bbox, camera, frame_width=frame_width, frame_height=frame_height, tolerance=tolerance
    )
    adjustments_used = 0
    while not evaluation.accepted:
        if adjustments_used >= max_adjustments:
            raise FramingError(
                "framing acceptance was not reachable within the adjustment budget"
            )
        if evaluation.center_offset > tolerance.center_offset_max:
            # Recenter: parallel-translate the camera so the projected rect
            # center lands exactly on the frame center (exact pinhole shift at
            # the bbox-center depth).
            right, up, forward = camera_basis(camera.position, camera.look_at)
            center_depth = _dot(_sub(bbox.center, camera.position), forward)
            rect = _project_rect(
                bbox, camera, frame_width=frame_width, frame_height=frame_height
            )
            error_x = 0.5 * (rect[0] + rect[1]) - 0.5
            error_y = 0.5 * (rect[2] + rect[3]) - 0.5
            sensor_height = camera.sensor_width_mm * frame_height / frame_width
            shift = _add(
                _scale(right, error_x * center_depth * camera.sensor_width_mm / camera.focal_length_mm),
                _scale(up, error_y * center_depth * sensor_height / camera.focal_length_mm),
            )
            camera = CameraFraming(
                position=_add(camera.position, shift),
                look_at=_add(camera.look_at, shift),
            )
        else:
            # Zoom: re-solve the distance. A margin shortfall zooms out by the
            # exact deficit; a band miss re-centers on the band mid.
            worst_margin = min(
                evaluation.margin_left,
                evaluation.margin_right,
                evaluation.margin_bottom,
                evaluation.margin_top,
            )
            target = band_mid
            if worst_margin < tolerance.margin_min:
                deficit = tolerance.margin_min - worst_margin
                target = max(
                    tolerance.longest_axis_min,
                    min(tolerance.longest_axis_max, band_mid - 2.0 * deficit),
                )
            distance = _solve_distance(
                bbox,
                camera.look_at,
                frame_width=frame_width,
                frame_height=frame_height,
                target_ratio=target,
            )
            camera = CameraFraming(
                position=_add(camera.look_at, _scale(view, distance)),
                look_at=camera.look_at,
            )
        adjustments_used += 1
        evaluation = _evaluate(
            bbox, camera, frame_width=frame_width, frame_height=frame_height, tolerance=tolerance
        )
    return FramingPlan(camera=camera, evaluation=evaluation, adjustments_used=adjustments_used)


__all__ = [
    "BoundingBox",
    "CameraFraming",
    "FramingError",
    "FramingEvaluation",
    "FramingPlan",
    "FramingTolerance",
    "camera_basis",
    "compute_framing",
]
