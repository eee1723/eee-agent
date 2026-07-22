from __future__ import annotations

import math

import pytest

from eee_agent.core.artifacts import ArtifactRef
from eee_agent.vision import (
    DeliveryEvaluation,
    FinalVisionDecision,
    NormalizedVisualReport,
    ProviderCapability,
    RedactedRawResponseRef,
    VisionRequest,
    VisionFailure,
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
    with pytest.raises(ValueError):
        VisionUnavailable("unavailable", "provider_missing", "missing")  # type: ignore[arg-type]


def test_failed_payload_is_typed_bounded_and_strict() -> None:
    failure = VisionFailure(
        VisionStatus.FAILED,
        "vision.provider_failed",
        "Visual evaluation provider failed.",
    )
    assert VisionFailure.from_dict(failure.to_dict()) == failure
    with pytest.raises(ValueError):
        VisionFailure(VisionStatus.UNAVAILABLE, "vision.failed", "failed")
    with pytest.raises(ValueError):
        VisionFailure(VisionStatus.FAILED, "x" * 65, "failed")
    payload = failure.to_dict()
    payload["raw_response"] = "must not pass"
    with pytest.raises(ValueError):
        VisionFailure.from_dict(payload)


def test_request_rejects_oversized_relative_artifact_path() -> None:
    with pytest.raises(ValueError):
        VisionRequest("vision-2", _artifact(relative_path="a/" + "x" * 512), "check")


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


def test_delivery_evaluation_is_bounded_and_contains_required_evidence() -> None:
    report = NormalizedVisualReport("ok", ("shape matches",), 0.8, True)
    decision = FinalVisionDecision(VisionStatus.COMPLETED, True, True, "ok")
    record = DeliveryEvaluation(
        brief="build a prop",
        spec="bounded modeling spec",
        changeset_digest="b" * 64,
        approval="approved",
        receipt="applied",
        validation_report=("graph:passed", "geometry:passed"),
        artifact_refs=(_artifact(),),
        artifact_status=("available",),
        knowledge_manifest_sha256="c" * 64,
        vision_status=VisionStatus.COMPLETED,
        vision_report=report,
        final_decision=decision,
        recovery_evidence=(),
    )
    payload = record.to_dict()
    assert payload["changeset_digest"] == "b" * 64
    assert payload["vision_status"] == "completed"
    with pytest.raises(ValueError):
        DeliveryEvaluation(
            brief="x" * 4097,
            spec=record.spec,
            changeset_digest=record.changeset_digest,
            approval=record.approval,
            receipt=record.receipt,
            validation_report=record.validation_report,
            artifact_refs=record.artifact_refs,
            artifact_status=record.artifact_status,
            knowledge_manifest_sha256=record.knowledge_manifest_sha256,
            vision_status=record.vision_status,
            vision_report=record.vision_report,
            final_decision=record.final_decision,
            recovery_evidence=record.recovery_evidence,
        )
    with pytest.raises(ValueError):
        DeliveryEvaluation(
            brief=record.brief,
            spec=record.spec,
            changeset_digest=record.changeset_digest,
            approval=record.approval,
            receipt=record.receipt,
            validation_report=record.validation_report,
            artifact_refs=(_artifact(relative_path="a/" + "x" * 512),),
            artifact_status=("available",),
            knowledge_manifest_sha256=record.knowledge_manifest_sha256,
            vision_status=record.vision_status,
            vision_report=record.vision_report,
            final_decision=record.final_decision,
            recovery_evidence=record.recovery_evidence,
        )


def test_delivery_evaluation_rejects_contradictory_vision_evidence() -> None:
    base = {
        "brief": "brief",
        "spec": "spec",
        "changeset_digest": "d" * 64,
        "approval": "approved",
        "receipt": "applied",
        "validation_report": ("geometry:passed",),
        "artifact_refs": (_artifact(),),
        "artifact_status": ("available",),
        "knowledge_manifest_sha256": None,
        "recovery_evidence": (),
    }
    failed_report = NormalizedVisualReport("mismatch", (), 0.8, False)
    with pytest.raises(ValueError):
        DeliveryEvaluation(
            **base,
            vision_status=VisionStatus.COMPLETED,
            vision_report=failed_report,
            final_decision=FinalVisionDecision(
                VisionStatus.COMPLETED, True, True, "contradiction"
            ),
        )
    with pytest.raises(ValueError):
        DeliveryEvaluation(
            **base,
            vision_status=VisionStatus.UNAVAILABLE,
            vision_report=failed_report,
            final_decision=FinalVisionDecision(
                VisionStatus.UNAVAILABLE, True, True, "provider unavailable"
            ),
        )
