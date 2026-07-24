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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="evaluate existing files only")
    ap.add_argument("--case", default=None, help="substring of case name to run")
    args = ap.parse_args()

    cases = load_cases()
    if args.case:
        cases = [c for c in cases if args.case.lower() in c["name"].lower()]
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
    print(f"\n==== {passed}/{len(results)} passed ====")
    return 0 if passed == len(results) else 2


if __name__ == "__main__":
    sys.exit(main())
