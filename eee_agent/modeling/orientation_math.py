"""Pure-Python PCA + axis-comparison helpers for orientation verification.

Ported verbatim from ``Edini/python3.11libs/edini/orientation_math.py``. No
``hou`` dependency — the math is testable in isolation and imported by the
Houdini-side verify gates (bake / structure / orientation / health).

This module contains:
- ``AXIS_VECTORS`` / ``LOCAL_AXIS_VECTORS`` — named axis → unit vector maps;
- ``KIND_EIGEN_RANK`` — which eigenvector (by eigenvalue rank) matches a kind;
- ``compute_covariance`` — 3x3 position covariance + centroid;
- ``jacobi_eigen_3x3`` — symmetric 3x3 eigendecomposition via Jacobi rotations;
- ``axis_angle_between`` — (angle, fix-quaternion) rotating one axis to another;
- ``dominant_axis_name`` — closest named axis to a vector;
- ``flip_to_hemisphere`` — flip a vector into a reference's hemisphere;
- ``rotate_vector_by_quaternion`` — rotate a 3-vector by a unit quaternion.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

Point3 = Sequence[float]
Vec3 = tuple[float, float, float]
Quaternion = tuple[float, float, float, float]

AXIS_VECTORS: dict[str, Vec3] = {
    "X": (1.0, 0.0, 0.0),
    "Y": (0.0, 1.0, 0.0),
    "Z": (0.0, 0.0, 1.0),
    "-X": (-1.0, 0.0, 0.0),
    "-Y": (0.0, -1.0, 0.0),
    "-Z": (0.0, 0.0, -1.0),
}

KIND_EIGEN_RANK = {
    "radial": 0,     # smallest eigenvalue -> symmetry axis (axle)
    "planar": 0,     # smallest eigenvalue -> surface normal
    "elongated": 2,  # largest eigenvalue -> long axis
}


def compute_covariance(
    points: list[Point3],
) -> tuple[list[list[float]], Vec3]:
    """Return (3x3 covariance matrix, centroid) for a set of 3D points."""
    n = len(points)
    if n == 0:
        return [[0.0] * 3 for _ in range(3)], (0.0, 0.0, 0.0)
    sx = sy = sz = 0.0
    for x, y, z in points:
        sx += x
        sy += y
        sz += z
    cx, cy, cz = sx / n, sy / n, sz / n
    cxx = cyy = czz = cxy = cxz = cyz = 0.0
    for x, y, z in points:
        dx, dy, dz = x - cx, y - cy, z - cz
        cxx += dx * dx
        cyy += dy * dy
        czz += dz * dz
        cxy += dx * dy
        cxz += dx * dz
        cyz += dy * dz
    cov = [
        [cxx / n, cxy / n, cxz / n],
        [cxy / n, cyy / n, cyz / n],
        [cxz / n, cyz / n, czz / n],
    ]
    return cov, (cx, cy, cz)


def jacobi_eigen_3x3(
    cov: list[list[float]],
    max_iter: int = 50,
    tol: float = 1e-10,
) -> tuple[list[float], list[Vec3]]:
    """Symmetric 3x3 eigendecomposition via Jacobi rotations.

    Returns (eigenvalues_ascending, eigenvectors) where eigenvectors[i]
    corresponds to eigenvalues[i]. Eigenvectors are unit-length and mutually
    orthogonal.
    """
    a = [row[:] for row in cov]
    v = [[1.0 if i == j else 0.0 for j in range(3)] for i in range(3)]

    for _ in range(max_iter):
        off = abs(a[0][1]) + abs(a[0][2]) + abs(a[1][2])
        if off < tol:
            break
        for p in range(3):
            for q in range(p + 1, 3):
                apq = a[p][q]
                if abs(apq) < tol:
                    continue
                app = a[p][p]
                aqq = a[q][q]
                phi = 0.5 * _atan2_safe(2.0 * apq, aqq - app)
                c = math.cos(phi)
                s = math.sin(phi)
                for i in range(3):
                    aip = a[i][p]
                    aiq = a[i][q]
                    a[i][p] = c * aip - s * aiq
                    a[i][q] = s * aip + c * aiq
                for i in range(3):
                    api = a[p][i]
                    aqi = a[q][i]
                    a[p][i] = c * api - s * aqi
                    a[q][i] = s * api + c * aqi
                for i in range(3):
                    vip = v[i][p]
                    viq = v[i][q]
                    v[i][p] = c * vip - s * viq
                    v[i][q] = s * vip + c * viq

    eigs = [a[0][0], a[1][1], a[2][2]]
    order = sorted(range(3), key=lambda i: eigs[i])
    sorted_eigs = [eigs[i] for i in order]
    result_vecs: list[Vec3] = []
    for c in range(3):
        col_idx = order[c]
        vec = (v[0][col_idx], v[1][col_idx], v[2][col_idx])
        m = math.sqrt(vec[0] ** 2 + vec[1] ** 2 + vec[2] ** 2)
        if m > 0:
            vec = (vec[0] / m, vec[1] / m, vec[2] / m)
        else:
            vec = (0.0, 0.0, 0.0)
        result_vecs.append(vec)
    return sorted_eigs, result_vecs


def _atan2_safe(y: float, x: float) -> float:
    if abs(y) < 1e-30 and abs(x) < 1e-30:
        return 0.0
    return math.atan2(y, x)


def axis_angle_between(
    detected: Vec3,
    expected: Vec3,
    signed: bool = False,
) -> tuple[float, Quaternion]:
    """Return (angle_degrees, quaternion_xyzw) rotating detected -> expected.

    Axes are undirected lines by default (signed=False): a detected vector
    pointing along -X is treated as correct when expected is +X. Set signed=True
    for cases where direction matters (e.g. saddle normal must point up).

    Quaternion sign chosen so w >= 0 (canonical form).
    """
    dm = math.sqrt(detected[0] ** 2 + detected[1] ** 2 + detected[2] ** 2)
    em = math.sqrt(expected[0] ** 2 + expected[1] ** 2 + expected[2] ** 2)
    if dm < 1e-9 or em < 1e-9:
        return 0.0, (0.0, 0.0, 0.0, 1.0)
    d = (detected[0] / dm, detected[1] / dm, detected[2] / dm)
    e = (expected[0] / em, expected[1] / em, expected[2] / em)
    raw_dot = d[0] * e[0] + d[1] * e[1] + d[2] * e[2]
    if not signed and raw_dot < 0:
        d = (-d[0], -d[1], -d[2])
        raw_dot = -raw_dot
    dot = max(-1.0, min(1.0, raw_dot))
    angle_rad = math.acos(dot)
    angle_deg = math.degrees(angle_rad)
    axis = (
        d[1] * e[2] - d[2] * e[1],
        d[2] * e[0] - d[0] * e[2],
        d[0] * e[1] - d[1] * e[0],
    )
    am = math.sqrt(axis[0] ** 2 + axis[1] ** 2 + axis[2] ** 2)
    if am < 1e-9:
        if dot > 0:
            return 0.0, (0.0, 0.0, 0.0, 1.0)
        perp = (1.0, 0.0, 0.0) if abs(d[0]) < 0.9 else (0.0, 1.0, 0.0)
        axis = (
            d[1] * perp[2] - d[2] * perp[1],
            d[2] * perp[0] - d[0] * perp[2],
            d[0] * perp[1] - d[1] * perp[0],
        )
        am = math.sqrt(axis[0] ** 2 + axis[1] ** 2 + axis[2] ** 2)
    axis = (axis[0] / am, axis[1] / am, axis[2] / am)
    half = angle_rad / 2.0
    s = math.sin(half)
    c = math.cos(half)
    q = (axis[0] * s, axis[1] * s, axis[2] * s, c)
    if q[3] < 0:
        q = (-q[0], -q[1], -q[2], -q[3])
    return angle_deg, q


def dominant_axis_name(vec: Vec3) -> str:
    """Return 'X' / 'Y' / 'Z' (or '-X' etc.) for the axis closest to vec.

    A (near-)zero vector has no dominant axis; return '?' so a degenerate PCA
    estimate is not silently reported as a spurious 'X'.
    """
    ax = abs(vec[0])
    ay = abs(vec[1])
    az = abs(vec[2])
    if ax + ay + az < 1e-12:
        return "?"
    if ax >= ay and ax >= az:
        return "X" if vec[0] >= 0 else "-X"
    if ay >= ax and ay >= az:
        return "Y" if vec[1] >= 0 else "-Y"
    return "Z" if vec[2] >= 0 else "-Z"


def flip_to_hemisphere(
    vec: Vec3,
    reference: Vec3,
) -> Vec3:
    """Flip vec to the same hemisphere as reference (positive dot product)."""
    dot = vec[0] * reference[0] + vec[1] * reference[1] + vec[2] * reference[2]
    if dot < 0:
        return (-vec[0], -vec[1], -vec[2])
    return vec


# ---- Construction-axis algebra ----
# Unlike the PCA helpers above (which ESTIMATE an axis from point positions),
# construction-axis math is deterministic: a component declares which local
# axis is its symmetry/long/normal axis, and the anchor's @orient quaternion
# rotates that local axis into world space.

LOCAL_AXIS_VECTORS: dict[str, Vec3] = {
    "X": (1.0, 0.0, 0.0),
    "Y": (0.0, 1.0, 0.0),
    "Z": (0.0, 0.0, 1.0),
    "-X": (-1.0, 0.0, 0.0),
    "-Y": (0.0, -1.0, 0.0),
    "-Z": (0.0, 0.0, -1.0),
}


def rotate_vector_by_quaternion(
    vec: Vec3,
    q: Quaternion,
) -> Vec3:
    """Rotate a 3-vector by a quaternion (x, y, z, w).

    Uses the standard v' = q * v * q^-1 formula expanded for unit quaternions.
    """
    vx, vy, vz = float(vec[0]), float(vec[1]), float(vec[2])
    qx, qy, qz, qw = float(q[0]), float(q[1]), float(q[2]), float(q[3])

    # t = 2 * cross(q.xyz, v)
    tx = 2.0 * (qy * vz - qz * vy)
    ty = 2.0 * (qz * vx - qx * vz)
    tz = 2.0 * (qx * vy - qy * vx)

    # v' = v + qw * t + cross(q.xyz, t)
    rx = vx + qw * tx + (qy * tz - qz * ty)
    ry = vy + qw * ty + (qz * tx - qx * tz)
    rz = vz + qw * tz + (qx * ty - qy * tx)

    return (rx, ry, rz)


__all__ = [
    "AXIS_VECTORS",
    "KIND_EIGEN_RANK",
    "LOCAL_AXIS_VECTORS",
    "Point3",
    "Quaternion",
    "Vec3",
    "axis_angle_between",
    "compute_covariance",
    "dominant_axis_name",
    "flip_to_hemisphere",
    "jacobi_eigen_3x3",
    "rotate_vector_by_quaternion",
]
