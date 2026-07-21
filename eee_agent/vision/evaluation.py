"""Bounded delivery record for deterministic and advisory evaluation evidence."""

from __future__ import annotations

import re
from dataclasses import dataclass

from eee_agent.core.artifacts import ArtifactRef
from eee_agent.vision.contracts import (
    FinalVisionDecision,
    NormalizedVisualReport,
    VisionStatus,
)


_DIGEST = re.compile(r"^[0-9a-f]{64}$")


def _text(value: object, name: str, maximum: int) -> None:
    if type(value) is not str or not value or len(value) > maximum:
        raise ValueError(f"{name} must be a bounded string")


def _evidence(values: object, name: str, maximum: int) -> None:
    if type(values) is not tuple or len(values) > maximum:
        raise ValueError(f"{name} must be a bounded tuple")
    for value in values:
        _text(value, name, 512)


@dataclass(frozen=True, slots=True)
class DeliveryEvaluation:
    brief: str
    spec: str
    changeset_digest: str
    approval: str
    receipt: str
    validation_report: tuple[str, ...]
    artifact_refs: tuple[ArtifactRef, ...]
    artifact_status: tuple[str, ...]
    knowledge_manifest_sha256: str | None
    vision_status: VisionStatus
    vision_report: NormalizedVisualReport | None
    final_decision: FinalVisionDecision
    recovery_evidence: tuple[str, ...]

    def __post_init__(self) -> None:
        _text(self.brief, "brief", 4096)
        _text(self.spec, "spec", 4096)
        if type(self.changeset_digest) is not str or not _DIGEST.fullmatch(
            self.changeset_digest
        ):
            raise ValueError("changeset_digest must be a lowercase sha256")
        _text(self.approval, "approval", 512)
        _text(self.receipt, "receipt", 512)
        _evidence(self.validation_report, "validation_report", 64)
        if type(self.artifact_refs) is not tuple or len(self.artifact_refs) > 16:
            raise ValueError("artifact_refs must be a bounded tuple")
        if any(type(item) is not ArtifactRef for item in self.artifact_refs):
            raise ValueError("artifact_refs must contain exact ArtifactRef values")
        if any(
            len(item.relative_path) > 512
            or not 1 <= item.size_bytes <= 16_777_216
            for item in self.artifact_refs
        ):
            raise ValueError("artifact refs exceed the delivery budget")
        _evidence(self.artifact_status, "artifact_status", 16)
        if len(self.artifact_status) != len(self.artifact_refs):
            raise ValueError("artifact status must match artifact refs")
        if self.knowledge_manifest_sha256 is not None and (
            type(self.knowledge_manifest_sha256) is not str
            or not _DIGEST.fullmatch(self.knowledge_manifest_sha256)
        ):
            raise ValueError("knowledge manifest must be a lowercase sha256")
        if type(self.vision_status) is not VisionStatus:
            raise ValueError("vision_status must be an exact VisionStatus")
        if self.vision_report is not None and type(self.vision_report) is not NormalizedVisualReport:
            raise ValueError("vision_report must be an exact NormalizedVisualReport")
        if type(self.final_decision) is not FinalVisionDecision:
            raise ValueError("final_decision must be an exact FinalVisionDecision")
        if self.final_decision.status is not self.vision_status:
            raise ValueError("vision status and final decision must agree")
        if self.vision_status is VisionStatus.COMPLETED:
            if self.vision_report is None:
                raise ValueError("completed vision evaluation requires a report")
            expected_accepted = (
                self.final_decision.deterministic_valid
                and self.vision_report.advisory_passed
            )
            if self.final_decision.accepted is not expected_accepted:
                raise ValueError("completed vision decision contradicts its report")
        elif self.vision_report is not None:
            raise ValueError("non-completed vision evaluation cannot carry a report")
        if (
            self.vision_status is VisionStatus.FAILED
            and self.final_decision.accepted
        ):
            raise ValueError("failed vision evaluation cannot be accepted")
        _evidence(self.recovery_evidence, "recovery_evidence", 32)

    def to_dict(self) -> dict[str, object]:
        return {
            "brief": self.brief,
            "spec": self.spec,
            "changeset_digest": self.changeset_digest,
            "approval": self.approval,
            "receipt": self.receipt,
            "validation_report": list(self.validation_report),
            "artifact_refs": [item.to_dict() for item in self.artifact_refs],
            "artifact_status": list(self.artifact_status),
            "knowledge_manifest_sha256": self.knowledge_manifest_sha256,
            "vision_status": self.vision_status.value,
            "vision_report": (
                None if self.vision_report is None else self.vision_report.to_dict()
            ),
            "final_decision": self.final_decision.to_dict(),
            "recovery_evidence": list(self.recovery_evidence),
        }
