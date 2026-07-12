"""Eval orchestrator: run each case (fresh scene -> agent -> evaluate export).

Modes:
  python -m eval.run_eval            run the agent for every case, then assert
  python -m eval.run_eval --dry      skip the agent; just assert already-exported files
  python -m eval.run_eval --case 3-storey house with windows   run one case

The agent must be able to connect to the Houdini RPC bridge and have a working
LLM key in .env. ``--dry`` needs neither — it only parses exported .obj files.
"""
from __future__ import annotations

import argparse
import os
import sys
import traceback
from typing import Any, Dict, List

import yaml

from eee_agent.config import recursion_limit, repo_root
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


def run_case_with_agent(case: Dict[str, Any], agent) -> Dict[str, Any]:
    from eee_agent.tools import scene  # imported lazily (needs bridge)

    export = case.get("export_path", "output/house.obj")
    prompt = case["prompt"]
    # Guarantee the export target matches what we evaluate.
    if "export" not in prompt.lower():
        prompt = f"{prompt.rstrip('.')} and export to {export}."

    scene.scene_reset.invoke({"scope": "/obj"})  # fresh scene per case
    invoked_ok = True
    invoke_err = ""
    try:
        agent.invoke(
            {"messages": [{"role": "user", "content": prompt}]},
            config={"recursion_limit": recursion_limit()},
        )
    except Exception as e:  # noqa: BLE001
        invoked_ok = False
        invoke_err = f"{type(e).__name__}: {e}"

    res = check_file(_abs(export), case.get("expect", {}))
    res["name"] = case["name"]
    res["invoked_ok"] = invoked_ok
    if invoke_err:
        res["issues"].append(f"invoke error: {invoke_err}")
        res["ok"] = False
    return res


def evaluate_dry(case: Dict[str, Any]) -> Dict[str, Any]:
    export = case.get("export_path", "output/house.obj")
    res = check_file(_abs(export), case.get("expect", {}))
    res["name"] = case["name"]
    res["invoked_ok"] = None  # not run
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
        print("no cases selected"); return 1

    agent = None
    if not args.dry:
        from eee_agent.app import build_agent
        agent = build_agent()

    results = []
    for c in cases:
        print(f"\n--- {c['name']} ---", flush=True)
        try:
            r = evaluate_dry(c) if args.dry else run_case_with_agent(c, agent)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            r = {"name": c["name"], "ok": False,
                 "issues": [f"harness error: {type(e).__name__}: {e}"]}
        results.append(r)
        stats = r.get("stats")
        print(f"  ok={r['ok']}  verts={stats.get('verts') if stats else '-'} "
              f"faces={stats.get('faces') if stats else '-'}")
        if r["issues"]:
            for iss in r["issues"]:
                print("    -", iss)

    passed = sum(1 for r in results if r["ok"])
    print(f"\n==== {passed}/{len(results)} passed ====")
    return 0 if passed == len(results) else 2


if __name__ == "__main__":
    sys.exit(main())
