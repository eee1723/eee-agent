"""P3 verifier fault-injection acceptance smoke (B5/B6) — run under hython.

Deterministic, no LLM, no bridge: builds a connected reference asset (a gate
frame: two legs + one beam, all interpenetrating, merged into ONE connected
component), verifies the baseline passes every assertion, then injects five
fault classes one at a time (fresh network per fault). Each fault MUST be
caught by its assertion chain, and each produces one bounded structured
location report (JSON line on stdout, JSON array at --evidence).

Exit 0 prints "FAULT INJECTION SMOKE OK"; any uncaught fault exits 1 with
"SMOKE FAIL: <fault>".
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
for candidate in (ROOT, ROOT / ".venv" / "Lib" / "site-packages"):
    if candidate.exists() and str(candidate) not in sys.path:
        sys.path.insert(0, str(candidate))

from eval.geometry_assertions import (  # noqa: E402
    evaluate,
    evaluate_color,
    evaluate_parts,
    mesh_component_count,
)

GEO_NAME = "eee_fault_smoke"
EXPECTED_COLOR = (0.2, 0.6, 0.2)

# part_name -> (center, size); legs and beam interpenetrate so the merged
# mesh is a single connected component resting on y=0.
PART_DEFS = {
    "leg_l": {"center": (-1.0, 1.0, 0.0), "size": (0.2, 2.0, 0.2)},
    "leg_r": {"center": (1.0, 1.0, 0.0), "size": (0.2, 2.0, 0.2)},
    "beam": {"center": (0.0, 1.9, 0.0), "size": (2.4, 0.2, 0.2)},
}

BASELINE_OVERALL = {
    # verts/faces are loose bounds (boolean seam counts are version-dependent);
    # the bbox envelope carries the placement/size precision.
    "min_verts": 24,
    "min_faces": 18,
    "bbox_min": [-1.21, -0.001, -0.11],
    "bbox_max": [1.21, 2.05, 0.11],
}

BASELINE_PARTS = {
    "leg_l": {"min_verts": 8, "min_faces": 6,
              "bbox_min": [-1.15, -0.001, -0.15], "bbox_max": [-0.85, 2.05, 0.15]},
    "leg_r": {"min_verts": 8, "min_faces": 6,
              "bbox_min": [0.85, -0.001, -0.15], "bbox_max": [1.15, 2.05, 0.15]},
    "beam": {"min_verts": 8, "min_faces": 6,
             "bbox_min": [-1.25, 1.75, -0.15], "bbox_max": [1.25, 2.05, 0.15]},
}


def die(message: str) -> None:
    print(f"SMOKE FAIL: {message}", file=sys.stderr)
    raise SystemExit(1)


def build_network(hou):
    """Fresh gate-frame network: box->color per part, merge, boolean union
    (welds interpenetrating parts into one connected mesh), xform, OUT."""
    old = hou.node(f"/obj/{GEO_NAME}")
    if old is not None:
        old.destroy()
    geo = hou.node("/obj").createNode("geo", GEO_NAME)
    for child in geo.children():
        child.destroy()
    merge = geo.createNode("merge", "merge_parts")
    part_paths = {}
    for i, (name, spec) in enumerate(PART_DEFS.items()):
        box = geo.createNode("box", name)
        box.parmTuple("t").set(spec["center"])
        box.parmTuple("size").set(spec["size"])
        color = geo.createNode("color", f"{name}_color")
        color.setInput(0, box)
        color.parm("colorr").set(EXPECTED_COLOR[0])
        color.parm("colorg").set(EXPECTED_COLOR[1])
        color.parm("colorb").set(EXPECTED_COLOR[2])
        merge.setInput(i, color)
        part_paths[name] = {"box": box.path(), "color": color.path()}
    union = geo.createNode("boolean", "weld_union")
    union.setInput(0, merge)
    xform = geo.createNode("xform", "place")
    xform.setInput(0, union)
    out = geo.createNode("null", "OUT")
    out.setInput(0, xform)
    out.setDisplayFlag(True)
    out.setRenderFlag(True)
    geo.layoutChildren()
    return geo, part_paths, merge, xform, out


def stats_of(node) -> dict:
    g = node.geometry()
    bb = g.boundingBox()
    mn, mx = bb.minvec(), bb.maxvec()
    mins = [mn.x(), mn.y(), mn.z()]
    maxs = [mx.x(), mx.y(), mx.z()]
    return {
        "verts": len(g.points()),
        "faces": len(g.prims()),
        "bbox": {"min": mins, "max": maxs,
                 "size": [maxs[i] - mins[i] for i in range(3)]},
    }


def color_attr_of(node) -> dict:
    g = node.geometry()
    cd = g.findPointAttrib("Cd")
    if cd is None:
        return {"present": False, "mean": None}
    vals = [p.attribValue(cd) for p in g.points()]
    n = len(vals)
    mean = [sum(v[i] for v in vals) / n for i in range(3)]
    return {"present": True, "mean": mean}


def collect(hou, part_paths, out, obj_path):
    """Cook the network and gather overall stats, per-part stats, Cd attr."""
    out.geometry().saveToFile(str(obj_path))
    overall = stats_of(out)
    parts = {}
    for name, paths in part_paths.items():
        node = hou.node(paths["color"])
        if node is not None:
            parts[name] = stats_of(node)
    beam = hou.node(part_paths["beam"]["color"])
    beam_attr = color_attr_of(beam) if beam is not None else None
    return overall, parts, beam_attr


def bounded(value):
    if isinstance(value, float):
        return round(value, 3)
    if isinstance(value, (list, tuple)):
        return [bounded(v) for v in value]
    return value


def make_report(fault, check, location, expected, actual, hint):
    return {
        "fault": fault,
        "check": check,
        "location": location,
        "expected": bounded(expected),
        "actual": bounded(actual),
        "hint": hint[:120],
    }


def check_baseline(obj_path, overall, parts, beam_attr):
    res = evaluate(overall, BASELINE_OVERALL)
    if not res["ok"]:
        die(f"baseline overall: {res['issues']}")
    res = evaluate_parts(parts, BASELINE_PARTS)
    if not res["ok"]:
        die(f"baseline parts: {res['issues']}")
    n = mesh_component_count(str(obj_path))
    if n != 1:
        die(f"baseline components: expected 1, got {n}")
    res = evaluate_color(beam_attr, EXPECTED_COLOR)
    if not res["ok"]:
        die(f"baseline color: {res['issues']}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--evidence", required=True,
                        help="path to write the JSON array of fault reports")
    args = parser.parse_args()

    try:
        import hou
    except ImportError:
        die("hou is not importable; run with hython")

    workdir = Path(tempfile.mkdtemp(prefix="eee_fault_"))
    obj_path = workdir / "merged.obj"
    reports = []
    geo = None
    try:
        # --- baseline: everything must pass -------------------------------
        geo, part_paths, merge, xform, out = build_network(hou)
        overall, parts, beam_attr = collect(hou, part_paths, out, obj_path)
        check_baseline(obj_path, overall, parts, beam_attr)
        geo.destroy()

        def rerun(fault, mutate, check_fn):
            """Rebuild, mutate, cook, run check. check_fn returns
            (captured, report) — captured must be True."""
            nonlocal geo
            geo, paths, mg, xf, o = build_network(hou)
            mutate(hou, paths, mg, xf)
            ov, pts, attr = collect(hou, paths, o, obj_path)
            captured, report = check_fn(hou, paths, mg, xf, ov, pts, attr)
            if not captured:
                die(fault)
            reports.append(report)
            print(json.dumps(report, ensure_ascii=False))
            geo.destroy()
            geo = None

        # --- fault 1: disconnected part ------------------------------------
        def mutate_disconnect(hou, paths, mg, xf):
            hou.node(paths["beam"]["box"]).parm("ty").set(2.9)

        def check_disconnect(hou, paths, mg, xf, ov, pts, attr):
            n = mesh_component_count(str(obj_path))
            captured = n is not None and n != 1
            return captured, make_report(
                "disconnected_part", "mesh_component_count",
                paths["beam"]["box"], 1, n,
                "beam 平移后与立柱不再相交导致网格断连：回退 beam 的 ty 使杆件重叠后再 merge",
            )

        rerun("disconnected_part", mutate_disconnect, check_disconnect)

        # --- fault 2: wrong size -------------------------------------------
        def mutate_size(hou, paths, mg, xf):
            hou.node(paths["leg_l"]["box"]).parmTuple("size").set((0.4, 4.0, 0.4))

        def check_size(hou, paths, mg, xf, ov, pts, attr):
            res = evaluate_parts(pts, BASELINE_PARTS)
            captured = (not res["ok"]) and any(
                i.startswith("leg_l: ") for i in res["issues"])
            return captured, make_report(
                "wrong_size", "evaluate_parts",
                paths["leg_l"]["box"], BASELINE_PARTS["leg_l"]["bbox_max"],
                pts["leg_l"]["bbox"]["max"],
                "leg_l 尺寸约放大 2 倍超出期望包络：核对 box size 参数与参考比例",
            )

        rerun("wrong_size", mutate_size, check_size)

        # --- fault 3: pivot / placement error ------------------------------
        def mutate_pivot(hou, paths, mg, xf):
            xf.parm("ty").set(0.5)

        def check_pivot(hou, paths, mg, xf, ov, pts, attr):
            res = evaluate(ov, BASELINE_OVERALL)
            captured = (not res["ok"]) and any(
                "y max" in i for i in res["issues"])
            return captured, make_report(
                "pivot_error", "evaluate",
                xf.path(), BASELINE_OVERALL["bbox_max"], ov["bbox"]["max"],
                "整体离地 0.5（bbox 底不再落在 y=0）：检查 xform 平移，把资产放回地面",
            )

        rerun("pivot_error", mutate_pivot, check_pivot)

        # --- fault 4: missing part -----------------------------------------
        def mutate_missing(hou, paths, mg, xf):
            hou.node(paths["leg_r"]["color"]).destroy()
            hou.node(paths["leg_r"]["box"]).destroy()

        def check_missing(hou, paths, mg, xf, ov, pts, attr):
            res = evaluate_parts(pts, BASELINE_PARTS)
            captured = "leg_r: missing" in res["issues"]
            return captured, make_report(
                "missing_part", "evaluate_parts",
                "leg_r", "present", "missing",
                "必需部件 leg_r 缺失：检查部件节点是否被删除或未接入 merge",
            )

        rerun("missing_part", mutate_missing, check_missing)

        # --- fault 5: wrong material / color -------------------------------
        def mutate_color(hou, paths, mg, xf):
            node = hou.node(paths["beam"]["color"])
            node.parm("colorr").set(1.0)
            node.parm("colorg").set(0.0)
            node.parm("colorb").set(0.0)

        def check_color(hou, paths, mg, xf, ov, pts, attr):
            res = evaluate_color(attr, EXPECTED_COLOR)
            captured = not res["ok"]
            return captured, make_report(
                "wrong_color", "evaluate_color",
                paths["beam"]["color"], list(EXPECTED_COLOR), attr["mean"],
                "beam 的 Cd 均值偏离期望颜色：检查 color 节点的颜色参数是否被改",
            )

        rerun("wrong_color", mutate_color, check_color)
    finally:
        node = hou.node(f"/obj/{GEO_NAME}")
        if node is not None:
            node.destroy()

    evidence = Path(args.evidence)
    evidence.parent.mkdir(parents=True, exist_ok=True)
    evidence.write_text(json.dumps(reports, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    print("FAULT INJECTION SMOKE OK")


if __name__ == "__main__":
    if "pytest" in sys.argv[0].lower():
        die("this smoke must run with hython")
    main()
