"""Pure-Python serialization of hou objects (ran client-side on rpyc proxies).

Everything here must return *plain* Python (dicts/lists/str/int/float) so the
agent never touches an rpyc proxy. Scalars come back from method calls fine over
rpyc; we just must avoid operator overloading on proxies.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional


def _type_name(node) -> Optional[str]:
    try:
        return str(node.type().name())
    except Exception:
        return None


def _vec3(v) -> List[float]:
    """Read a hou.Vector3 (proxied) into a plain list of floats."""
    return [float(v.x()), float(v.y()), float(v.z())]


def node_info(node) -> Dict[str, Any]:
    info = {
        "path": str(node.path()),
        "name": str(node.name()),
        "type": _type_name(node),
    }
    try:
        info["inputs"] = [str(i.path()) if i is not None else None for i in node.inputs()]
    except Exception:
        info["inputs"] = []
    return info


def bbox_dict(bb) -> Dict[str, List[float]]:
    mn = _vec3(bb.minvec())
    mx = _vec3(bb.maxvec())
    return {
        "min": mn,
        "max": mx,
        "size": [mx[i] - mn[i] for i in range(3)],
    }


def attrib_dict(attrib) -> Dict[str, Any]:
    return {
        "name": str(attrib.name()),
        "type": str(attrib.type()).replace("attribType.", ""),
        "data_type": str(attrib.dataType()).replace("attribData.", ""),
        "size": int(attrib.size()),
    }


def _attrib_list(attribs) -> List[Dict[str, Any]]:
    out = []
    for a in attribs:
        try:
            out.append(attrib_dict(a))
        except Exception:
            continue
    return out


def geometry_summary(geo) -> Dict[str, Any]:
    """Full stats dict for a cooked hou.Geometry (proxied)."""
    bb = None
    try:
        bb = bbox_dict(geo.boundingBox())
    except Exception:
        # Empty geometry has no bounding box.
        pass
    return {
        "points": int(geo.pointCount()),
        "prims": int(geo.primCount()),
        "bbox": bb,
        "point_attribs": _attrib_list(geo.pointAttribs()),
        "prim_attribs": _attrib_list(geo.primAttribs()),
        "detail_attribs": _attrib_list(geo.globalAttribs()),
    }
