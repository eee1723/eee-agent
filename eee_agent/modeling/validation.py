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

from eee_agent.changesets.contracts import ChangeSet, ConnectInput, CreateNode, SetParm
from eee_agent.core.artifacts import ArtifactRef
from eee_agent.houdini_bridge.capture import CaptureFramingReport
from eee_agent.houdini_bridge.contracts import SceneQueryResult
from eee_agent.houdini_bridge.sensitivity import (
    SensitivitySampleResult,
    SensitivitySampleTarget,
)
from eee_agent.modeling.catalog import NodeCatalog
from eee_agent.modeling.compiler import CompilationResult
from eee_agent.modeling.framing import FramingTolerance
from eee_agent.modeling.golden_cases import GoldenCase
from eee_agent.modeling.contracts import (
    ModelingBrief,
    ProceduralSpec,
    QualityProfile,
    RepairBudget,
    RepairStatus,
    RepairTicket,
    ValidatorKind,
)
from eee_agent.runtime.models import canonical_json_dumps, thaw_json


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


def validate_applied_scene(
    *, changeset: ChangeSet, query: SceneQueryResult
) -> tuple[ValidatorResult, ValidatorResult]:
    """Return Cook and Geometry results from bounded post-Apply scene facts."""
    if type(changeset) is not ChangeSet:
        raise TypeError("changeset must be an exact ChangeSet")
    if type(query) is not SceneQueryResult:
        raise TypeError("query must be an exact SceneQueryResult")
    evidence = _evidence(
        "scene.geometry",
        query.to_dict(),
        "Read-only Cook and geometry evidence",
    )
    container_ids = {
        operation.parent.node_id
        for operation in changeset.operations
        if isinstance(operation, CreateNode)
        and operation.parent.node_id is not None
    }
    expected_paths = {
        node.path
        for node in changeset.affected_nodes
        if node.node_id not in container_ids
    }
    actual_by_path = {node.path: node for node in query.nodes}
    stale = (
        query.binding.instance_id != changeset.scene_binding.instance_id
        or query.binding.scene_epoch != changeset.scene_binding.scene_epoch
    )
    if stale:
        return (
            ValidatorResult(
                ValidatorKind.COOK,
                ValidationStatus.STALE,
                "modeling.validation.stale",
                "Scene evidence no longer matches the approved scene binding.",
                (evidence,),
            ),
            ValidatorResult(
                ValidatorKind.GEOMETRY,
                ValidationStatus.STALE,
                "modeling.validation.stale",
                "Scene evidence no longer matches the approved scene binding.",
                (evidence,),
            ),
        )

    missing = sorted(expected_paths - set(actual_by_path))
    unreadable = sorted(
        path
        for path in expected_paths & set(actual_by_path)
        if actual_by_path[path].geometry_stats is None
    )
    cook = ValidatorResult(
        ValidatorKind.COOK,
        ValidationStatus.FAILED if missing or unreadable else ValidationStatus.PASSED,
        "modeling.cook.failed" if missing or unreadable else "modeling.cook.valid",
        (
            "One or more compiled nodes are missing or have unreadable geometry."
            if missing or unreadable
            else "Every compiled node returned readable geometry facts."
        ),
        (evidence,),
    )
    source_ids = {
        operation.source.node_id
        for operation in changeset.operations
        if isinstance(operation, ConnectInput)
        and operation.source.node_id is not None
    }
    terminal_paths = {
        node.path
        for node in changeset.affected_nodes
        if node.node_id not in source_ids and node.node_id not in container_ids
    }
    invalid_terminal: list[str] = []
    for path in sorted(terminal_paths):
        node = actual_by_path.get(path)
        stats = None if node is None else node.geometry_stats
        if stats is None:
            invalid_terminal.append(path)
            continue
        points = stats.get("points")
        primitives = stats.get("primitives")
        bbox = stats.get("bbox")
        if (
            type(points) is not int
            or type(primitives) is not int
            or points <= 0
            or primitives <= 0
            or not isinstance(bbox, Mapping)
        ):
            invalid_terminal.append(path)
    geometry = ValidatorResult(
        ValidatorKind.GEOMETRY,
        ValidationStatus.FAILED if invalid_terminal else ValidationStatus.PASSED,
        (
            "modeling.geometry.empty_or_invalid"
            if invalid_terminal
            else "modeling.geometry.valid"
        ),
        (
            "Terminal model outputs must contain non-empty bounded geometry."
            if invalid_terminal
            else "Terminal model outputs contain non-empty bounded geometry."
        ),
        (evidence,),
    )
    return cook, geometry


def _geometry_evidence_digest(query: SceneQueryResult) -> str:
    payload = {
        node.path: thaw_json(node.geometry_stats)
        for node in sorted(query.nodes, key=lambda item: item.path)
    }
    return _evidence("scene.sensitivity", payload, "Geometry sensitivity snapshot").digest


def derive_sensitivity_sample_plan(
    *,
    changeset: ChangeSet,
    catalog: NodeCatalog,
    quality_profile: QualityProfile,
) -> tuple[SensitivitySampleTarget, ...]:
    """Derive the bounded deterministic sample targets for one applied ChangeSet.

    Only parameters the compiled ChangeSet itself sets (``SetParm``) and that
    the trusted catalog defines as safe literal numeric parameters of the
    target node type are sampled — the spec/catalog boundary is the only
    source of sample authority, so sampling runs only when the changeset/spec
    defines sensitivity sample parameters. Each sample value is a
    deterministic +1 perturbation of the compiled value. The plan is empty
    when the profile does not enable ``ParameterSensitivity`` or no eligible
    parameter exists, and is capped by ``QualityProfile.max_parameter_samples``
    and the validator's 16-sample bound.
    """
    if type(changeset) is not ChangeSet:
        raise TypeError("changeset must be an exact ChangeSet")
    if type(catalog) is not NodeCatalog:
        raise TypeError("catalog must be an exact NodeCatalog")
    if type(quality_profile) is not QualityProfile:
        raise TypeError("quality_profile must be an exact QualityProfile")
    if ValidatorKind.PARAMETER_SENSITIVITY not in quality_profile.validators:
        return ()
    limit = min(quality_profile.max_parameter_samples, 16)
    definitions = catalog.by_type
    targets: list[SensitivitySampleTarget] = []
    seen: set[tuple[str, str]] = set()
    for operation in changeset.operations:
        if not isinstance(operation, SetParm):
            continue
        key = (operation.target.path, operation.parm_name)
        if key in seen:
            continue
        seen.add(key)
        value = operation.value
        sample_value: int | float
        if type(value) is int:
            sample_value = value + 1
        elif type(value) is float:
            sample_value = value + 1.0
        else:
            continue
        definition = definitions.get(operation.target.expected_type)
        if definition is None:
            continue
        parm = definition.parameters_by_name.get(operation.parm_name)
        if (
            parm is None
            or type(parm.default_value) is bool
            or type(parm.default_value) not in (int, float)
        ):
            continue
        targets.append(
            SensitivitySampleTarget(
                node_id=operation.target.node_id,
                path=operation.target.path,
                parm_name=operation.parm_name,
                value=sample_value,
            )
        )
        if len(targets) >= limit:
            break
    return tuple(targets)


def validate_parameter_sensitivity(
    *,
    changeset: ChangeSet,
    baseline: SceneQueryResult,
    samples: tuple[SceneQueryResult, ...],
    restored: SceneQueryResult,
) -> ValidatorResult:
    """Verify a bounded sample changed geometry and exact restoration occurred."""
    if type(changeset) is not ChangeSet:
        raise TypeError("changeset must be an exact ChangeSet")
    if type(baseline) is not SceneQueryResult or type(restored) is not SceneQueryResult:
        raise TypeError("baseline and restored must be exact SceneQueryResult values")
    items = tuple(samples)
    if not items or len(items) > 16 or any(type(item) is not SceneQueryResult for item in items):
        raise ValueError("samples must contain 1..16 SceneQueryResult values")
    expected_binding = changeset.scene_binding
    all_queries = (baseline, *items, restored)
    if any(
        query.binding.instance_id != expected_binding.instance_id
        or query.binding.scene_epoch != expected_binding.scene_epoch
        for query in all_queries
    ):
        return ValidatorResult(
            ValidatorKind.PARAMETER_SENSITIVITY,
            ValidationStatus.STALE,
            "modeling.sensitivity.stale",
            "One or more sensitivity snapshots no longer match the approved scene binding.",
        )
    baseline_digest = _geometry_evidence_digest(baseline)
    restored_digest = _geometry_evidence_digest(restored)
    sample_digests = tuple(_geometry_evidence_digest(item) for item in items)
    payload = {
        "baseline": baseline_digest,
        "samples": list(sample_digests),
        "restored": restored_digest,
    }
    evidence = _evidence(
        "scene.sensitivity",
        payload,
        "Bounded parameter sample and exact restoration evidence",
    )
    if restored_digest != baseline_digest:
        return ValidatorResult(
            ValidatorKind.PARAMETER_SENSITIVITY,
            ValidationStatus.FAILED,
            "modeling.sensitivity.restore_failed",
            "The scene did not return exactly to its baseline geometry evidence.",
            (evidence,),
        )
    if all(sample_digest == baseline_digest for sample_digest in sample_digests):
        return ValidatorResult(
            ValidatorKind.PARAMETER_SENSITIVITY,
            ValidationStatus.FAILED,
            "modeling.sensitivity.insensitive",
            "The bounded parameter samples did not change geometry evidence.",
            (evidence,),
        )
    return ValidatorResult(
        ValidatorKind.PARAMETER_SENSITIVITY,
        ValidationStatus.PASSED,
        "modeling.sensitivity.valid",
        "A bounded parameter sample changed geometry and exact restoration passed.",
        (evidence,),
    )


def validate_golden_case_semantics(
    *, case: GoldenCase, changeset: ChangeSet, query: SceneQueryResult
) -> ValidatorResult:
    """Check deterministic node names/types for one trusted Golden Case."""
    if type(case) is not GoldenCase:
        raise TypeError("case must be an exact GoldenCase")
    if type(changeset) is not ChangeSet or type(query) is not SceneQueryResult:
        raise TypeError("changeset and query must be exact typed values")
    evidence = _evidence(
        "scene.semantic",
        {"case_id": case.case_id, "query": query.to_dict()},
        "Golden Case semantic evidence",
    )
    if (
        query.binding.instance_id != changeset.scene_binding.instance_id
        or query.binding.scene_epoch != changeset.scene_binding.scene_epoch
    ):
        return ValidatorResult(
            ValidatorKind.SEMANTIC,
            ValidationStatus.STALE,
            "modeling.semantic.stale",
            "Golden Case evidence no longer matches the approved scene binding.",
            (evidence,),
        )
    expected_names = {
        node.node_name
        for component in case.spec.components
        for node in component.nodes
    }
    expected_types = {
        node.path.rsplit("/", 1)[-1]: node.expected_type
        for node in changeset.affected_nodes
    }
    actual = {node.display_name: node for node in query.nodes}
    missing = sorted(name for name in expected_names if name not in actual)
    wrong_type = sorted(
        name
        for name, expected_type in expected_types.items()
        if name in actual and actual[name].node_type != expected_type
    )
    terminal = actual.get(case.expected_terminal_node_name)
    if terminal is None or terminal.node_type != "null":
        wrong_type.append(case.expected_terminal_node_name)
    if missing or wrong_type:
        return ValidatorResult(
            ValidatorKind.SEMANTIC,
            ValidationStatus.FAILED,
            "modeling.semantic.invalid",
            "Golden Case node names or types do not match the trusted specification.",
            (evidence,),
        )
    return ValidatorResult(
        ValidatorKind.SEMANTIC,
        ValidationStatus.PASSED,
        "modeling.semantic.valid",
        "Golden Case node names, types, and terminal output match.",
        (evidence,),
    )


def validate_artifact_capture(
    *,
    changeset: ChangeSet,
    artifact: ArtifactRef,
    framing: CaptureFramingReport,
) -> ValidatorResult:
    """Verify the registered post-Apply capture reference and framing evidence.

    The Runtime already re-hashed the delivered bytes against the bridge
    reference before registration; this validator independently re-checks the
    deterministic framing acceptance band and the capture media type so an
    out-of-band report can never be recorded as a passing Artifact stage.
    """
    if type(changeset) is not ChangeSet:
        raise TypeError("changeset must be an exact ChangeSet")
    if type(artifact) is not ArtifactRef:
        raise TypeError("artifact must be an exact ArtifactRef")
    if type(framing) is not CaptureFramingReport:
        raise TypeError("framing must be an exact CaptureFramingReport")
    evidence = _evidence(
        "artifact.capture",
        {"artifact": artifact.to_dict(), "framing": framing.to_dict()},
        "Content-addressed capture and deterministic framing evidence",
    )
    if artifact.media_type != "image/png":
        return ValidatorResult(
            ValidatorKind.ARTIFACT,
            ValidationStatus.FAILED,
            "modeling.artifact.invalid",
            "The captured artifact is not the deterministic PNG capture.",
            (evidence,),
        )
    tolerance = FramingTolerance()
    framing_ok = (
        framing.margin_left >= tolerance.margin_min
        and framing.margin_right >= tolerance.margin_min
        and framing.margin_bottom >= tolerance.margin_min
        and framing.margin_top >= tolerance.margin_min
        and tolerance.longest_axis_min
        <= framing.longest_axis_ratio
        <= tolerance.longest_axis_max
        and framing.center_offset <= tolerance.center_offset_max
    )
    if not framing_ok:
        return ValidatorResult(
            ValidatorKind.ARTIFACT,
            ValidationStatus.FAILED,
            "modeling.artifact.framing_invalid",
            "The capture framing report is outside the deterministic acceptance band.",
            (evidence,),
        )
    return ValidatorResult(
        ValidatorKind.ARTIFACT,
        ValidationStatus.PASSED,
        "modeling.artifact.valid",
        "A hash-verified capture with accepted deterministic framing is registered.",
        (evidence,),
    )


def validate_scene_query(
    *,
    report: ValidationReport,
    changeset: ChangeSet,
    query: SceneQueryResult,
    sensitivity: SensitivitySampleResult | None = None,
    capture: tuple[ArtifactRef, CaptureFramingReport] | None = None,
) -> ValidationReport:
    """Resolve Cook and Geometry stages from one exact read-only scene query.

    When typed Bridge sample-and-restore evidence is supplied, the
    ParameterSensitivity stage is resolved from it in the same step; when the
    registered content-addressed capture evidence is supplied, the Artifact
    stage is resolved from it in the same step. Without evidence a stage keeps
    its prior (Unavailable) result.
    """
    if type(report) is not ValidationReport:
        raise TypeError("report must be an exact ValidationReport")
    if type(changeset) is not ChangeSet:
        raise TypeError("changeset must be an exact ChangeSet")
    if type(query) is not SceneQueryResult:
        raise TypeError("query must be an exact SceneQueryResult")
    if sensitivity is not None and type(sensitivity) is not SensitivitySampleResult:
        raise TypeError("sensitivity must be an exact SensitivitySampleResult or None")
    if capture is not None:
        if (
            type(capture) is not tuple
            or len(capture) != 2
            or type(capture[0]) is not ArtifactRef
            or type(capture[1]) is not CaptureFramingReport
        ):
            raise TypeError(
                "capture must be an exact (ArtifactRef, CaptureFramingReport) tuple or None"
            )
    if report.changeset_digest != changeset.digest:
        raise ValueError("report does not bind the supplied ChangeSet")
    cook, geometry = validate_applied_scene(changeset=changeset, query=query)

    replacements = {
        ValidatorKind.COOK: cook,
        ValidatorKind.GEOMETRY: geometry,
    }
    if sensitivity is not None:
        replacements[ValidatorKind.PARAMETER_SENSITIVITY] = (
            validate_parameter_sensitivity(
                changeset=changeset,
                baseline=sensitivity.baseline,
                samples=sensitivity.samples,
                restored=sensitivity.restored,
            )
        )
    if capture is not None:
        replacements[ValidatorKind.ARTIFACT] = validate_artifact_capture(
            changeset=changeset,
            artifact=capture[0],
            framing=capture[1],
        )
    results = tuple(
        sorted(
            (replacements.get(item.validator, item) for item in report.results),
            key=lambda item: item.validator.value,
        )
    )
    return ValidationReport(
        spec_digest=report.spec_digest,
        changeset_digest=report.changeset_digest,
        results=results,
        repair_budget=report.repair_budget,
    )


def issue_repair_ticket(
    *,
    budget: RepairBudget,
    ticket_id: str,
    validator: ValidatorKind,
    failure_code: str,
    message: str,
    evidence_digests: tuple[str, ...],
    failed_parameter_samples: tuple[str, ...],
    replay_boundary_digest: str,
) -> tuple[RepairBudget, RepairTicket]:
    """Consume one stage budget and produce an explicit repair request.

    Exhaustion is represented as a ticket rather than an exception so Runtime
    can persist the failure and stop without silently mutating the proposal.
    """
    if type(budget) is not RepairBudget:
        raise TypeError("budget must be an exact RepairBudget")
    if type(validator) is not ValidatorKind:
        raise TypeError("validator must be an exact ValidatorKind")
    remaining = budget.remaining(validator)
    if remaining > 0:
        next_budget = budget.record(validator)
        attempt = next_budget.used(validator)
        status = "Open"
    else:
        next_budget = budget
        attempt = max(1, budget.max_attempts_per_stage)
        status = "Exhausted"
    ticket = RepairTicket(
        ticket_id=ticket_id,
        validator=validator,
        failure_code=failure_code,
        message=message,
        evidence_digests=evidence_digests,
        failed_parameter_samples=failed_parameter_samples,
        replay_boundary_digest=replay_boundary_digest,
        attempt=attempt,
        status=RepairStatus(status),
    )
    return next_budget, ticket


__all__ = [
    "EvidenceRef",
    "ValidationReport",
    "ValidationStatus",
    "ValidatorResult",
    "derive_sensitivity_sample_plan",
    "issue_repair_ticket",
    "validate_applied_scene",
    "validate_artifact_capture",
    "validate_compilation",
    "validate_parameter_sensitivity",
    "validate_golden_case_semantics",
    "validate_scene_query",
]
