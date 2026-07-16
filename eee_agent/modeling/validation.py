"""Deterministic modeling validation and bounded repair bookkeeping.

This module is deliberately provider-neutral.  It validates facts already
available at the compiler boundary and records unavailable post-Apply stages
explicitly; it never infers Houdini success from a model response.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from enum import StrEnum
from typing import Mapping

from eee_agent.changesets.contracts import ChangeSet, CreateNode
from eee_agent.modeling.catalog import NodeCatalog
from eee_agent.modeling.compiler import CompilationResult
from eee_agent.modeling.contracts import (
    ModelingBrief,
    ProceduralSpec,
    QualityProfile,
    RepairBudget,
    RepairTicket,
    ValidatorKind,
)
from eee_agent.runtime.models import canonical_json_dumps


class ValidationStatus(StrEnum):
    PASSED = "Passed"
    FAILED = "Failed"
    UNAVAILABLE = "Unavailable"
    STALE = "Stale"


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    """Bounded digest-addressed evidence attached to one validator result."""

    kind: str
    digest: str
    summary: str

    def __post_init__(self) -> None:
        if type(self.kind) is not str or not self.kind:
            raise ValueError("EvidenceRef.kind must be non-empty")
        if type(self.digest) is not str or len(self.digest) != 64:
            raise ValueError("EvidenceRef.digest must be a SHA-256 hex digest")
        try:
            int(self.digest, 16)
        except ValueError as exc:
            raise ValueError("EvidenceRef.digest must be a SHA-256 hex digest") from exc
        if type(self.summary) is not str or not self.summary:
            raise ValueError("EvidenceRef.summary must be non-empty")

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "digest": self.digest, "summary": self.summary}


@dataclass(frozen=True, slots=True)
class ValidatorResult:
    validator: ValidatorKind
    status: ValidationStatus
    code: str
    message: str
    evidence: tuple[EvidenceRef, ...] = ()

    def __post_init__(self) -> None:
        if type(self.validator) is not ValidatorKind:
            raise TypeError("ValidatorResult.validator must be ValidatorKind")
        if type(self.status) is not ValidationStatus:
            raise TypeError("ValidatorResult.status must be ValidationStatus")
        if type(self.code) is not str or not self.code:
            raise ValueError("ValidatorResult.code must be non-empty")
        if type(self.message) is not str or not self.message:
            raise ValueError("ValidatorResult.message must be non-empty")
        items = tuple(self.evidence)
        if len(items) > 16 or any(type(item) is not EvidenceRef for item in items):
            raise ValueError("ValidatorResult.evidence is invalid or too large")
        object.__setattr__(self, "evidence", items)

    def to_dict(self) -> dict[str, object]:
        return {
            "validator": self.validator.value,
            "status": self.status.value,
            "code": self.code,
            "message": self.message,
            "evidence": [item.to_dict() for item in self.evidence],
        }


@dataclass(frozen=True, slots=True)
class ValidationReport:
    spec_digest: str
    changeset_digest: str
    results: tuple[ValidatorResult, ...]
    repair_budget: RepairBudget
    schema_version: int = 1

    def __post_init__(self) -> None:
        if type(self.schema_version) is not int or self.schema_version != 1:
            raise ValueError("ValidationReport.schema_version must be 1")
        for label, value in (("spec_digest", self.spec_digest), ("changeset_digest", self.changeset_digest)):
            if type(value) is not str or len(value) != 64:
                raise ValueError(f"ValidationReport.{label} must be SHA-256")
            try:
                int(value, 16)
            except ValueError as exc:
                raise ValueError(f"ValidationReport.{label} must be SHA-256") from exc
        items = tuple(self.results)
        if items != tuple(sorted(items, key=lambda item: item.validator.value)):
            raise ValueError("ValidationReport.results must use deterministic validator order")
        if len({item.validator for item in items}) != len(items):
            raise ValueError("ValidationReport.results contains duplicate validators")
        if type(self.repair_budget) is not RepairBudget:
            raise TypeError("ValidationReport.repair_budget must be RepairBudget")
        object.__setattr__(self, "results", items)

    @property
    def digest(self) -> str:
        return hashlib.sha256(canonical_json_dumps(self.to_dict()).encode("utf-8")).hexdigest()

    @property
    def hard_failures(self) -> tuple[ValidatorResult, ...]:
        return tuple(item for item in self.results if item.status is ValidationStatus.FAILED)

    @property
    def complete(self) -> bool:
        return bool(self.results) and not self.hard_failures and all(
            item.status is ValidationStatus.PASSED for item in self.results
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "spec_digest": self.spec_digest,
            "changeset_digest": self.changeset_digest,
            "results": [item.to_dict() for item in self.results],
            "repair_budget": self.repair_budget.to_dict(),
        }


def _evidence(kind: str, payload: Mapping[str, object], summary: str) -> EvidenceRef:
    digest = hashlib.sha256(canonical_json_dumps(dict(payload)).encode("utf-8")).hexdigest()
    return EvidenceRef(kind, digest, summary)


def _graph_result(changeset: ChangeSet) -> ValidatorResult:
    operations = changeset.operations
    op_ids = [operation.op_id for operation in operations]
    refs = {ref.node_id: ref.path for ref in changeset.checkpoint_plan.nodes if ref.node_id}
    failures: list[str] = []
    if len(op_ids) != len(set(op_ids)):
        failures.append("duplicate operation IDs")
    created_ids: set[str] = set()
    for operation in operations:
        if isinstance(operation, CreateNode):
            parent = operation.parent
            if (
                parent.node_id is not None
                and parent.node_id not in refs
                and parent.node_id not in created_ids
            ):
                failures.append(f"checkpoint coverage missing for parent {parent.node_id}")
            created_ids.add(operation.node_id)
    payload = {"operation_ids": op_ids, "checkpoint_nodes": refs}
    if failures:
        return ValidatorResult(
            ValidatorKind.GRAPH,
            ValidationStatus.FAILED,
            "modeling.graph.invalid",
            "; ".join(failures),
            (_evidence("changeset.graph", payload, "Typed graph coverage check"),),
        )
    return ValidatorResult(
        ValidatorKind.GRAPH,
        ValidationStatus.PASSED,
        "modeling.graph.valid",
        "Typed operations and checkpoint coverage are deterministic.",
        (_evidence("changeset.graph", payload, "Typed graph coverage check"),),
    )


def validate_compilation(
    *,
    brief: ModelingBrief,
    spec: ProceduralSpec,
    quality_profile: QualityProfile,
    catalog: NodeCatalog,
    compilation: CompilationResult,
) -> ValidationReport:
    """Run deterministic pre-Apply validators over compiler facts.

    Runtime reads (cook/geometry/sensitivity/semantic/artifact) are marked
    ``Unavailable`` until a typed Bridge inspection supplies their evidence.
    This is intentional: an unavailable validator can never be mistaken for a
    passing quality gate.
    """
    for name, expected in (
        ("brief", ModelingBrief),
        ("spec", ProceduralSpec),
        ("quality_profile", QualityProfile),
        ("catalog", NodeCatalog),
        ("compilation", CompilationResult),
    ):
        if type(locals()[name]) is not expected:
            raise TypeError(f"{name} must be an exact {expected.__name__}")
    spec_ok = spec.brief_digest == brief.digest and spec.quality_profile_id == quality_profile.profile_id
    catalog_ok = compilation.catalog_digest == catalog.digest
    spec_result = ValidatorResult(
        ValidatorKind.SPEC_CONTRACT,
        ValidationStatus.PASSED if spec_ok and catalog_ok else ValidationStatus.FAILED,
        "modeling.spec.valid" if spec_ok and catalog_ok else "modeling.spec.invalid",
        "Brief, profile, and catalog digests agree." if spec_ok and catalog_ok else "Compiler inputs are not bound to trusted brief/profile/catalog.",
        (_evidence("spec.contract", {"brief": brief.digest, "spec": spec.digest, "catalog": catalog.digest, "compiled_catalog": compilation.catalog_digest}, "Compiler binding check"),),
    )
    unavailable = tuple(
        ValidatorResult(kind, ValidationStatus.UNAVAILABLE, "modeling.validation.unavailable", "Typed Bridge evidence is required for this validator.")
        for kind in ValidatorKind
        if kind not in (ValidatorKind.SPEC_CONTRACT, ValidatorKind.GRAPH)
    )
    results = tuple(sorted((spec_result, _graph_result(compilation.changeset), *unavailable), key=lambda item: item.validator.value))
    return ValidationReport(
        spec_digest=spec.digest,
        changeset_digest=compilation.changeset.digest,
        results=results,
        repair_budget=RepairBudget(max_attempts_per_stage=quality_profile.max_repairs_per_stage, attempts=()),
    )


__all__ = [
    "EvidenceRef",
    "ValidationReport",
    "ValidationStatus",
    "ValidatorResult",
    "validate_compilation",
]
