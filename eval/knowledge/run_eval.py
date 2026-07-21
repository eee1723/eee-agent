"""Golden retrieval evaluator for the Houdini knowledge service.

Loads golden query cases from YAML, runs each as a read-only ``service.search``
request against a freshly built cache, and reports deterministic retrieval
metrics: exact-symbol top-1, recall@5, MRR, ambiguity correctness, whether every
response respects the service hard caps, the maximum response size and the p95
local query latency. Exits non-zero unless: unambiguous exact-symbol top-1 is
100%, recall@5 is at least 95%, ambiguity correctness is 100% and every response
respects the hard caps.

Golden cases carry only request metadata and expected entity IDs/symbols -- never
official document bodies -- and the emitted JSON carries only IDs, codes and
metrics. The service is constructed with a no-op stale checker because the cache
is freshly built in the same command, so it cannot be stale by construction.
"""
from __future__ import annotations

import argparse
import json
import math
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterator

import yaml

from eee_agent.knowledge.api import (
    KnowledgeErrorCode,
    SearchRequest,
    SearchResponse,
)
from eee_agent.knowledge.service import KnowledgeService

__all__ = [
    "CAP_CANDIDATES",
    "CAP_NEIGHBORS",
    "CAP_RESULTS",
    "CAP_SIGNATURES",
    "CAP_SUMMARY",
    "CAP_TAGS",
    "CaseResult",
    "build_request",
    "caps_ok",
    "evaluate",
    "first_rank",
    "load_cases",
    "main",
    "percentile",
    "response_chars",
    "summarize",
]

# Hard response caps mirrored from the service (design §12.1). The evaluator
# asserts every response conforms as defense in depth.
CAP_RESULTS = 25
CAP_SUMMARY = 240
CAP_CANDIDATES = 10
CAP_NEIGHBORS = 8
CAP_TAGS = 12
CAP_SIGNATURES = 5

_DEFAULT_CASES = Path(__file__).with_name("golden_queries.yaml")


def load_cases(path: Path | str) -> list[dict]:
    """Load and minimally validate golden cases from a YAML file."""
    with open(path, "r", encoding="utf-8") as handle:
        data = yaml.safe_load(handle)
    cases = data or []
    if not isinstance(cases, list):
        raise ValueError("golden queries file must contain a list of cases")
    validated: list[dict] = []
    for case in cases:
        if not isinstance(case, dict) or "request" not in case:
            raise ValueError(f"invalid golden case (needs 'request'): {case!r}")
        validated.append(case)
    return validated


def build_request(req: dict) -> SearchRequest:
    """Map a YAML request dict to a Stage 5 SearchRequest DTO."""
    kinds = req.get("kinds")
    return SearchRequest(
        symbol=req.get("symbol"),
        query=req.get("query"),
        kinds=tuple(kinds) if kinds else (),
        context=req.get("context"),
        tag=req.get("tag"),
        superclass=req.get("superclass"),
        predicate=req.get("predicate"),
        direction=req.get("direction", "outgoing"),
        include_historical=bool(req.get("include_historical", False)),
        limit=int(req.get("limit", 5)),
    )


def first_rank(response: SearchResponse, expected: set[str]) -> int | None:
    """1-based rank of the first expected entity in results, or None."""
    if not expected:
        return None
    for index, result in enumerate(response.results):
        if result.entity_id in expected:
            return index + 1
    return None


def response_chars(response: SearchResponse) -> int:
    """Serialized size of the response dict in characters."""
    return len(json.dumps(response.to_dict(), sort_keys=True, ensure_ascii=False))


def caps_ok(response: SearchResponse) -> bool:
    """True iff the response respects every service hard cap."""
    if len(response.results) > CAP_RESULTS:
        return False
    if len(response.candidates) > CAP_CANDIDATES:
        return False
    for result in response.results:
        if len(result.summary) > CAP_SUMMARY:
            return False
        if len(result.neighbors) > CAP_NEIGHBORS:
            return False
        if len(result.tags) > CAP_TAGS:
            return False
        if len(result.signatures) > CAP_SIGNATURES:
            return False
    return True


def _outcome_correct(expect: str, response: SearchResponse) -> bool:
    code = response.code
    if expect == "no_match":
        return code == KnowledgeErrorCode.NO_MATCH
    if expect == "invalid":
        return code == KnowledgeErrorCode.INVALID_ARGUMENT
    if expect == "ambiguous":
        return code == KnowledgeErrorCode.AMBIGUOUS_SYMBOL
    if expect == "match":
        return response.ok
    return False


@dataclass(frozen=True)
class CaseResult:
    id: str
    category: str
    expect: str
    has_symbol: bool
    response_ok: bool
    response_code: str | None
    top1_hit: bool
    recall5_hit: bool
    rank: int | None
    ambiguity_correct: bool
    outcome_correct: bool
    caps_ok: bool
    response_chars: int
    latency_ms: float

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "category": self.category,
            "expect": self.expect,
            "has_symbol": self.has_symbol,
            "ok": self.response_ok,
            "code": self.response_code,
            "top1_hit": self.top1_hit,
            "recall5_hit": self.recall5_hit,
            "rank": self.rank,
            "ambiguity_correct": self.ambiguity_correct,
            "outcome_correct": self.outcome_correct,
            "caps_ok": self.caps_ok,
            "response_chars": self.response_chars,
            "latency_ms": self.latency_ms,
        }


def evaluate(
    cases: list[dict],
    service: KnowledgeService,
    *,
    timer: Callable[[], float] = time.perf_counter,
) -> tuple[list[CaseResult], dict]:
    """Run every case against ``service`` and return per-case results + metrics."""
    results: list[CaseResult] = []
    for case in cases:
        request = build_request(case.get("request") or {})
        expected = set(case.get("expected_any") or ())
        expect = case.get("expect", "match")
        start = timer()
        response = service.search(request)
        latency_ms = (timer() - start) * 1000.0

        rank = first_rank(response, expected)
        is_ambiguous = response.code == KnowledgeErrorCode.AMBIGUOUS_SYMBOL
        top1_hit = bool(response.results) and response.results[0].entity_id in expected
        recall5_hit = rank is not None and rank <= 5
        if expect == "ambiguous":
            ambiguity_correct = is_ambiguous and len(response.candidates) >= 2
        elif expect == "match":
            ambiguity_correct = not is_ambiguous
        else:
            ambiguity_correct = True

        results.append(CaseResult(
            id=str(case.get("id", "")),
            category=str(case.get("category", "")),
            expect=expect,
            has_symbol=bool(request.symbol and request.symbol.strip()),
            response_ok=response.ok,
            response_code=response.code.value if response.code else None,
            top1_hit=top1_hit,
            recall5_hit=recall5_hit,
            rank=rank,
            ambiguity_correct=ambiguity_correct,
            outcome_correct=_outcome_correct(expect, response),
            caps_ok=caps_ok(response),
            response_chars=response_chars(response),
            latency_ms=latency_ms,
        ))
    return results, summarize(results)


def _rate(items: list, predicate: Callable[[CaseResult], bool]) -> float:
    if not items:
        return 1.0
    return sum(1.0 for item in items if predicate(item)) / len(items)


def summarize(results: list[CaseResult]) -> dict:
    """Compute the deterministic metric summary over per-case results."""
    match = [r for r in results if r.expect == "match"]
    exact = [r for r in match if r.has_symbol]
    ambiguity = [r for r in results if r.expect in ("match", "ambiguous")]
    outcomes = [r for r in results if r.expect in ("no_match", "invalid", "ambiguous")]

    mrr = (
        sum((1.0 / r.rank) if r.rank else 0.0 for r in match) / len(match)
        if match else 0.0
    )
    metrics = {
        "cases": len(results),
        "match_cases": len(match),
        "exact_symbol_cases": len(exact),
        "exact_top1": _rate(exact, lambda r: r.top1_hit),
        "recall_at_5": _rate(match, lambda r: r.recall5_hit),
        "mrr": mrr,
        "ambiguity_correctness": _rate(ambiguity, lambda r: r.ambiguity_correct),
        "outcome_correctness": _rate(outcomes, lambda r: r.outcome_correct),
        "caps_ok": all(r.caps_ok for r in results),
        "max_response_chars": max((r.response_chars for r in results), default=0),
        "p95_latency_ms": percentile([r.latency_ms for r in results], 95),
    }
    metrics["passes"] = (
        metrics["exact_top1"] >= 1.0
        and metrics["recall_at_5"] >= 0.95
        and metrics["ambiguity_correctness"] >= 1.0
        and metrics["caps_ok"]
    )
    return metrics


def percentile(values: list[float], percent: float) -> float:
    """Nearest-rank percentile. Returns 0.0 for an empty input."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, math.ceil(percent / 100.0 * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def _build_service(kb_path: str) -> KnowledgeService:
    """Construct the read-only service over a freshly built cache."""
    return KnowledgeService(Path(kb_path), stale_checker=lambda metadata: False)


def main(argv: list[str] | None = None) -> int:
    """Run the evaluator. Returns 0 iff all thresholds pass, else 1."""
    parser = argparse.ArgumentParser(
        prog="run_eval",
        description="Evaluate Houdini knowledge retrieval quality.",
    )
    parser.add_argument("--kb", required=True, help="path to a built knowledge cache")
    parser.add_argument(
        "--cases", default=None,
        help="path to golden queries YAML (defaults to the packaged file)",
    )
    parser.add_argument("--pretty", action="store_true", help="pretty-print JSON")
    namespace = parser.parse_args(argv)

    cases_path = Path(namespace.cases) if namespace.cases else _DEFAULT_CASES
    cases = load_cases(cases_path)
    service = _build_service(namespace.kb)
    results, metrics = evaluate(cases, service)
    payload = {"metrics": metrics, "cases": [r.to_dict() for r in results]}
    print(json.dumps(payload, indent=2 if namespace.pretty else None, sort_keys=True))
    return 0 if metrics["passes"] else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
