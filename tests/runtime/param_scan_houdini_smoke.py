"""Deterministic manifest-driven parameter scan harness.

This file is intentionally usable from ``hython`` without the Runtime,
Provider, LLM, or Bridge.  The small pure helpers are also imported by offline
tests.  A committed component stores its manifest as JSON in the container
comment; the harness restores that JSON, sweeps design-intent parameters, and
emits bounded evidence.
"""
from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any


def scaled_envelope(default_env: list[float], minimum: float, default: float,
                    maximum: float, *, scan_tol: float | None = None) -> tuple[list[float], list[float]]:
    if default <= 0:
        raise ValueError("default must be positive")
    if minimum > default or default > maximum:
        raise ValueError("expected min <= default <= max")
    tol = min(0.5, (maximum - minimum) / default) if scan_tol is None else min(0.5, max(0.0, scan_tol))
    return (
        [value * (1.0 - tol) for value in default_env],
        [value * (1.0 + tol) for value in default_env],
    )


def load_manifest(comment: str) -> list[dict[str, Any]]:
    value = json.loads(comment)
    if not isinstance(value, list) or len(value) > 64:
        raise ValueError("container comment does not contain a bounded manifest")
    return [item for item in value if isinstance(item, dict)]


def _node_stats(node: Any) -> dict[str, Any]:
    geometry = node.geometry()
    bbox = geometry.boundingBox()
    mn, mx = bbox.minvec(), bbox.maxvec()
    return {
        "points": int(geometry.pointCount()),
        "primitives": int(geometry.primCount()),
        "bbox_min": [float(mn.x()), float(mn.y()), float(mn.z())],
        "bbox_max": [float(mx.x()), float(mx.y()), float(mx.z())],
    }


def scan_component(container: Any, *, screenshot_dir: Path | None = None) -> dict[str, Any]:
    manifest = load_manifest(container.comment())
    output = next((child for child in container.children()
                   if child.isDisplayFlagSet()), None)
    if output is None:
        raise RuntimeError("committed component has no display output")
    design = [item for item in manifest if item.get("classification") == "design_intent"]
    evidence: list[dict[str, Any]] = []
    defaults: dict[str, float] = {}
    for item in design:
        binding = item.get("binding") or {}
        node = container.node(binding["node"])
        parm = node.parm(binding["parm"])
        defaults[item["name"]] = float(item["default"])
        tiers: list[dict[str, Any]] = []
        for tier, value in (("min", item["min"]), ("default", item["default"]), ("max", item["max"])):
            parm.set(float(value))
            output.cook(force=True)
            errors = list(output.errors() or [])
            stats = _node_stats(output)
            tiers.append({"tier": tier, "value": value, "ok": not errors and stats["primitives"] > 0,
                          "issues": [str(error)[:200] for error in errors], "stats": stats, "screenshot": None})
        parm.set(defaults[item["name"]])
        evidence.append({"name": item["name"], "tiers": tiers})
    return {"asset": container.path(), "parameters": evidence, "cook_errors": []}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("hip", nargs="?", help="optional committed HIP file")
    parser.add_argument("--output", default="output/param_scan_evidence.json")
    args = parser.parse_args()
    try:
        import hou  # type: ignore
    except ImportError:
        print("PARAM SCAN NOT_RUN: hython is required")
        return 3
    if args.hip:
        hou.hipFile.load(args.hip)
    containers = [node for node in hou.node("/obj").children()
                  if node.comment().lstrip().startswith("[")]
    if not containers:
        print("PARAM SCAN NOT_RUN: no manifest-bearing container")
        return 3
    report = scan_component(containers[0])
    Path(args.output).write_text(json.dumps(report, sort_keys=True), encoding="utf-8")
    print("PARAM SCAN OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
