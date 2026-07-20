from __future__ import annotations

import math

import pytest

from eee_agent.core.artifacts import ArtifactRef
from eee_agent.vision import (
    FinalVisionDecision,
    NormalizedVisualReport,
    ProviderCapability,
    RedactedRawResponseRef,
    VisionRequest,
    VisionStatus,
    VisionUnavailable,
)


def _artifact(**changes: object) -> ArtifactRef:
    values: dict[str, object] = {
        "artifact_id": "art_" + "1" * 32,
        "relative_path": "ses_" + "2" * 32 + "/run_" + "3" * 32 + "/preview.png",
        "sha256": "a" * 64,
        "media_type": "image/png",
        "size_bytes": 64,
    }
    values.update(changes)
    return ArtifactRef(**values)  # type: ignore[arg-type]


def test_capability_and_request_are_frozen_and_bounded() -> None:
    capability = ProviderCapability("vision", True, ("image/png",), 1024, None)
    request = VisionRequest("vision-1", _artifact(), "Check silhouette")
    assert capability.available and request.artifact.media_type == "image/png"
    with pytest.raises((AttributeError, TypeError)):
        request.instruction = "changed"  # type: ignore[misc]
    with pytest.raises(ValueError):
        VisionRequest("vision-2", _artifact(media_type="text/plain"), "check")
    with pytest.raises(ValueError):
        VisionRequest("vision-2", _artifact(), "x" * 4097)


def test_from_dict_rejects_unknown_fields_and_absolute_artifacts() -> None:
    payload = {
        "request_id": "vision-1",
        "artifact": _artifact().to_dict(),
        "instruction": "check",
        "secret": "must-not-pass",
    }
    with pytest.raises(ValueError):
        VisionRequest.from_dict(payload)
    bad = dict(_artifact().to_dict())
    bad["relative_path"] = "C:/Users/EEE/secret.png"
    with pytest.raises(ValueError):
        VisionRequest.from_dict(
            {"request_id": "vision-1", "artifact": bad, "instruction": "check"}
        )


@pytest.mark.parametrize("confidence", [math.nan, math.inf, -0.1, 1.1, 1])
def test_report_rejects_nonfinite_or_wrong_type_confidence(confidence: object) -> None:
    with pytest.raises(ValueError):
        NormalizedVisualReport("summary", (), confidence, True)  # type: ignore[arg-type]


def test_report_and_unavailable_payloads_are_bounded() -> None:
    report = NormalizedVisualReport("ok", ("silhouette matches",), 0.9, True)
    assert report.advisory_passed
    with pytest.raises(ValueError):
        NormalizedVisualReport("ok", tuple("x" for _ in range(33)), 0.5, True)
    with pytest.raises(ValueError):
        VisionUnavailable(VisionStatus.COMPLETED, "provider_missing", "missing")


def test_advisory_decision_cannot_override_deterministic_failure() -> None:
    with pytest.raises(ValueError):
        FinalVisionDecision(VisionStatus.COMPLETED, True, False, "looks good")
    decision = FinalVisionDecision(VisionStatus.COMPLETED, False, False, "validator failed")
    assert not decision.accepted


def test_every_wire_contract_rejects_unknown_fields() -> None:
    raw = RedactedRawResponseRef(_artifact())
    payload = raw.to_dict()
    payload["local_path"] = "C:/secret.json"
    with pytest.raises(ValueError):
        RedactedRawResponseRef.from_dict(payload)

    decision = FinalVisionDecision(VisionStatus.UNAVAILABLE, False, True, "optional")
    decision_payload = decision.to_dict()
    decision_payload["provider_raw"] = "unbounded"
    with pytest.raises(ValueError):
        FinalVisionDecision.from_dict(decision_payload)
