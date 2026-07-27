"""Offline geometry evaluation harness.

``--dry`` is the only executable evaluation path: it checks already-exported
geometry files against declarative case expectations. Live agent execution is
deferred: it would drive the Runtime's authenticated sandbox → verify → commit
pipeline, and no bounded harness for that exists yet. A fresh checkout has no
``output/*.obj`` exports, so ``--dry`` reports 0 passed until the case exports
are produced. This module must not load removed legacy tools or reset Houdini
scenes.

Examples:
  python -m eval.run_eval --dry
  python -m eval.run_eval --dry --case "3-storey house with windows"
"""

from __future__ import annotations

import argparse
import os
import sys
import traceback
import json
import subprocess
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List

import yaml

from eee_agent.config import repo_root
from eval.geometry_assertions import check_file


def load_cases(rel_dir: str = "eval/cases") -> List[Dict[str, Any]]:
    cases_dir = os.path.join(repo_root(), rel_dir)
    out: List[Dict[str, Any]] = []
    for fn in sorted(os.listdir(cases_dir)):
        if not fn.endswith((".yaml", ".yml")):
            continue
        with open(os.path.join(cases_dir, fn), "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh)
        if isinstance(data, list):
            out.extend(data)
        elif isinstance(data, dict):
            out.append(data)
    for case in out:
        if not isinstance(case.get("name"), str) or not case["name"].strip():
            raise ValueError("case.name must be a non-empty string")
        for field in ("parts", "color", "parameters", "scan_expect", "tags"):
            if field in case and case[field] is None:
                case[field] = {} if field != "tags" else []
        if "tags" in case and not isinstance(case["tags"], list):
            raise ValueError(f"case {case['name']}: tags must be a list")
        if "parameters" in case and not isinstance(case["parameters"], list):
            raise ValueError(f"case {case['name']}: parameters must be a list")
    return out


def _abs(path: str) -> str:
    return path if os.path.isabs(path) else os.path.join(repo_root(), path)


_LIVE_DEFERRED = (
    "Live agent evaluation is deferred until Runtime MVP S6 provides the "
    "authenticated, bounded provider; use --dry for file assertions."
)


def evaluate_live_deferred(case: Dict[str, Any]) -> Dict[str, Any]:
    """Return a bounded unsupported result without starting an agent or Houdini."""
    return {
        "name": case["name"],
        "ok": False,
        "invoked_ok": None,
        "deferred": True,
        "issues": [_LIVE_DEFERRED],
    }


def evaluate_dry(case: Dict[str, Any]) -> Dict[str, Any]:
    export = case.get("export_path", "output/house.obj")
    res = check_file(_abs(export), case.get("expect", {}))
    res["name"] = case["name"]
    res["invoked_ok"] = None
    return res


def _git_sha() -> str | None:
    try:
        return subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=repo_root(), text=True,
            stderr=subprocess.DEVNULL,
        ).strip() or None
    except Exception:
        return None


def build_report(results: list[dict[str, Any]], cases: list[dict[str, Any]]) -> dict[str, Any]:
    param_cases = [
        case for case in cases
        if "parametric" in set(case.get("tags", []))
    ]
    param_covered = sum(
        1 for case in param_cases
        if case.get("parameters")
        and all(all(key in parm for key in ("min", "default", "max"))
                for parm in case["parameters"])
    )
    assertion_passed = sum(1 for r in results if r.get("ok"))
    notes: list[str] = []
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "git_sha": _git_sha(),
        "evidence_sources": ["eval cases YAML", "dry geometry assertion results"],
        "metrics": {
            "translation_success_rate": (
                assertion_passed / len(results) if results else None
            ),
            "param_coverage": (
                param_covered / len(param_cases) if param_cases else None
            ),
            "geometry_assertion_pass_rate": (
                assertion_passed / len(results) if results else None
            ),
            "avg_repair_rounds": None,
            "e2e_duration_s": None,
            "provider_tokens": None,
            "est_cost": None,
        },
        "cases": results,
    }
    if not results:
        notes.append("not_run")
    if any(report["metrics"][key] is None for key in (
        "avg_repair_rounds", "e2e_duration_s", "provider_tokens", "est_cost"
    )):
        notes.append("runtime/provider evidence not_run")
    report["notes"] = notes
    return report


def _read_runtime_sqlite(path: str) -> dict[str, Any]:
    """Read optional runtime evidence without mutating or requiring its schema."""
    out: dict[str, Any] = {}
    try:
        uri = f"file:{os.path.abspath(path)}?mode=ro"
        with sqlite3.connect(uri, uri=True) as conn:
            try:
                row = conn.execute(
                    "SELECT started_at, finished_at FROM runs "
                    "WHERE started_at IS NOT NULL AND finished_at IS NOT NULL"
                ).fetchall()
                durations = []
                from datetime import datetime
                for started, finished in row:
                    durations.append(
                        (datetime.fromisoformat(finished) - datetime.fromisoformat(started)).total_seconds()
                    )
                out["e2e_duration_s"] = sum(durations) / len(durations) if durations else None
            except sqlite3.Error:
                out["e2e_duration_s"] = None
            try:
                rows = conn.execute(
                    "SELECT run_id, event_type, payload_json FROM events "
                    "WHERE event_type IN ('model.usage_updated', 'tool.completed') "
                    "ORDER BY seq"
                ).fetchall()
                tokens = {"input": 0, "output": 0, "total": 0, "cache_read": 0, "cache_creation": 0}
                repair_counts: dict[str, int] = {}
                repair_seen_ok: set[str] = set()
                for run_id, event_type, payload in rows:
                    try:
                        item = json.loads(payload)
                    except (TypeError, json.JSONDecodeError):
                        continue
                    if event_type == "model.usage_updated":
                        tokens["input"] += int(item.get("input_tokens", 0))
                        tokens["output"] += int(item.get("output_tokens", 0))
                        tokens["total"] += int(item.get("total_tokens", 0))
                        tokens["cache_read"] += int(
                            item.get("cache_read_tokens", item.get("cache_read", 0))
                        )
                        tokens["cache_creation"] += int(
                            item.get("cache_creation_tokens", item.get("cache_creation", 0))
                        )
                        continue
                    if item.get("name") != "verify_geometry":
                        continue
                    content = item.get("content")
                    if isinstance(content, str):
                        try:
                            content = json.loads(content)
                        except json.JSONDecodeError:
                            continue
                    if not isinstance(content, dict) or not run_id:
                        continue
                    ok = content.get("ok") is True
                    if ok:
                        repair_seen_ok.add(str(run_id))
                    elif str(run_id) not in repair_seen_ok:
                        repair_counts[str(run_id)] = repair_counts.get(str(run_id), 0) + 1
                out["provider_tokens"] = tokens["total"] if rows else None
                out["provider_token_breakdown"] = tokens if rows else None
                out["avg_repair_rounds"] = (
                    sum(repair_counts.values()) / len(repair_counts)
                    if repair_counts else (0.0 if rows else None)
                )
            except sqlite3.Error:
                out["provider_tokens"] = None
                out["provider_token_breakdown"] = None
                out["avg_repair_rounds"] = None
    except (OSError, sqlite3.Error, ValueError):
        return {}
    return out


def aggregate_report(report_dir: str, cases: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Aggregate existing evidence JSON files into the stable C7 report shape."""
    selected = cases if cases is not None else load_cases()
    evidence_results: list[dict[str, Any]] = []
    root = Path(report_dir)
    for path in sorted(root.rglob("*.json")) if root.exists() else ():
        if path.name == "eval_report.json":
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        if "name" in payload and ("ok" in payload or "status" in payload):
            evidence_results.append(payload)
        elif "case" in payload:
            evidence_results.append(
                {**payload, "name": payload["case"], "ok": payload.get("status") == "passed"}
            )
        elif "asset" in payload and "parameters" in payload:
            evidence_results.append(
                {
                    **payload,
                    "name": str(payload["asset"]),
                    "ok": all(
                        tier.get("ok") is True
                        for parameter in payload.get("parameters", [])
                        for tier in parameter.get("tiers", [])
                    ),
                }
            )
    report = build_report(evidence_results, selected)
    sqlite_candidates = list(root.rglob("*.sqlite")) + list(root.rglob("*.db"))
    runtime = _read_runtime_sqlite(str(sqlite_candidates[0])) if sqlite_candidates else {}
    report["metrics"].update(runtime)
    if runtime.get("provider_tokens") is None:
        report["notes"].append("provider token evidence not_run")
    return report


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="evaluate existing files only")
    ap.add_argument("--case", default=None, help="substring of case name to run")
    ap.add_argument("--report", default=None, help="write an aggregated JSON report to this directory")
    args = ap.parse_args()

    cases = load_cases()
    if args.case:
        selector = args.case.lower()
        if selector.startswith("tag:"):
            tag = selector[4:]
            cases = [c for c in cases if tag in {str(t).lower() for t in c.get("tags", [])}]
        else:
            cases = [c for c in cases if selector in c["name"].lower()]
    if not cases:
        print("no cases selected")
        return 1

    results = []
    for c in cases:
        print(f"\n--- {c['name']} ---", flush=True)
        try:
            r = evaluate_dry(c) if args.dry else evaluate_live_deferred(c)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            r = {
                "name": c["name"],
                "ok": False,
                "issues": [f"harness error: {type(e).__name__}: {e}"],
            }
        results.append(r)
        stats = r.get("stats")
        print(
            f"  ok={r['ok']}  verts={stats.get('verts') if stats else '-'} "
            f"faces={stats.get('faces') if stats else '-'}"
        )
        for iss in r.get("issues", []):
            print("    -", iss)

    passed = sum(1 for r in results if r["ok"])
    if args.report:
        report = build_report(results, cases)
        os.makedirs(args.report, exist_ok=True)
        report_path = os.path.join(args.report, "eval_report.json")
        with open(report_path, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, sort_keys=True, indent=2)
        print(f"report={report_path}")
        print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    print(f"\n==== {passed}/{len(results)} passed ====")
    return 0 if passed == len(results) else 2


if __name__ == "__main__":
    sys.exit(main())
