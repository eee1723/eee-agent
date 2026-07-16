from __future__ import annotations

from dataclasses import replace

import pytest

from eee_agent.modeling.contracts import ValidatorKind
from eee_agent.modeling.validation import ValidationStatus, validate_compilation
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
