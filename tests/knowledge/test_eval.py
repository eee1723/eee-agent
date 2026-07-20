"""Stage 7 Task 15 — golden retrieval evaluator unit tests.

Exercises the metric computation (top-1, recall@5, MRR, ambiguity correctness,
hard caps, p95 latency) with a fake service so no HFS/RPC/model is needed, and
validates the golden YAML structure and category coverage. ``run_eval`` lives
under ``eval/`` (not a package), so it is loaded by path.
"""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]
_RUN_EVAL_PATH = _REPO / "eval" / "knowledge" / "run_eval.py"
_GOLDEN_PATH = _REPO / "eval" / "knowledge" / "golden_queries.yaml"

_spec = importlib.util.spec_from_file_location("eee_kb_run_eval", _RUN_EVAL_PATH)
run_eval = importlib.util.module_from_spec(_spec)
# Register before exec so the @dataclass machinery can resolve the module's
# (stringized, PEP 563) field annotations via sys.modules.
sys.modules["eee_kb_run_eval"] = run_eval
_spec.loader.exec_module(run_eval)

from eee_agent.knowledge.api import (  # noqa: E402
    CandidateSummary,
    KnowledgeErrorCode,
    SearchResult,
    SearchResponse,
)


def _result(
    entity_id: str = "node_document:sop/x.txt@current",
    *,
    summary: str = "s",
    neighbors=(),
    tags=(),
    signatures=(),
) -> SearchResult:
    return SearchResult(
        entity_id=entity_id, kind="node_document", subtype="",
        canonical_name=entity_id, title=entity_id, summary=summary,
        authority="official_houdini_docs", is_current=True,
        operator_type=None, operator_type_status=None, tags=tuple(tags),
        signatures=tuple(signatures), match_reasons=("symbol",),
        neighbors=tuple(neighbors), source_path="sop/x.txt", source_anchor=None,
    )


def _cand(entity_id: str) -> CandidateSummary:
    return CandidateSummary(
        entity_id=entity_id, kind="node_document",
        canonical_name=entity_id, title=entity_id,
    )


class _FakeService:
    """Returns canned responses in call order; records requests."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def search(self, request):
        self.calls.append(request)
        if self._responses:
            return self._responses.pop(0)
        return SearchResponse(ok=False, code=KnowledgeErrorCode.NO_MATCH)


# --- load_cases / build_request -------------------------------------------


def test_load_cases_reads_yaml(tmp_path: Path) -> None:
    yaml_path = tmp_path / "cases.yaml"
    yaml_path.write_text(
        "- id: a\n  category: sop_exact\n  request: {symbol: boolean}\n"
        "  expect: match\n  expected_any: ['node_document:sop/boolean.txt@current']\n",
        encoding="utf-8",
    )
    cases = run_eval.load_cases(yaml_path)
    assert len(cases) == 1
    assert cases[0]["id"] == "a"
    assert cases[0]["request"]["symbol"] == "boolean"


def test_load_cases_rejects_case_without_request(tmp_path: Path) -> None:
    yaml_path = tmp_path / "bad.yaml"
    yaml_path.write_text("- id: a\n  expect: match\n", encoding="utf-8")
    with pytest.raises(ValueError):
        run_eval.load_cases(yaml_path)


def test_build_request_maps_fields_and_defaults() -> None:
    req = run_eval.build_request({
        "symbol": "hou.Node", "kinds": ["hom_class", "hom_method"],
        "predicate": "inherits_from", "direction": "incoming",
        "include_historical": True, "limit": 9,
    })
    assert req.symbol == "hou.Node"
    assert req.kinds == ("hom_class", "hom_method")
    assert req.predicate == "inherits_from"
    assert req.direction == "incoming"
    assert req.include_historical is True
    assert req.limit == 9


def test_build_request_defaults_for_empty_request() -> None:
    req = run_eval.build_request({})
    assert req.symbol is None
    assert req.kinds == ()
    assert req.direction == "outgoing"
    assert req.include_historical is False
    assert req.limit == 5


# --- rank / size / caps ----------------------------------------------------


def test_first_rank_returns_one_based_position() -> None:
    response = SearchResponse(
        ok=True, code=None,
        results=(_result("a"), _result("b"), _result("c")),
    )
    assert run_eval.first_rank(response, {"b"}) == 2
    assert run_eval.first_rank(response, {"z"}) is None


def test_response_chars_is_positive_int() -> None:
    response = SearchResponse(ok=True, code=None, results=(_result("a"),))
    assert isinstance(run_eval.response_chars(response), int)
    assert run_eval.response_chars(response) > 0


def test_caps_ok_passes_within_limits() -> None:
    response = SearchResponse(ok=True, code=None, results=(_result("a"),))
    assert run_eval.caps_ok(response) is True


def test_caps_ok_fails_when_summary_over_cap() -> None:
    response = SearchResponse(
        ok=True, code=None, results=(_result("a", summary="x" * (run_eval.CAP_SUMMARY + 1)),),
    )
    assert run_eval.caps_ok(response) is False


def test_caps_ok_fails_when_too_many_results() -> None:
    response = SearchResponse(
        ok=True, code=None,
        results=tuple(_result(f"e{i}") for i in range(run_eval.CAP_RESULTS + 1)),
    )
    assert run_eval.caps_ok(response) is False


# --- evaluate / summarize --------------------------------------------------


def _case(cid, symbol=None, query=None, expect="match", expected=("e1",)):
    return {
        "id": cid, "category": "x", "expect": expect,
        "request": {"symbol": symbol, "query": query},
        "expected_any": list(expected),
    }


def test_evaluate_records_top1_recall_and_latency() -> None:
    cases = [_case("c1", symbol="e1", expected=("e1",))]
    service = _FakeService([SearchResponse(ok=True, code=None, results=(_result("e1"),))])
    results, metrics = run_eval.evaluate(cases, service, timer=_stepping_timer())
    assert len(results) == 1
    r = results[0]
    assert r.top1_hit is True
    assert r.recall5_hit is True
    assert r.rank == 1
    assert r.latency_ms > 0
    assert metrics["exact_top1"] == 1.0
    assert metrics["recall_at_5"] == 1.0
    assert metrics["passes"] is True


def _stepping_timer():
    ticks = iter([0.0, 0.002, 10.0, 10.005])

    def _timer():
        return next(ticks)

    return _timer


def test_exact_top1_drops_below_threshold_on_miss() -> None:
    cases = [
        _case("hit", symbol="e1", expected=("e1",)),
        _case("miss", symbol="e1", expected=("e9",)),
    ]
    service = _FakeService([
        SearchResponse(ok=True, code=None, results=(_result("e1"),)),
        SearchResponse(ok=True, code=None, results=(_result("e1"),)),  # wrong top-1
    ])
    _, metrics = run_eval.evaluate(cases, service)
    assert metrics["exact_top1"] == 0.5
    assert metrics["passes"] is False  # exact_top1 < 100%


def test_recall_at_5_uses_top_five_window() -> None:
    results = tuple(_result(f"e{i}") for i in range(6))  # target at rank 6
    cases = [_case("c", query="q", expected=("e5",))]
    service = _FakeService([SearchResponse(ok=True, code=None, results=results)])
    _, metrics = run_eval.evaluate(cases, service)
    assert metrics["recall_at_5"] == 0.0


def test_mrr_reciprocal_rank() -> None:
    cases = [_case("c", symbol="e2", expected=("e2",))]
    service = _FakeService([
        SearchResponse(ok=True, code=None, results=(_result("e1"), _result("e2"))),
    ])
    _, metrics = run_eval.evaluate(cases, service)
    assert metrics["mrr"] == 0.5


def test_ambiguity_correctness_for_match_and_ambiguous() -> None:
    cases = [
        _case("m", symbol="e1", expected=("e1",), expect="match"),
        {"id": "a", "category": "ambiguity", "expect": "ambiguous",
         "request": {"symbol": "scatter"}, "expected_any": []},
    ]
    service = _FakeService([
        SearchResponse(ok=True, code=None, results=(_result("e1"),)),
        SearchResponse(ok=False, code=KnowledgeErrorCode.AMBIGUOUS_SYMBOL,
                       candidates=(_cand("c1"), _cand("c2"))),
    ])
    _, metrics = run_eval.evaluate(cases, service)
    assert metrics["ambiguity_correctness"] == 1.0


def test_ambiguity_failure_when_match_case_returns_ambiguous() -> None:
    cases = [_case("m", symbol="e1", expected=("e1",), expect="match")]
    service = _FakeService([
        SearchResponse(ok=False, code=KnowledgeErrorCode.AMBIGUOUS_SYMBOL,
                       candidates=(_cand("c1"),)),
    ])
    _, metrics = run_eval.evaluate(cases, service)
    assert metrics["ambiguity_correctness"] == 0.0
    assert metrics["passes"] is False


def test_caps_violation_fails_pass_flag() -> None:
    cases = [_case("c", symbol="e1", expected=("e1",))]
    service = _FakeService([
        SearchResponse(ok=True, code=None,
                       results=(_result("e1", summary="x" * (run_eval.CAP_SUMMARY + 1)),)),
    ])
    _, metrics = run_eval.evaluate(cases, service)
    assert metrics["caps_ok"] is False
    assert metrics["passes"] is False


# --- percentile ------------------------------------------------------------


def test_percentile_nearest_rank() -> None:
    values = list(range(1, 21))  # 1..20
    assert run_eval.percentile(values, 95) == 19  # ceil(0.95*20)=19 -> values[18]
    assert run_eval.percentile(values, 100) == 20
    assert run_eval.percentile([42], 95) == 42
    assert run_eval.percentile([], 95) == 0.0


# --- main exit code --------------------------------------------------------


def _write_cases(tmp_path: Path) -> Path:
    p = tmp_path / "golden.yaml"
    p.write_text(
        "- id: ok\n  category: sop_exact\n  request: {symbol: e1}\n"
        "  expect: match\n  expected_any: ['e1']\n",
        encoding="utf-8",
    )
    return p


def test_main_returns_zero_when_passing(tmp_path: Path, monkeypatch) -> None:
    cases_path = _write_cases(tmp_path)
    monkeypatch.setattr(
        run_eval, "_build_service",
        lambda kb: _FakeService([SearchResponse(ok=True, code=None, results=(_result("e1"),))]),
    )
    code = run_eval.main(["--kb", str(tmp_path / "k.sqlite"), "--cases", str(cases_path)])
    assert code == 0


def test_main_returns_nonzero_on_recall_failure(tmp_path: Path, monkeypatch, capsys) -> None:
    cases_path = _write_cases(tmp_path)
    monkeypatch.setattr(
        run_eval, "_build_service",
        lambda kb: _FakeService([SearchResponse(ok=True, code=None, results=(_result("WRONG"),))]),
    )
    code = run_eval.main(["--kb", str(tmp_path / "k.sqlite"), "--cases", str(cases_path)])
    assert code == 1
    out = json.loads(capsys.readouterr().out)
    assert out["metrics"]["passes"] is False


# --- golden YAML structure -------------------------------------------------

_REQUIRED_CATEGORIES = {
    "sop_exact", "sop_namespace", "sop_historical", "node_purpose",
    "vex_name", "vex_purpose", "hom_class", "hom_function", "hom_method",
    "hom_inheritance", "ambiguity", "no_match", "invalid", "skill",
    "filter", "relation",
}
_FORBIDDEN_BODY_KEYS = {"body", "text", "content", "document", "raw"}


def test_golden_yaml_exists_and_has_30_to_50_cases() -> None:
    cases = run_eval.load_cases(_GOLDEN_PATH)
    assert 30 <= len(cases) <= 50, f"expected 30-50 cases, got {len(cases)}"


def test_golden_yaml_covers_all_categories() -> None:
    cases = run_eval.load_cases(_GOLDEN_PATH)
    categories = {c.get("category") for c in cases}
    missing = _REQUIRED_CATEGORIES - categories
    assert not missing, f"missing categories: {sorted(missing)}"


def test_golden_yaml_cases_have_required_shape() -> None:
    cases = run_eval.load_cases(_GOLDEN_PATH)
    valid_expect = {"match", "ambiguous", "no_match", "invalid"}
    for case in cases:
        assert case.get("id"), f"case missing id: {case!r}"
        assert case.get("category"), f"case missing category: {case!r}"
        assert case.get("expect") in valid_expect, f"bad expect: {case!r}"
        assert isinstance(case.get("request"), dict)
        if case["expect"] == "match":
            assert case.get("expected_any"), f"match case needs expected_any: {case['id']}"


def test_golden_yaml_has_no_official_document_bodies() -> None:
    cases = run_eval.load_cases(_GOLDEN_PATH)
    for case in cases:
        assert not (_FORBIDDEN_BODY_KEYS & set(case)), (
            f"case {case.get('id')} carries a body key: {case!r}"
        )
        # Requests/expected carry IDs and short query terms, not doc prose.
        for value in case.get("expected_any", []):
            assert isinstance(value, str) and len(value) < 200
        query = case["request"].get("query")
        if query:
            assert isinstance(query, str) and len(query) < 200
