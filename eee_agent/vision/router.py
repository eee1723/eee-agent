"""Advisory Vision routing over verified Runtime artifact bytes."""

from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Protocol, runtime_checkable

from eee_agent.config import (
    DEFAULT_VISION_TIMEOUT_SECONDS,
    MAX_VISION_TIMEOUT_SECONDS,
    MIN_VISION_TIMEOUT_SECONDS,
)
from eee_agent.runtime.artifacts import ArtifactStore
from eee_agent.vision.contracts import (
    FinalVisionDecision,
    NormalizedVisualReport,
    ProviderCapability,
    VisionRequest,
    VisionFailure,
    VisionStatus,
    VisionUnavailable,
)


@runtime_checkable
class VisionProvider(Protocol):
    async def capability(self) -> ProviderCapability: ...

    async def evaluate(
        self, request: VisionRequest, image_bytes: bytes
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True, slots=True)
class VisionOutcome:
    decision: FinalVisionDecision
    report: NormalizedVisualReport | None = None
    unavailable: VisionUnavailable | None = None
    failure: VisionFailure | None = None

    def __post_init__(self) -> None:
        if type(self.decision) is not FinalVisionDecision:
            raise ValueError("decision must be an exact FinalVisionDecision")
        if self.report is not None and type(self.report) is not NormalizedVisualReport:
            raise ValueError("report must be an exact NormalizedVisualReport")
        if self.unavailable is not None and type(self.unavailable) is not VisionUnavailable:
            raise ValueError("unavailable must be an exact VisionUnavailable")
        if self.failure is not None and type(self.failure) is not VisionFailure:
            raise ValueError("failure must be an exact VisionFailure")
        status = self.decision.status
        if status is VisionStatus.COMPLETED:
            valid = self.report is not None and self.unavailable is None and self.failure is None
        elif status in {VisionStatus.UNAVAILABLE, VisionStatus.WAIVED}:
            valid = (
                self.report is None
                and self.unavailable is not None
                and self.unavailable.status is status
                and self.failure is None
            )
        elif status is VisionStatus.FAILED:
            valid = (
                self.report is None
                and self.unavailable is None
                and self.failure is not None
                and self.failure.status is status
            )
        else:  # pragma: no cover - VisionStatus is exhaustive
            valid = False
        if not valid:
            raise ValueError("vision outcome evidence does not match its status")


def _unavailable(
    code: str, message: str, *, deterministic_valid: bool, waived: bool = False
) -> VisionOutcome:
    status = VisionStatus.WAIVED if waived else VisionStatus.UNAVAILABLE
    unavailable = VisionUnavailable(status, code, message)
    return VisionOutcome(
        decision=FinalVisionDecision(
            status,
            deterministic_valid,
            deterministic_valid,
            message,
        ),
        unavailable=unavailable,
    )


def _failed(
    code: str,
    message: str,
    *,
    deterministic_valid: bool,
) -> VisionOutcome:
    failure = VisionFailure(VisionStatus.FAILED, code, message)
    return VisionOutcome(
        decision=FinalVisionDecision(
            VisionStatus.FAILED,
            False,
            deterministic_valid,
            message,
        ),
        failure=failure,
    )


def _read_bounded(path: Path, maximum: int) -> bytes:
    """Read at most ``maximum + 1`` bytes from an already-rooted artifact."""
    with path.open("rb") as stream:
        return stream.read(maximum + 1)


class VisionRouter:
    """Resolve a durable artifact and give only its verified bytes to a provider."""

    def __init__(
        self,
        artifacts: ArtifactStore,
        provider: VisionProvider | None,
        *,
        timeout_seconds: float = DEFAULT_VISION_TIMEOUT_SECONDS,
    ) -> None:
        if type(artifacts) is not ArtifactStore:
            raise TypeError("artifacts must be an exact ArtifactStore")
        if provider is not None and not isinstance(provider, VisionProvider):
            raise TypeError("provider must implement VisionProvider")
        if (
            type(timeout_seconds) is not float
            or not MIN_VISION_TIMEOUT_SECONDS
            <= timeout_seconds
            <= MAX_VISION_TIMEOUT_SECONDS
        ):
            raise ValueError("timeout_seconds is invalid")
        self._artifacts = artifacts
        self._provider = provider
        self._timeout = timeout_seconds

    async def evaluate(
        self,
        request: VisionRequest,
        *,
        deterministic_valid: bool,
        waived: bool = False,
    ) -> VisionOutcome:
        if type(request) is not VisionRequest:
            raise TypeError("request must be an exact VisionRequest")
        if type(deterministic_valid) is not bool or type(waived) is not bool:
            raise TypeError("vision decision flags must be bool")
        if waived:
            return _unavailable(
                "vision.user_waived",
                "Visual evaluation was waived by the user.",
                deterministic_valid=deterministic_valid,
                waived=True,
            )
        if self._provider is None:
            return _unavailable(
                "vision.provider_unavailable",
                "Visual evaluation provider is unavailable.",
                deterministic_valid=deterministic_valid,
            )

        try:
            capability = await asyncio.wait_for(
                self._provider.capability(), timeout=self._timeout
            )
        except asyncio.TimeoutError:
            return _unavailable(
                "vision.provider_timeout",
                "Visual evaluation provider timed out.",
                deterministic_valid=deterministic_valid,
            )
        except Exception:
            return _unavailable(
                "vision.provider_unavailable",
                "Visual evaluation provider is unavailable.",
                deterministic_valid=deterministic_valid,
            )
        if type(capability) is not ProviderCapability:
            return _unavailable(
                "vision.provider_invalid",
                "Visual evaluation provider capability is invalid.",
                deterministic_valid=deterministic_valid,
            )
        if not capability.available:
            return _unavailable(
                capability.reason_code or "vision.provider_unavailable",
                "Visual evaluation provider is unavailable.",
                deterministic_valid=deterministic_valid,
            )
        if (
            request.artifact.media_type not in capability.media_types
            or request.artifact.size_bytes > capability.max_image_bytes
        ):
            return _unavailable(
                "vision.artifact_unsupported",
                "The artifact is not supported by the visual provider.",
                deterministic_valid=deterministic_valid,
            )

        stored = await self._artifacts.get(request.artifact.artifact_id)
        if stored is None or stored != request.artifact:
            return _unavailable(
                "vision.artifact_unavailable",
                "The visual artifact is missing or unavailable.",
                deterministic_valid=deterministic_valid,
            )
        path = self._artifacts.path_for(stored)
        try:
            resolved_root = self._artifacts.root.resolve(strict=True)
            resolved_path = path.resolve(strict=True)
            if not resolved_path.is_relative_to(resolved_root) or not resolved_path.is_file():
                raise OSError("artifact escaped its managed root")
            image_bytes = await asyncio.to_thread(
                _read_bounded, resolved_path, capability.max_image_bytes
            )
        except OSError:
            return _unavailable(
                "vision.artifact_unavailable",
                "The visual artifact is missing or unavailable.",
                deterministic_valid=deterministic_valid,
            )
        if (
            len(image_bytes) != stored.size_bytes
            or len(image_bytes) > capability.max_image_bytes
            or hashlib.sha256(image_bytes).hexdigest() != stored.sha256
        ):
            return _unavailable(
                "vision.artifact_integrity_failed",
                "The visual artifact failed its integrity check.",
                deterministic_valid=deterministic_valid,
            )

        try:
            payload = await asyncio.wait_for(
                self._provider.evaluate(request, image_bytes), timeout=self._timeout
            )
            report = NormalizedVisualReport.from_dict(payload)
        except asyncio.TimeoutError:
            return _failed(
                "vision.provider_timeout",
                "Visual evaluation provider timed out.",
                deterministic_valid=deterministic_valid,
            )
        except (TypeError, ValueError):
            return _failed(
                "vision.response_invalid",
                "Visual evaluation provider response is invalid.",
                deterministic_valid=deterministic_valid,
            )
        except Exception:
            return _failed(
                "vision.provider_failed",
                "Visual evaluation provider failed.",
                deterministic_valid=deterministic_valid,
            )

        accepted = deterministic_valid and report.advisory_passed
        return VisionOutcome(
            decision=FinalVisionDecision(
                VisionStatus.COMPLETED,
                accepted,
                deterministic_valid,
                report.summary,
            ),
            report=report,
        )
