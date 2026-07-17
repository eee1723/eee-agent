"""Additive ``capture.v1`` Bridge capability and typed capture DTOs.

This module adds the smallest auditable screenshot capture surface on top of
the accepted :mod:`eee_agent.houdini_bridge.changesets` typed operation
pattern (Task 19-A). It defines:

* the advertised :data:`CAPTURE_V1` capability;
* frozen, slotted, JSON-canonical DTOs for the ``capture.capture`` request
  and its bounded content-addressed reference response; and
* strict parsers (:func:`parse_capture_request` / :func:`parse_capture_response`).

It imports **neither** ``hou`` **nor** ``rpyc``. The operation is an internal
trusted Runtime-to-Bridge operation exactly like ``changeset.apply``: it runs
only on the single main-thread FIFO, the Houdini side writes the PNG to the
Runtime-owned artifacts directory (``<name>.png.tmp`` then an atomic rename)
and returns only content-addressed reference fields — image bytes NEVER cross
the bridge wire. Any uncertainty (stale scene, an unresolvable evidence node,
a cook/framing/render failure, or a temp-scope cleanup failure) fails closed
with a structured error instead of a guessed visual success. The request
carries only absolute node paths, the scene epoch, the absolute artifacts
target directory, the artifact id, and deterministic framing/render settings.

Strictness mirrors the rest of the bridge package: exact primitive types,
exact field sets, deep-frozen canonical JSON, duplicate-key rejection, finite
numbers, and an explicit size limit.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import PureWindowsPath

from eee_agent.core.ids import IdKind, require_id
from eee_agent.houdini_bridge.changesets import (
    _CONTROL_RE,
    _MAX_DEADLINE_MS,
    _MAX_NODE_PATH_LEN,
    _MAX_RESULT_BYTES,
    _MIN_DEADLINE_MS,
    _REQUEST_FIELDS,
    _RESPONSE_REQUIRED_FIELDS,
    _load_strict_json,
    _require_exact_bool,
    _require_exact_dict,
    _require_exact_int,
    _require_exact_keys,
    _require_request_id,
    _require_sha256,
)
from eee_agent.houdini_bridge.contracts import (
    PROTOCOL,
    BridgeError,
)
from eee_agent.runtime.models import canonical_json_dumps

# --------------------------------------------------------------------------
# constants
# --------------------------------------------------------------------------

CAPTURE_V1 = "capture.v1"
CAPTURE_OPERATION = "capture.capture"

PNG_MEDIA_TYPE = "image/png"

_MAX_NODE_PATHS = 64
_MAX_TARGET_DIR_LEN = 1024
_MAX_PNG_BYTES = 64 * 1024 * 1024

# exact field sets for envelope + payload validation
_PAYLOAD_FIELDS = frozenset({"node_paths", "target_dir", "artifact_id", "settings"})
_SETTINGS_FIELDS = frozenset(
    {
        "width",
        "height",
        "preflight_width",
        "preflight_height",
        "margin_min",
        "longest_axis_min",
        "longest_axis_max",
        "center_offset_max",
        "max_adjustments",
    }
)
_FRAMING_FIELDS = frozenset(
    {
        "adjustments_used",
        "margin_left",
        "margin_right",
        "margin_bottom",
        "margin_top",
        "longest_axis_ratio",
        "center_offset",
    }
)
_RESULT_FIELDS = frozenset(
    {"artifact_id", "relative_path", "sha256", "media_type", "size_bytes", "framing"}
)

_PNG_FILE_NAME_RE = re.compile(r"^[A-Za-z0-9_-]+\.png$")


# --------------------------------------------------------------------------
# shared strictness helpers
# --------------------------------------------------------------------------


def _require_node_path(value: object, label: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{label} must be a string")
    if not value.startswith("/"):
        raise ValueError(f"{label} must be an absolute Houdini path")
    if len(value) > _MAX_NODE_PATH_LEN:
        raise ValueError(f"{label} exceeds the maximum node path length")


def _require_target_dir(value: object, label: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{label} must be a string")
    if not value or len(value) > _MAX_TARGET_DIR_LEN:
        raise ValueError(f"{label} must be a non-empty string (<=1024 chars)")
    if _CONTROL_RE.search(value) is not None:
        raise ValueError(f"{label} must not contain control characters")
    # The Runtime and the Bridge share one machine; an absolute OS path has a
    # Windows drive or a POSIX root. Relative targets are rejected so the
    # Houdini side can never write outside the Runtime-owned artifacts root.
    if not PureWindowsPath(value).drive and not value.startswith("/"):
        raise ValueError(f"{label} must be an absolute directory path")


def _require_png_file_name(value: object, label: str) -> None:
    if type(value) is not str:
        raise TypeError(f"{label} must be a string")
    if _PNG_FILE_NAME_RE.fullmatch(value) is None or len(value) > 64:
        raise ValueError(f"{label} must be one bounded ``<name>.png`` component")


def _require_ratio(value: object, label: str) -> None:
    if type(value) is bool or type(value) not in (int, float):
        raise TypeError(f"{label} must be an exact int or float")
    if not math.isfinite(value):
        raise ValueError(f"{label} must be finite")


# --------------------------------------------------------------------------
# deterministic framing/render settings + request DTOs
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CaptureSettings:
    """The deterministic framing/render settings for one capture.

    Defaults are the accepted Task 19-A constants: 1280x960 PNG output, a
    640x480 preflight frame of the same 4:3 aspect, at least 6% margin on all
    four edges, the longest projected axis occupying 72%..84% of the frame,
    at most 3% center offset, and at most two framing adjustments. The
    Runtime always sends the canonical defaults; strict ranges keep a
    malformed wire value from changing the deterministic capture look.
    """

    width: int = 1280
    height: int = 960
    preflight_width: int = 640
    preflight_height: int = 480
    margin_min: float = 0.06
    longest_axis_min: float = 0.72
    longest_axis_max: float = 0.84
    center_offset_max: float = 0.03
    max_adjustments: int = 2

    def __post_init__(self) -> None:
        for label, value in (
            ("width", self.width),
            ("height", self.height),
            ("preflight_width", self.preflight_width),
            ("preflight_height", self.preflight_height),
        ):
            _require_exact_int(value, f"CaptureSettings.{label}")
            if value < 16 or value > 4096 or value % 2 != 0:
                raise ValueError(f"CaptureSettings.{label} must be an even int in 16..4096")
        if self.width * self.preflight_height != self.height * self.preflight_width:
            raise ValueError("CaptureSettings preflight frame must share the output aspect")
        for label, value in (
            ("margin_min", self.margin_min),
            ("longest_axis_min", self.longest_axis_min),
            ("longest_axis_max", self.longest_axis_max),
            ("center_offset_max", self.center_offset_max),
        ):
            _require_ratio(value, f"CaptureSettings.{label}")
        if not 0.0 < self.margin_min < 0.25:
            raise ValueError("CaptureSettings.margin_min must be in (0, 0.25)")
        if not 0.5 <= self.longest_axis_min < self.longest_axis_max < 1.0:
            raise ValueError("CaptureSettings longest-axis bounds must be in [0.5, 1.0)")
        if not 0.0 < self.center_offset_max < 0.25:
            raise ValueError("CaptureSettings.center_offset_max must be in (0, 0.25)")
        _require_exact_int(self.max_adjustments, "CaptureSettings.max_adjustments")
        if not 0 <= self.max_adjustments <= 2:
            raise ValueError("CaptureSettings.max_adjustments must be in 0..2")

    def to_dict(self) -> dict[str, object]:
        return {
            "width": self.width,
            "height": self.height,
            "preflight_width": self.preflight_width,
            "preflight_height": self.preflight_height,
            "margin_min": self.margin_min,
            "longest_axis_min": self.longest_axis_min,
            "longest_axis_max": self.longest_axis_max,
            "center_offset_max": self.center_offset_max,
            "max_adjustments": self.max_adjustments,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> CaptureSettings:
        value = _require_exact_dict(data, "CaptureSettings")
        _require_exact_keys(value, _SETTINGS_FIELDS, "CaptureSettings")
        return cls(
            width=value["width"],  # type: ignore[arg-type]
            height=value["height"],  # type: ignore[arg-type]
            preflight_width=value["preflight_width"],  # type: ignore[arg-type]
            preflight_height=value["preflight_height"],  # type: ignore[arg-type]
            margin_min=value["margin_min"],  # type: ignore[arg-type]
            longest_axis_min=value["longest_axis_min"],  # type: ignore[arg-type]
            longest_axis_max=value["longest_axis_max"],  # type: ignore[arg-type]
            center_offset_max=value["center_offset_max"],  # type: ignore[arg-type]
            max_adjustments=value["max_adjustments"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class CaptureRequest:
    """A parsed, validated ``capture.capture`` request envelope.

    Carries the bounded evidence node set (the exact compiled node paths whose
    cooked geometry drives framing), the absolute Runtime-owned artifacts
    target directory, the artifact id (the Houdini side writes exactly
    ``<artifact_id>.png``), and the deterministic framing/render settings. The
    envelope ``scene_epoch`` is re-checked against the tracked scene before any
    node is created; a mismatch fails closed with zero scene changes.
    """

    request_id: str
    deadline_ms: int
    scene_epoch: int
    node_paths: tuple[str, ...]
    target_dir: str
    artifact_id: str
    settings: CaptureSettings

    def __post_init__(self) -> None:
        _require_request_id(self.request_id, "CaptureRequest.request_id")
        _require_exact_int(self.deadline_ms, "CaptureRequest.deadline_ms")
        if self.deadline_ms < _MIN_DEADLINE_MS or self.deadline_ms > _MAX_DEADLINE_MS:
            raise ValueError("CaptureRequest.deadline_ms must be in 1..30000")
        _require_exact_int(self.scene_epoch, "CaptureRequest.scene_epoch")
        if self.scene_epoch < 1:
            raise ValueError("CaptureRequest.scene_epoch must be >= 1")
        if isinstance(self.node_paths, str) or not isinstance(
            self.node_paths, Sequence
        ):
            raise TypeError("CaptureRequest.node_paths must be a sequence")
        node_paths = tuple(self.node_paths)
        if not node_paths or len(node_paths) > _MAX_NODE_PATHS:
            raise ValueError("CaptureRequest.node_paths must contain 1..64 paths")
        if len(set(node_paths)) != len(node_paths):
            raise ValueError("CaptureRequest.node_paths contains duplicates")
        for path in node_paths:
            _require_node_path(path, "CaptureRequest.node_paths")
        object.__setattr__(self, "node_paths", node_paths)
        _require_target_dir(self.target_dir, "CaptureRequest.target_dir")
        if type(self.artifact_id) is not str:
            raise TypeError("CaptureRequest.artifact_id must be a string")
        require_id(self.artifact_id, IdKind.ARTIFACT)
        if type(self.settings) is not CaptureSettings:
            raise TypeError("CaptureRequest.settings must be an exact CaptureSettings")

    @property
    def file_name(self) -> str:
        """The exact PNG file name the Houdini side writes (no separators)."""
        return f"{self.artifact_id}.png"

    @classmethod
    def build(
        cls,
        *,
        request_id: str,
        deadline_ms: int,
        scene_epoch: int,
        node_paths: Sequence[str],
        target_dir: str,
        artifact_id: str,
        settings: CaptureSettings | None = None,
    ) -> CaptureRequest:
        return cls(
            request_id=request_id,
            deadline_ms=deadline_ms,
            scene_epoch=scene_epoch,
            node_paths=tuple(node_paths),
            target_dir=target_dir,
            artifact_id=artifact_id,
            settings=CaptureSettings() if settings is None else settings,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "protocol": PROTOCOL,
            "kind": "request",
            "request_id": self.request_id,
            "operation": CAPTURE_OPERATION,
            "deadline_ms": self.deadline_ms,
            "scene_epoch": self.scene_epoch,
            "payload": {
                "node_paths": list(self.node_paths),
                "target_dir": self.target_dir,
                "artifact_id": self.artifact_id,
                "settings": self.settings.to_dict(),
            },
        }

    def to_json(self) -> str:
        return canonical_json_dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> CaptureRequest:
        envelope = _require_exact_dict(data, "CaptureRequest envelope")
        _require_exact_keys(envelope, _REQUEST_FIELDS, "CaptureRequest envelope")
        if envelope["protocol"] != PROTOCOL:
            raise ValueError("CaptureRequest protocol must be eee.bridge/1")
        if envelope["kind"] != "request":
            raise ValueError("CaptureRequest kind must be request")
        if envelope["operation"] != CAPTURE_OPERATION:
            raise ValueError("CaptureRequest operation must be capture.capture")
        payload = _require_exact_dict(envelope["payload"], "CaptureRequest payload")
        _require_exact_keys(payload, _PAYLOAD_FIELDS, "CaptureRequest payload")
        node_paths = payload["node_paths"]
        if type(node_paths) is not list:
            raise TypeError("CaptureRequest payload node_paths must be a list")
        return cls(
            request_id=envelope["request_id"],  # type: ignore[arg-type]
            deadline_ms=envelope["deadline_ms"],  # type: ignore[arg-type]
            scene_epoch=envelope["scene_epoch"],  # type: ignore[arg-type]
            node_paths=tuple(node_paths),  # type: ignore[arg-type]
            target_dir=payload["target_dir"],  # type: ignore[arg-type]
            artifact_id=payload["artifact_id"],  # type: ignore[arg-type]
            settings=CaptureSettings.from_dict(payload["settings"]),  # type: ignore[arg-type]
        )


# --------------------------------------------------------------------------
# framing report + result + response DTOs
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CaptureFramingReport:
    """The bounded deterministic framing evidence of one completed capture.

    Records how many of the at-most-two framing adjustments were used and the
    achieved margins/longest-axis/center-offset the acceptance rule verified.
    """

    adjustments_used: int
    margin_left: float
    margin_right: float
    margin_bottom: float
    margin_top: float
    longest_axis_ratio: float
    center_offset: float

    def __post_init__(self) -> None:
        _require_exact_int(self.adjustments_used, "CaptureFramingReport.adjustments_used")
        if not 0 <= self.adjustments_used <= 2:
            raise ValueError("CaptureFramingReport.adjustments_used must be in 0..2")
        for label, value in (
            ("margin_left", self.margin_left),
            ("margin_right", self.margin_right),
            ("margin_bottom", self.margin_bottom),
            ("margin_top", self.margin_top),
            ("longest_axis_ratio", self.longest_axis_ratio),
            ("center_offset", self.center_offset),
        ):
            _require_ratio(value, f"CaptureFramingReport.{label}")

    def to_dict(self) -> dict[str, object]:
        return {
            "adjustments_used": self.adjustments_used,
            "margin_left": self.margin_left,
            "margin_right": self.margin_right,
            "margin_bottom": self.margin_bottom,
            "margin_top": self.margin_top,
            "longest_axis_ratio": self.longest_axis_ratio,
            "center_offset": self.center_offset,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> CaptureFramingReport:
        value = _require_exact_dict(data, "CaptureFramingReport")
        _require_exact_keys(value, _FRAMING_FIELDS, "CaptureFramingReport")
        return cls(
            adjustments_used=value["adjustments_used"],  # type: ignore[arg-type]
            margin_left=value["margin_left"],  # type: ignore[arg-type]
            margin_right=value["margin_right"],  # type: ignore[arg-type]
            margin_bottom=value["margin_bottom"],  # type: ignore[arg-type]
            margin_top=value["margin_top"],  # type: ignore[arg-type]
            longest_axis_ratio=value["longest_axis_ratio"],  # type: ignore[arg-type]
            center_offset=value["center_offset"],  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class CaptureResult:
    """The bounded content-addressed reference of one completed capture.

    A result is returned only after the PNG was written, hashed, and
    atomically renamed inside the Runtime-owned target directory and the owned
    temp camera/ROP scope was destroyed; anything less surfaces as a
    structured bridge error, never as a partial result. Carries reference
    fields only — never image bytes.
    """

    artifact_id: str
    relative_path: str
    sha256: str
    media_type: str
    size_bytes: int
    framing: CaptureFramingReport

    def __post_init__(self) -> None:
        if type(self.artifact_id) is not str:
            raise TypeError("CaptureResult.artifact_id must be a string")
        require_id(self.artifact_id, IdKind.ARTIFACT)
        _require_png_file_name(self.relative_path, "CaptureResult.relative_path")
        _require_sha256(self.sha256, "CaptureResult.sha256")
        if type(self.media_type) is not str or self.media_type != PNG_MEDIA_TYPE:
            raise ValueError("CaptureResult.media_type must be image/png")
        _require_exact_int(self.size_bytes, "CaptureResult.size_bytes")
        if not 0 < self.size_bytes <= _MAX_PNG_BYTES:
            raise ValueError("CaptureResult.size_bytes must be in 1..67108864")
        if type(self.framing) is not CaptureFramingReport:
            raise TypeError("CaptureResult.framing must be a CaptureFramingReport")
        if len(canonical_json_dumps(self.to_dict()).encode("utf-8")) > _MAX_RESULT_BYTES:
            raise ValueError("CaptureResult exceeds the maximum result size")

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "relative_path": self.relative_path,
            "sha256": self.sha256,
            "media_type": self.media_type,
            "size_bytes": self.size_bytes,
            "framing": self.framing.to_dict(),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> CaptureResult:
        value = _require_exact_dict(data, "CaptureResult")
        _require_exact_keys(value, _RESULT_FIELDS, "CaptureResult")
        return cls(
            artifact_id=value["artifact_id"],  # type: ignore[arg-type]
            relative_path=value["relative_path"],  # type: ignore[arg-type]
            sha256=value["sha256"],  # type: ignore[arg-type]
            media_type=value["media_type"],  # type: ignore[arg-type]
            size_bytes=value["size_bytes"],  # type: ignore[arg-type]
            framing=CaptureFramingReport.from_dict(value["framing"]),  # type: ignore[arg-type]
        )


@dataclass(frozen=True, slots=True)
class CaptureResponse:
    """A parsed, validated ``capture.capture`` response envelope.

    Carries exactly one of a typed :class:`CaptureResult` or a
    :class:`BridgeError`. A malformed reference payload (bad digest, unknown
    field, oversized result) fails closed at parse time.
    """

    request_id: str
    result: CaptureResult | None
    error: BridgeError | None

    def __post_init__(self) -> None:
        _require_request_id(self.request_id, "CaptureResponse.request_id")
        if self.result is not None and type(self.result) is not CaptureResult:
            raise TypeError(
                "CaptureResponse.result must be a CaptureResult or None"
            )
        if self.error is not None and type(self.error) is not BridgeError:
            raise TypeError("CaptureResponse.error must be an exact BridgeError or None")
        if (self.result is None) == (self.error is None):
            raise ValueError("CaptureResponse must carry exactly one of result or error")

    def to_dict(self) -> dict[str, object]:
        if self.result is not None:
            return {
                "protocol": PROTOCOL,
                "kind": "response",
                "request_id": self.request_id,
                "ok": True,
                "result": self.result.to_dict(),
            }
        return {
            "protocol": PROTOCOL,
            "kind": "response",
            "request_id": self.request_id,
            "ok": False,
            "error": self.error.to_dict(),  # type: ignore[union-attr]
        }

    def to_json(self) -> str:
        return canonical_json_dumps(self.to_dict())

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> CaptureResponse:
        envelope = _require_exact_dict(data, "CaptureResponse envelope")
        if not _RESPONSE_REQUIRED_FIELDS.issubset(envelope.keys()):
            raise ValueError("CaptureResponse envelope is missing required fields")
        extra = set(envelope.keys()) - _RESPONSE_REQUIRED_FIELDS - {"result", "error"}
        if extra:
            raise ValueError("CaptureResponse envelope has unknown fields")
        if envelope["protocol"] != PROTOCOL:
            raise ValueError("CaptureResponse protocol must be eee.bridge/1")
        if envelope["kind"] != "response":
            raise ValueError("CaptureResponse kind must be response")
        ok = envelope["ok"]
        _require_exact_bool(ok, "CaptureResponse.ok")
        if ok is True:
            result = envelope.get("result")
            error = envelope.get("error")
            if result is None or error is not None:
                raise ValueError(
                    "CaptureResponse ok=true requires result and no error"
                )
            return cls(
                request_id=envelope["request_id"],  # type: ignore[arg-type]
                result=CaptureResult.from_dict(result),  # type: ignore[arg-type]
                error=None,
            )
        error = envelope.get("error")
        result = envelope.get("result")
        if error is None or result is not None:
            raise ValueError(
                "CaptureResponse ok=false requires error and no result"
            )
        return cls(
            request_id=envelope["request_id"],  # type: ignore[arg-type]
            result=None,
            error=BridgeError.from_dict(error),  # type: ignore[arg-type]
        )


# --------------------------------------------------------------------------
# JSON text entrypoints
# --------------------------------------------------------------------------


def parse_capture_request(raw: str | bytes) -> CaptureRequest:
    """Parse a ``capture.capture`` request from strict JSON text."""
    return CaptureRequest.from_dict(_load_strict_json(raw, "Capture request"))


def parse_capture_response(raw: str | bytes) -> CaptureResponse:
    """Parse a ``capture.capture`` response from strict JSON text."""
    return CaptureResponse.from_dict(_load_strict_json(raw, "Capture response"))
