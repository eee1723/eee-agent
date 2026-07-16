from __future__ import annotations

from dataclasses import replace

import pytest

from eee_agent.modeling.contracts import RepairBudget, RepairStatus, ValidatorKind
from eee_agent.modeling.validation import (
    ValidationStatus,
    issue_repair_ticket,
    validate_compilation,
)
from tests.modeling.test_compiler import _brief, _catalog, _compile, _profile, _spec


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
