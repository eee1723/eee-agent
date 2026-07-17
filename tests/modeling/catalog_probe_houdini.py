"""Read-only-ish disposable catalog probe; creates and removes temp nodes."""

from __future__ import annotations

import json

import hou

_CANDIDATES = {
    "polyextrude": ("dist", "inset", "divs"),
    "fuse": ("tol3d", "consolidatesnappedpoints", "deldegen"),
    "normal": ("type", "cuspangle", "method", "normalize", "reverse"),
    "subdivide": ("algorithm", "iterations", "creaseweight", "bias"),
    "line": ("originx", "originy", "originz", "dirx", "diry", "dirz", "dist", "points"),
    "resample": ("length", "segs", "dosegs", "treatpolysas", "evenlastseg"),
    "sweep": ("surfacetype", "skinend", "scale", "roll", "twist"),
    "copytopoints": ("transform", "pack", "pivot", "targetgroup"),
    "boolean": ("booleanop", "treatas", "subtractchoices", "seamoperation"),
}


def main() -> None:
    root = hou.node("/obj").createNode("geo", "eee_catalog_probe")
    try:
        result = []
        for requested, names in _CANDIDATES.items():
            node = root.createNode(requested)
            node_type = node.type()
            parms = []
            for parm in node.parms():
                if parm.name() not in names:
                    continue
                template = parm.parmTemplate()
                if not hasattr(template, "defaultValue"):
                    continue
                default = template.defaultValue()
                if isinstance(default, tuple):
                    if len(default) == 1:
                        default = default[0]
                    else:
                        default = list(default)
                if type(default) in (bool, int, float, str, list):
                    parms.append(
                        {
                            "name": parm.name(),
                            "label": template.label(),
                            "default": default,
                            "evaluated": parm.eval(),
                            "template": template.type().name(),
                        }
                    )
            result.append(
                {
                    "requested": requested,
                    "internal": node_type.name(),
                    "min_inputs": node_type.minNumInputs(),
                    "max_inputs": node_type.maxNumInputs(),
                    "parms": parms,
                }
            )
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    finally:
        root.destroy()


if __name__ == "__main__":
    main()
