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
import tempfile
from pathlib import Path
from typing import Any

from eval.geometry_assertions import evaluate, mesh_component_count


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


def make_report(
    fault: str,
    check: str,
    location: str,
    expected: object,
    actual: object,
    hint: str,
) -> dict[str, Any]:
    """Return the bounded B6-compatible failure shape."""
    return {
        "fault": fault[:80],
        "check": check[:80],
        "location": location[:200],
        "expected": expected,
        "actual": actual,
        "hint": hint[:120],
    }


def _assertion_stats(stats: dict[str, Any]) -> dict[str, Any]:
    return {
        "verts": stats["points"],
        "faces": stats["primitives"],
        "bbox": {
            "min": list(stats["bbox_min"]),
            "max": list(stats["bbox_max"]),
            "size": [
                stats["bbox_max"][index] - stats["bbox_min"][index]
                for index in range(3)
            ],
        },
    }


def tier_issues(
    stats: dict[str, Any],
    *,
    envelope: dict[str, Any] | None = None,
    expected_components: int | None = None,
    obj_path: str | None = None,
) -> list[str]:
    """Evaluate one cooked tier using the shared geometry assertion shape."""
    issues: list[str] = []
    if envelope:
        issues.extend(evaluate(_assertion_stats(stats), envelope)["issues"])
        actual_size = [
            stats["bbox_max"][index] - stats["bbox_min"][index]
            for index in range(3)
        ]
        for index, axis in enumerate("xyz"):
            if actual_size[index] < envelope["size_min"][index] - 1e-6:
                issues.append(
                    f"{axis} size {actual_size[index]:.3f} < {envelope['size_min'][index]:.3f}"
                )
            if actual_size[index] > envelope["size_max"][index] + 1e-6:
                issues.append(
                    f"{axis} size {actual_size[index]:.3f} > {envelope['size_max'][index]:.3f}"
                )
    if expected_components is not None and obj_path is not None:
        components = mesh_component_count(obj_path)
        if components != expected_components:
            issues.append(
                f"mesh components {components} != expected {expected_components}"
            )
    return issues


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


def _find_output(container: Any) -> Any:
    """Choose the deterministic top-level OUT sink, never the first flag."""
    children = list(container.children() or ())
    named = next((child for child in children if child.name() == "OUT"), None)
    if named is not None:
        return named
    sinks = [
        child for child in children
        if not list(child.outputs() or ())
        and any(source is not None for source in (child.inputs() or ()))
    ]
    if sinks:
        return max(
            sinks,
            key=lambda child: sum(
                1 for source in (child.inputs() or ()) if source is not None
            ),
        )
    return next(
        (child for child in children if child.isDisplayFlagSet()),
        children[-1] if children else None,
    )


def scan_component(
    container: Any,
    *,
    screenshot_dir: Path | None = None,
    scan_expect: dict[str, Any] | None = None,
    default_envelope: list[float] | None = None,
    screenshot_callback: Any = None,
) -> dict[str, Any]:
    manifest = load_manifest(container.comment())
    output = _find_output(container)
    if output is None:
        raise RuntimeError("committed component has no display output")
    design = [item for item in manifest if item.get("classification") == "design_intent"]
    evidence: list[dict[str, Any]] = []
    cook_errors: list[str] = []
    scan_expect = scan_expect or {}
    expected_components = scan_expect.get("mesh_components")
    scan_tol = scan_expect.get("scan_tol")
    defaults: dict[str, float] = {}
    bindings: dict[str, Any] = {}
    for item in design:
        binding = item.get("binding") or {}
        node = container.node(binding["node"])
        if node is None:
            raise RuntimeError(f"manifest binding node not found: {binding['node']}")
        parm = node.parm(binding["parm"])
        if parm is None:
            raise RuntimeError(f"manifest binding parm not found: {binding['parm']}")
        # Report the geometry owned by the parameter's subnet when one exists.
        # The committed top-level OUT is still cooked for every tier, but a
        # global merge can mask a smaller component's change (for example a
        # wheel changing while the frame remains larger).
        component = (
            node
            if node.type().name() == "subnet"
            else node.parent()
        )
        component_output = (
            _find_output(component) if component is not container else output
        )
        if component_output is None:
            raise RuntimeError(
                f"manifest binding node has no component output: {binding['node']}"
            )
        defaults[item["name"]] = float(item["default"])
        bindings[item["name"]] = parm
        envelope: dict[str, Any] | None = None
        if default_envelope is not None:
            lower, upper = scaled_envelope(
                default_envelope,
                float(item["min"]),
                float(item["default"]),
                float(item["max"]),
                scan_tol=float(scan_tol) if scan_tol is not None else None,
            )
            # ``default_envelope`` is a positive per-axis extent.  Keep the
            # shared assertion's signed bbox contract while checking the
            # scaled extent separately, so centered geometry with negative
            # minima is handled correctly.
            envelope = {
                "bbox_min": [-value for value in upper],
                "bbox_max": upper,
                "size_min": [2.0 * value for value in lower],
                "size_max": [2.0 * value for value in upper],
            }
        tiers: list[dict[str, Any]] = []
        for tier, value in (("min", item["min"]), ("default", item["default"]), ("max", item["max"])):
            issues: list[str] = []
            screenshot: str | None = None
            try:
                for name, default_value in defaults.items():
                    bindings[name].set(default_value)
                parm.set(float(value))
                # Cook the owning subnet explicitly before the top-level sink.
                # This avoids Houdini keeping a subnet's stale output cache
                # when a spare/control parm changes across consecutive tiers.
                node.cook(force=True)
                if component is not container:
                    component.cook(force=True)
                component_output.cook(force=True)
                output.cook(force=True)
                errors = list(output.errors() or [])
                if errors:
                    cook_errors.extend(str(error)[:200] for error in errors)
                    issues.extend(str(error)[:200] for error in errors)
                stats = _node_stats(component_output)
                with tempfile.NamedTemporaryFile(
                    suffix=".obj", delete=False
                ) as handle:
                    obj_path = handle.name
                try:
                    component_output.geometry().saveToFile(obj_path)
                    issues.extend(
                        tier_issues(
                            stats,
                            envelope=envelope,
                            expected_components=expected_components,
                            obj_path=obj_path,
                        )
                    )
                finally:
                    Path(obj_path).unlink(missing_ok=True)
                if screenshot_callback is not None:
                    screenshot = screenshot_callback(
                        output, item["name"], tier, screenshot_dir
                    )
            except Exception as exc:  # noqa: BLE001 - bounded evidence
                issues.append(f"{type(exc).__name__}: {exc}"[:200])
                stats = None
            tiers.append(
                {
                    "tier": tier,
                    "value": value,
                    "ok": not issues and stats is not None,
                    "issues": issues,
                    "stats": stats,
                    "screenshot": screenshot,
                    "reports": [
                        make_report(
                            "parameter_scan",
                            "geometry_assertion",
                            f"{item['name']}:{tier}",
                            envelope,
                            stats,
                            "inspect the failing parameter tier and rebuild the component",
                        )
                    ] if issues else [],
                }
            )
        parm.set(defaults[item["name"]])
        evidence.append({"name": item["name"], "tiers": tiers})
    return {
        "asset": container.path(),
        "parameters": evidence,
        "cook_errors": cook_errors[:32],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("hip", nargs="?", help="optional committed HIP file")
    parser.add_argument("--output", default="output/param_scan_evidence.json")
    parser.add_argument("--scan-expect", default=None, help="JSON object with mesh_components/scan_tol")
    parser.add_argument("--default-envelope", default=None, help="JSON array [x,y,z] for bbox envelope")
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
    try:
        scan_expect = json.loads(args.scan_expect) if args.scan_expect else None
        default_envelope = (
            json.loads(args.default_envelope) if args.default_envelope else None
        )
        report = scan_component(
            containers[0],
            scan_expect=scan_expect,
            default_envelope=default_envelope,
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"PARAM SCAN FAIL: {type(exc).__name__}: {exc}")
        return 1
    if all(
        tier.get("screenshot") is None
        for parameter in report["parameters"]
        for tier in parameter["tiers"]
    ):
        report["notes"] = ["screenshots not_run"]
    payload = json.dumps(report, sort_keys=True)
    if len(payload.encode("utf-8")) > 16 * 1024:
        print("PARAM SCAN FAIL: evidence exceeds 16 KiB")
        return 1
    Path(args.output).write_text(payload, encoding="utf-8")
    if any(
        tier.get("ok") is not True
        for parameter in report["parameters"]
        for tier in parameter["tiers"]
    ):
        print("PARAM SCAN FAIL: one or more parameter tiers failed")
        return 1
    print("PARAM SCAN OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
