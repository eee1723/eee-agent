from __future__ import annotations

from dataclasses import replace

import pytest

from eee_agent.modeling.contracts import RepairBudget, RepairStatus, ValidatorKind
from eee_agent.modeling.validation import (
    ValidationStatus,
    issue_repair_ticket,
    validate_applied_scene,
    validate_compilation,
    validate_scene_query,
)
from eee_agent.houdini_bridge.contracts import (
    SceneBinding,
    SceneQueryResult,
    SelectedNode,
)
from tests.modeling.test_compiler import (
    _brief,
    _catalog,
    _compile,
    _compile_bootstrap,
    _profile,
    _spec,
)


def test_compilation_report_has_deterministic_preapply_results() -> None:
    brief = _brief()
    compilation = _compile(brief=brief)
    report = validate_compilation(
        brief=brief,
        spec=_spec(brief),
        quality_profile=_profile(),
        catalog=_catalog(),
        compilation=compilation,
    )
    assert report.digest == validate_compilation(
        brief=brief,
        spec=_spec(brief),
        quality_profile=_profile(),
        catalog=_catalog(),
        compilation=compilation,
    ).digest
    assert report.complete is False
    assert report.hard_failures == ()
    assert report.results[0].validator is ValidatorKind.ARTIFACT
    assert any(item.status is ValidationStatus.UNAVAILABLE for item in report.results)


def test_compilation_report_fails_when_catalog_digest_is_stale() -> None:
    brief = _brief()
    compilation = _compile(brief=brief)
    stale = replace(compilation, catalog_digest="0" * 64)
    report = validate_compilation(
        brief=brief,
        spec=_spec(brief),
        quality_profile=_profile(),
        catalog=_catalog(),
        compilation=stale,
    )
    spec_result = next(item for item in report.results if item.validator is ValidatorKind.SPEC_CONTRACT)
    assert spec_result.status is ValidationStatus.FAILED
    assert spec_result.code == "modeling.spec.invalid"


def test_repair_ticket_consumes_two_attempts_then_exhausts() -> None:
    budget = RepairBudget(max_attempts_per_stage=2, attempts=())
    digest = "a" * 64
    for attempt in (1, 2):
        budget, ticket = issue_repair_ticket(
            budget=budget,
            ticket_id=f"repair_graph_{attempt}",
            validator=ValidatorKind.GRAPH,
            failure_code="modeling.graph.invalid",
            message="Graph evidence is stale.",
            evidence_digests=(digest,),
            failed_parameter_samples=(),
            replay_boundary_digest=digest,
        )
        assert ticket.attempt == attempt
        assert ticket.status is RepairStatus.OPEN
    unchanged, exhausted = issue_repair_ticket(
        budget=budget,
        ticket_id="repair_graph_3",
        validator=ValidatorKind.GRAPH,
        failure_code="modeling.graph.invalid",
        message="Graph evidence is stale.",
        evidence_digests=(digest,),
        failed_parameter_samples=(),
        replay_boundary_digest=digest,
    )
    assert unchanged == budget
    assert exhausted.attempt == 2
    assert exhausted.status is RepairStatus.EXHAUSTED


def _scene_query(compilation, *, stale: bool = False, empty_last: bool = False):
    binding = compilation.changeset.scene_binding
    if stale:
        binding = SceneBinding(
            instance_id=binding.instance_id,
            scene_epoch=binding.scene_epoch + 1,
            hip_path=binding.hip_path,
            observed_revision="stale-revision",
        )
    nodes = []
    for index, ref in enumerate(compilation.changeset.affected_nodes):
        stats = {
            "points": 0 if empty_last and index == len(compilation.changeset.affected_nodes) - 1 else 8,
            "primitives": 0 if empty_last and index == len(compilation.changeset.affected_nodes) - 1 else 6,
            "bbox": {"min": [0.0, 0.0, 0.0], "max": [1.0, 1.0, 1.0]},
        }
        nodes.append(
            SelectedNode(
                path=ref.path,
                node_type=ref.expected_type,
                parent_path=ref.path.rsplit("/", 1)[0],
                display_name=ref.path.rsplit("/", 1)[1],
                is_locked=False,
                geometry_stats=None if ref.expected_type == "geo" else stats,
            )
        )
    return SceneQueryResult(binding=binding, selected_nodes=(), nodes=tuple(nodes))


def _preapply_report(compilation):
    brief = _brief()
    return validate_compilation(
        brief=brief,
        spec=_spec(brief),
        quality_profile=_profile(),
        catalog=_catalog(),
        compilation=compilation,
    )


def test_scene_query_resolves_cook_and_geometry() -> None:
    compilation = _compile()
    report = validate_scene_query(
        report=_preapply_report(compilation),
        changeset=compilation.changeset,
        query=_scene_query(compilation),
    )
    by_kind = {item.validator: item for item in report.results}
    assert by_kind[ValidatorKind.COOK].status is ValidationStatus.PASSED
    assert by_kind[ValidatorKind.GEOMETRY].status is ValidationStatus.PASSED


def test_scene_query_fails_empty_terminal_geometry() -> None:
    compilation = _compile()
    report = validate_scene_query(
        report=_preapply_report(compilation),
        changeset=compilation.changeset,
        query=_scene_query(compilation, empty_last=True),
    )
    by_kind = {item.validator: item for item in report.results}
    assert by_kind[ValidatorKind.COOK].status is ValidationStatus.PASSED
    assert by_kind[ValidatorKind.GEOMETRY].status is ValidationStatus.FAILED


def test_scene_query_marks_binding_mismatch_stale() -> None:
    compilation = _compile()
    report = validate_scene_query(
        report=_preapply_report(compilation),
        changeset=compilation.changeset,
        query=_scene_query(compilation, stale=True),
    )
    by_kind = {item.validator: item for item in report.results}
    assert by_kind[ValidatorKind.COOK].status is ValidationStatus.STALE
    assert by_kind[ValidatorKind.GEOMETRY].status is ValidationStatus.STALE


def test_bootstrap_validation_ignores_object_container_geometry() -> None:
    compilation = _compile_bootstrap()
    cook, geometry = validate_applied_scene(
        changeset=compilation.changeset,
        query=_scene_query(compilation),
    )
    assert cook.status is ValidationStatus.PASSED
    assert geometry.status is ValidationStatus.PASSED
