"""Tests for the ported orientation_math module (pure Python, no hou).

Verifies the PCA + axis-comparison math ported from Pi. These are deterministic
numeric checks against hand-computed expectations.
"""

from __future__ import annotations

import math

import pytest

from eee_agent.modeling.orientation_math import (
    AXIS_VECTORS,
    KIND_EIGEN_RANK,
    axis_angle_between,
    compute_covariance,
    dominant_axis_name,
    flip_to_hemisphere,
    jacobi_eigen_3x3,
    rotate_vector_by_quaternion,
)


class TestComputeCovariance:
    def test_empty_points(self) -> None:
        cov, centroid = compute_covariance([])
        assert cov == [[0.0, 0.0, 0.0], [0.0, 0.0, 0.0], [0.0, 0.0, 0.0]]
        assert centroid == (0.0, 0.0, 0.0)

    def test_centroid_of_symmetric_cloud(self) -> None:
        pts = [(-1.0, 0.0, 0.0), (1.0, 0.0, 0.0)]
        _, centroid = compute_covariance(pts)
        assert centroid == pytest.approx((0.0, 0.0, 0.0))

    def test_covariance_diagonal_dominant_along_x(self) -> None:
        # Points spread along X -> cxx largest.
        pts = [(-2.0, 0.0, 0.0), (2.0, 0.0, 0.0), (0.0, 0.0, 0.0)]
        cov, _ = compute_covariance(pts)
        assert cov[0][0] > cov[1][1]
        assert cov[0][0] > cov[2][2]
        assert cov[1][1] == pytest.approx(0.0)
        assert cov[2][2] == pytest.approx(0.0)


class TestJacobiEigen:
    def test_identity_matrix(self) -> None:
        eigs, vecs = jacobi_eigen_3x3([[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]])
        assert eigs == pytest.approx([1.0, 1.0, 1.0])
        for v in vecs:
            m = math.sqrt(sum(c * c for c in v))
            assert m == pytest.approx(1.0)

    def test_diagonal_matrix_ascending(self) -> None:
        cov = [[3.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 2.0]]
        eigs, _ = jacobi_eigen_3x3(cov)
        # ascending: 1, 2, 3
        assert eigs == pytest.approx([1.0, 2.0, 3.0])

    def test_eigenvectors_orthonormal(self) -> None:
        # An elongated-along-Y cloud.
        pts = [(0.0, -3.0, 0.0), (0.0, 3.0, 0.0), (0.0, 0.0, 0.0)]
        cov, _ = compute_covariance(pts)
        _, vecs = jacobi_eigen_3x3(cov)
        # dot products ~ 0 between distinct, ~1 with self
        for i in range(3):
            for j in range(3):
                dot = sum(vecs[i][k] * vecs[j][k] for k in range(3))
                if i == j:
                    assert dot == pytest.approx(1.0, abs=1e-6)
                else:
                    assert dot == pytest.approx(0.0, abs=1e-6)


class TestDominantAxisName:
    @pytest.mark.parametrize("vec,expected", [
        ((1.0, 0.0, 0.0), "X"),
        ((-1.0, 0.0, 0.0), "-X"),
        ((0.0, 1.0, 0.0), "Y"),
        ((0.0, -1.0, 0.0), "-Y"),
        ((0.0, 0.0, 1.0), "Z"),
        ((0.0, 0.0, -1.0), "-Z"),
        ((0.1, 0.9, 0.0), "Y"),
        ((0.9, 0.1, 0.0), "X"),
    ])
    def test_named_axes(self, vec, expected) -> None:
        assert dominant_axis_name(vec) == expected

    @pytest.mark.parametrize("vec", [
        (0.0, 0.0, 0.0),
        (1e-13, 1e-13, 1e-13),
    ])
    def test_zero_vector_reports_no_axis(self, vec) -> None:
        # A degenerate PCA estimate (collinear/coplanar points -> zero
        # eigenvector) must NOT read as a spurious 'X'; it reports '?'.
        assert dominant_axis_name(vec) == "?"


class TestAxisAngleBetween:
    def test_identical_axes_zero_angle(self) -> None:
        angle, q = axis_angle_between((1.0, 0.0, 0.0), (1.0, 0.0, 0.0))
        assert angle == pytest.approx(0.0)
        assert q == pytest.approx((0.0, 0.0, 0.0, 1.0))

    def test_perpendicular_axes_90_deg(self) -> None:
        angle, _ = axis_angle_between((1.0, 0.0, 0.0), (0.0, 1.0, 0.0))
        assert angle == pytest.approx(90.0)

    def test_unsigned_treats_opposite_as_same(self) -> None:
        angle, _ = axis_angle_between((-1.0, 0.0, 0.0), (1.0, 0.0, 0.0), signed=False)
        assert angle == pytest.approx(0.0)

    def test_signed_treats_opposite_as_180(self) -> None:
        angle, _ = axis_angle_between((-1.0, 0.0, 0.0), (1.0, 0.0, 0.0), signed=True)
        assert angle == pytest.approx(180.0)

    def test_zero_vector_safe(self) -> None:
        angle, q = axis_angle_between((0.0, 0.0, 0.0), (1.0, 0.0, 0.0))
        assert angle == pytest.approx(0.0)
        assert q == pytest.approx((0.0, 0.0, 0.0, 1.0))

    def test_quaternion_is_canonical_w_nonneg(self) -> None:
        _, q = axis_angle_between((1.0, 0.0, 0.0), (0.0, 1.0, 0.0))
        assert q[3] >= 0.0


class TestFlipToHemisphere:
    def test_same_hemisphere_unchanged(self) -> None:
        assert flip_to_hemisphere((1.0, 0.0, 0.0), (1.0, 0.0, 0.0)) == (1.0, 0.0, 0.0)

    def test_opposite_hemisphere_flipped(self) -> None:
        assert flip_to_hemisphere((-1.0, 0.0, 0.0), (1.0, 0.0, 0.0)) == (1.0, 0.0, 0.0)


class TestRotateVectorByQuaternion:
    def test_identity_quaternion_no_rotation(self) -> None:
        v = (1.0, 2.0, 3.0)
        q = (0.0, 0.0, 0.0, 1.0)  # identity
        assert rotate_vector_by_quaternion(v, q) == pytest.approx(v)

    def test_90deg_around_z_rotates_x_to_y(self) -> None:
        half = math.radians(45.0)
        q = (0.0, 0.0, math.sin(half), math.cos(half))  # 90deg around Z
        result = rotate_vector_by_quaternion((1.0, 0.0, 0.0), q)
        assert result == pytest.approx((0.0, 1.0, 0.0), abs=1e-6)


class TestKindEigenRank:
    def test_radial_and_planar_use_smallest(self) -> None:
        assert KIND_EIGEN_RANK["radial"] == 0
        assert KIND_EIGEN_RANK["planar"] == 0

    def test_elongated_uses_largest(self) -> None:
        assert KIND_EIGEN_RANK["elongated"] == 2


class TestAxisVectors:
    def test_all_six_axes_present(self) -> None:
        for name in ("X", "Y", "Z", "-X", "-Y", "-Z"):
            assert name in AXIS_VECTORS
            v = AXIS_VECTORS[name]
            assert len(v) == 3
            assert math.sqrt(sum(c * c for c in v)) == pytest.approx(1.0)
