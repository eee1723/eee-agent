"""Strict, bounded contracts for advisory visual evaluation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from typing import ClassVar, Mapping

from eee_agent.core.artifacts import ArtifactRef


class VisionStatus(str, Enum):
    COMPLETED = "completed"
    UNAVAILABLE = "unavailable"
    WAIVED = "waived"
    FAILED = "failed"


def _text(value: object, name: str, *, maximum: int, allow_empty: bool = False) -> str:
    if type(value) is not str or (not allow_empty and not value) or len(value) > maximum:
        raise ValueError(f"{name} must be a bounded string")
    return value


def _fields(payload: Mapping[str, object], expected: frozenset[str]) -> None:
    if type(payload) is not dict or frozenset(payload) != expected:
        raise ValueError("vision payload fields are invalid")


class _StrictContract:
    _FIELDS: ClassVar[frozenset[str]]

    @classmethod
    def _payload(cls, payload: Mapping[str, object]) -> Mapping[str, object]:
        _fields(payload, cls._FIELDS)
        return payload


@dataclass(frozen=True, slots=True)
class ProviderCapability(_StrictContract):
    provider_id: str
    available: bool
    media_types: tuple[str, ...]
    max_image_bytes: int
    reason_code: str | None

    _FIELDS = frozenset(
        {"provider_id", "available", "media_types", "max_image_bytes", "reason_code"}
    )

    def __post_init__(self) -> None:
        _text(self.provider_id, "provider_id", maximum=64)
        if type(self.available) is not bool:
            raise ValueError("available must be a bool")
        if type(self.media_types) is not tuple or not 1 <= len(self.media_types) <= 8:
            raise ValueError("media_types must be a bounded tuple")
        allowed = {"image/png", "image/jpeg", "image/webp"}
        if any(type(item) is not str or item not in allowed for item in self.media_types):
            raise ValueError("unsupported vision media type")
        if type(self.max_image_bytes) is not int or not 1 <= self.max_image_bytes <= 16_777_216:
            raise ValueError("max_image_bytes is invalid")
        if self.reason_code is not None:
            _text(self.reason_code, "reason_code", maximum=64)
        if self.available == (self.reason_code is not None):
            raise ValueError("capability reason is inconsistent")

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "ProviderCapability":
        data = cls._payload(payload)
        media = data["media_types"]
        if type(media) is not list:
            raise ValueError("media_types must be a list")
        return cls(
            provider_id=data["provider_id"],  # type: ignore[arg-type]
            available=data["available"],  # type: ignore[arg-type]
            media_types=tuple(media),
            max_image_bytes=data["max_image_bytes"],  # type: ignore[arg-type]
            reason_code=data["reason_code"],  # type: ignore[arg-type]
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "available": self.available,
            "media_types": list(self.media_types),
            "max_image_bytes": self.max_image_bytes,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class VisionRequest(_StrictContract):
    request_id: str
    artifact: ArtifactRef
    instruction: str

    _FIELDS = frozenset({"request_id", "artifact", "instruction"})

    def __post_init__(self) -> None:
        _text(self.request_id, "request_id", maximum=128)
        if type(self.artifact) is not ArtifactRef:
            raise ValueError("artifact must be an exact ArtifactRef")
        if self.artifact.media_type not in {"image/png", "image/jpeg", "image/webp"}:
            raise ValueError("artifact media type is not visual")
        if not 1 <= self.artifact.size_bytes <= 16_777_216:
            raise ValueError("artifact size is outside the vision budget")
        _text(self.instruction, "instruction", maximum=4096)

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "VisionRequest":
        data = cls._payload(payload)
        artifact = data["artifact"]
        if type(artifact) is not dict:
            raise ValueError("artifact must be an object")
        expected = frozenset(
            {"artifact_id", "relative_path", "sha256", "media_type", "size_bytes", "schema_version"}
        )
        _fields(artifact, expected)
        return cls(
            request_id=data["request_id"],  # type: ignore[arg-type]
            artifact=ArtifactRef(**artifact),  # type: ignore[arg-type]
            instruction=data["instruction"],  # type: ignore[arg-type]
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "request_id": self.request_id,
            "artifact": self.artifact.to_dict(),
            "instruction": self.instruction,
        }


@dataclass(frozen=True, slots=True)
class VisionUnavailable(_StrictContract):
    status: VisionStatus
    reason_code: str
    message: str

    _FIELDS = frozenset({"status", "reason_code", "message"})

    def __post_init__(self) -> None:
        if self.status not in {VisionStatus.UNAVAILABLE, VisionStatus.WAIVED}:
            raise ValueError("unavailable status is invalid")
        _text(self.reason_code, "reason_code", maximum=64)
        _text(self.message, "message", maximum=512)

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "VisionUnavailable":
        data = cls._payload(payload)
        try:
            status = VisionStatus(data["status"])
        except (TypeError, ValueError):
            raise ValueError("vision status is invalid") from None
        return cls(status, data["reason_code"], data["message"])  # type: ignore[arg-type]

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "reason_code": self.reason_code,
            "message": self.message,
        }


@dataclass(frozen=True, slots=True)
class NormalizedVisualReport(_StrictContract):
    summary: str
    observations: tuple[str, ...]
    confidence: float
    advisory_passed: bool

    _FIELDS = frozenset({"summary", "observations", "confidence", "advisory_passed"})

    def __post_init__(self) -> None:
        _text(self.summary, "summary", maximum=2048)
        if type(self.observations) is not tuple or len(self.observations) > 32:
            raise ValueError("observations must be a bounded tuple")
        for item in self.observations:
            _text(item, "observation", maximum=512)
        if type(self.confidence) is not float or not math.isfinite(self.confidence) or not 0 <= self.confidence <= 1:
            raise ValueError("confidence must be finite and in 0..1")
        if type(self.advisory_passed) is not bool:
            raise ValueError("advisory_passed must be a bool")

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "NormalizedVisualReport":
        data = cls._payload(payload)
        observations = data["observations"]
        if type(observations) is not list:
            raise ValueError("observations must be a list")
        return cls(
            data["summary"], tuple(observations), data["confidence"], data["advisory_passed"]  # type: ignore[arg-type]
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "summary": self.summary,
            "observations": list(self.observations),
            "confidence": self.confidence,
            "advisory_passed": self.advisory_passed,
        }


@dataclass(frozen=True, slots=True)
class RedactedRawResponseRef(_StrictContract):
    artifact: ArtifactRef
    redacted: bool = True

    _FIELDS = frozenset({"artifact", "redacted"})

    def __post_init__(self) -> None:
        if type(self.artifact) is not ArtifactRef or type(self.redacted) is not bool or not self.redacted:
            raise ValueError("raw response reference must be redacted")

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "RedactedRawResponseRef":
        data = cls._payload(payload)
        artifact = data["artifact"]
        if type(artifact) is not dict:
            raise ValueError("artifact must be an object")
        expected = frozenset(
            {"artifact_id", "relative_path", "sha256", "media_type", "size_bytes", "schema_version"}
        )
        _fields(artifact, expected)
        return cls(ArtifactRef(**artifact), data["redacted"])  # type: ignore[arg-type]

    def to_dict(self) -> dict[str, object]:
        return {"artifact": self.artifact.to_dict(), "redacted": self.redacted}


@dataclass(frozen=True, slots=True)
class FinalVisionDecision(_StrictContract):
    status: VisionStatus
    accepted: bool
    deterministic_valid: bool
    summary: str

    _FIELDS = frozenset({"status", "accepted", "deterministic_valid", "summary"})

    def __post_init__(self) -> None:
        if type(self.status) is not VisionStatus:
            raise ValueError("status must be VisionStatus")
        if type(self.accepted) is not bool or type(self.deterministic_valid) is not bool:
            raise ValueError("decision flags must be bool")
        _text(self.summary, "summary", maximum=1024)
        if not self.deterministic_valid and self.accepted:
            raise ValueError("advisory vision cannot override deterministic failure")

    @classmethod
    def from_dict(cls, payload: Mapping[str, object]) -> "FinalVisionDecision":
        data = cls._payload(payload)
        try:
            status = VisionStatus(data["status"])
        except (TypeError, ValueError):
            raise ValueError("vision status is invalid") from None
        return cls(
            status,
            data["accepted"],  # type: ignore[arg-type]
            data["deterministic_valid"],  # type: ignore[arg-type]
            data["summary"],  # type: ignore[arg-type]
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "status": self.status.value,
            "accepted": self.accepted,
            "deterministic_valid": self.deterministic_valid,
            "summary": self.summary,
        }
