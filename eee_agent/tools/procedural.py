"""Component-based procedural modeling tools.

Implements the locked architecture (Phase C): a SOP **subnet** work container
under a geo, with **spare parms** as the user-facing parameter interface (the
single source of truth), **component subnets** (`comp_*`) each with `OUT_geo` +
`OUT_anchors`, and **anchor** dependencies wired via `object_merge` path refs
(DAG, cycle-checked). Components reference root parms via `ch("../p_nameX")`
expressions (relative path computed by the tool, never hand-written by the agent).

All HOM APIs below were verified against Houdini 21.0.440 via hython:
subnet, addSpareParmTuple, setExpression('ch("../p_x")'), object_merge objpath1,
copytopoints pack.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from langchain_core.tools import tool

from eee_agent.bridge import hou_client, serialize

MARKER_PARM = "__proc_root__"
COMP_PREFIX = "comp_"
DEFAULT_GEO = "proc_geo"

# module-level cache of the active work container path (survives across tool
# calls within one agent process)
_WORK: Dict[str, Optional[str]] = {"path": None}


# --------------------------------------------------------------------------- #
# helpers (run inside run() with the live hou proxy)
# --------------------------------------------------------------------------- #
def _rel(from_path: str, to_path: str) -> str:
    """Relative network path from `from_path` to `to_path` (Houdini op syntax)."""
    f = from_path.strip("/").split("/")
    t = to_path.strip("/").split("/")
    i = 0
    while i < len(f) and i < len(t) and f[i] == t[i]:
        i += 1
    ups = len(f) - i
    downs = t[i:]
    return "../" * ups + "/".join(downs)


def _is_work(node) -> bool:
    try:
        if str(node.type().name()) != "subnet":
            return False
        p = node.parm(MARKER_PARM)
        return p is not None and int(p.eval()) == 1
    except Exception:
        return False


def _discover_work(hou) -> Optional[str]:
    obj = hou.node("/obj")
    if obj is None:
        return None
    for geo in obj.children():
        for child in geo.children():
            if _is_work(child):
                return str(child.path())
    return None


def _get_work(hou):
    path = _WORK.get("path")
    if path:
        n = hou.node(path)
        if n is not None and _is_work(n):
            return n
    path = _discover_work(hou)
    if path:
        _WORK["path"] = path
        return hou.node(path)
    raise RuntimeError("no work container — call ensure_work_container first")


def _spare_parm_templates(hou, name: str, parm_type: str, size: int, default, label: str):
    """Build the right ParmTemplate for the type. Verified Float; Int/String same shape."""
    label = label or name
    if parm_type == "float":
        dv = tuple(float(x) for x in (default or [0.0] * size))
        return hou.FloatParmTemplate(name, label, size, dv)
    if parm_type == "int":
        dv = tuple(int(x) for x in (default or [0] * size))
        return hou.IntParmTemplate(name, label, size, dv)
    if parm_type == "string":
        dv = tuple(str(x) for x in (default or [""] * size))
        return hou.StringParmTemplate(name, label, size, dv)
    if parm_type == "toggle":
        return hou.IntParmTemplate(name, label, 1, (1 if default else 0,))
    raise ValueError(f"unsupported parm_type: {parm_type}")


def _components(hou, work) -> List[str]:
    return [str(c.path()) for c in work.children()
            if str(c.name()).startswith(COMP_PREFIX) and str(c.type().name()) == "subnet"]


def _producer_of_object_merge(hou, om, work) -> Optional[str]:
    """Resolve an object_merge's objpath1 to the owning component path, or None."""
    try:
        raw = str(om.parm("objpath1").eval())
    except Exception:
        return None
    if not raw:
        return None
    # resolve relative to the object_merge node, then find comp_ ancestor under work
    try:
        target = om.node(raw)
    except Exception:
        target = None
    if target is None:
        return None
    tp = str(target.path())
    wp = str(work.path())
    if not tp.startswith(wp + "/"):
        return None
    rest = tp[len(wp) + 1:].split("/")
    if rest and rest[0].startswith(COMP_PREFIX):
        return wp + "/" + rest[0]
    return None


# --------------------------------------------------------------------------- #
# tools
# --------------------------------------------------------------------------- #
@tool
def ensure_work_container(name: str = "proc_model") -> dict:
    """Get or create the procedural-modeling work container.

    Workflow: if the user has selected a subnet that is one of our work containers
    (marked with the ``__proc_root__`` spare parm), continue in it. If a work
    container already exists, reuse it. Otherwise create a new geo + work subnet
    (marked) and use it. ALL modeling must happen inside this container. Call this
    first, before any component/parm work.

    Returns {mode: "continue"|"created", path}.
    """
    def _fn(hou):
        # 1. user selection
        try:
            for n in hou.selectedNodes():
                if _is_work(n):
                    _WORK["path"] = str(n.path())
                    return {"mode": "continue", "path": str(n.path()), "note": "selected work container"}
        except Exception:
            pass
        # 2. discover existing
        existing = _discover_work(hou)
        if existing:
            _WORK["path"] = existing
            return {"mode": "continue", "path": existing, "note": "reused existing work container"}
        # 3. create
        obj = hou.node("/obj")
        geo = hou.node("/obj/" + DEFAULT_GEO)
        if geo is None:
            geo = obj.createNode("geo", DEFAULT_GEO)
        work = geo.createNode("subnet", name or "proc_model")
        marker = hou.IntParmTemplate(MARKER_PARM, "Proc Root Marker", 1, (1,))
        work.addSpareParmTuple(marker)
        _WORK["path"] = str(work.path())
        return {"mode": "created", "path": str(work.path())}

    res, err = hou_client.run(_fn)
    if err is not None:
        return {"ok": False, "error": err}
    return {"ok": True, **res}


@tool
def add_root_parm(name: str, parm_type: str = "float", size: int = 1,
                  default: Optional[List[float]] = None, label: str = "") -> dict:
    """Add a user-facing parameter to the work container (the single source of truth).

    The parm is stored as a spare parm named ``p_<name>`` on the work container
    (the ``p_`` prefix avoids clashing with subnet built-ins). Components reference
    it via ``ch("<relative>p_<name><component>")`` — use set_expression with
    root_parm=<name> and the tool computes the relative path automatically.

    Args:
      name: parm name (without p_ prefix), e.g. "width".
      parm_type: float | int | string | toggle.
      size: channel count (1 scalar, 3 vector, ...). toggle ignores this.
      default: list of default values (length = size).
      label: UI label.
    """
    pname = "p_" + name
    comps = [pname + ("xyzw"[i] if size > 1 else "") for i in range(size)]

    def _fn(hou):
        work = _get_work(hou)
        if work.parmTuple(pname) is not None:
            return {"name": pname, "exists": True, "components": comps}
        tpl = _spare_parm_templates(hou, pname, parm_type, size, default, label)
        work.addSpareParmTuple(tpl)
        return {"name": pname, "components": comps, "default": default}

    res, err = hou_client.run(_fn)
    if err is not None:
        return {"ok": False, "error": err}
    return {"ok": True, **res}


@tool
def make_component(name: str) -> dict:
    """Create a component subnet inside the work container.

    Scaffolds two null outputs inside it: ``OUT_geo`` (the component's geometry)
    and ``OUT_anchors`` (the anchor point cloud it produces for children to
    consume). Build the component's nodes, then wire your final geo into OUT_geo
    and your anchor-generating node into OUT_anchors (via expose_anchors).
    """
    comp_name = COMP_PREFIX + name

    def _fn(hou):
        work = _get_work(hou)
        if work.node(comp_name) is not None:
            return {"component": str(work.path()) + "/" + comp_name, "exists": True}
        comp = work.createNode("subnet", comp_name)
        comp.createNode("null", "OUT_geo")
        comp.createNode("null", "OUT_anchors")
        return {"component": str(comp.path()),
                "out_geo": str(comp.path()) + "/OUT_geo",
                "out_anchors": str(comp.path()) + "/OUT_anchors"}

    res, err = hou_client.run(_fn)
    if err is not None:
        return {"ok": False, "error": err}
    return {"ok": True, **res}


@tool
def expose_anchors(component_path: str, node_path: str, anchor_type: str = "") -> dict:
    """Designate `node_path` as the anchor output of `component_path`.

    Wires node_path -> the component's OUT_anchors (pass-through). The anchor
    points should carry ``s@anchor_type`` (set via set_vex) so consumers can
    filter. Records anchor_type as metadata on the component.
    """
    def _fn(hou):
        comp = hou.node(component_path)
        src = hou.node(node_path)
        if comp is None:
            raise RuntimeError(f"component not found: {component_path}")
        if src is None:
            raise RuntimeError(f"node not found: {node_path}")
        out = comp.node("OUT_anchors")
        if out is None:
            out = comp.createNode("null", "OUT_anchors")
        out.setInput(0, src)
        if anchor_type:
            try:
                comp.setComment("anchor_type=" + anchor_type)
            except Exception:
                pass
        return {"component": component_path, "out_anchors": str(out.path()),
                "anchor_type": anchor_type}

    res, err = hou_client.run(_fn)
    if err is not None:
        return {"ok": False, "error": err}
    return {"ok": True, **res}


@tool
def wire_anchor(producer_component: str, consumer_component: str) -> dict:
    """Wire producer's OUT_anchors into the consumer (creates an object_merge in
    the consumer referencing the producer's anchors). Establishes the dependency
    edge producer -> consumer. Refuses if it would create a cycle.

    Both must be `comp_*` subnets under the same work container.
    """
    def _fn(hou):
        work = _get_work(hou)
        prod = hou.node(producer_component)
        cons = hou.node(consumer_component)
        if prod is None or cons is None:
            raise RuntimeError("producer or consumer not found")
        prod_anchors = prod.node("OUT_anchors")
        if prod_anchors is None:
            raise RuntimeError(f"producer has no OUT_anchors: {producer_component}")

        # cycle check: would adding producer->consumer close a loop? i.e. does
        # consumer already (transitively) depend on producer?
        edges = _build_edges(hou, work)
        if _reaches(edges, consumer_component, producer_component):
            raise RuntimeError("cycle: consumer already depends on producer")

        om_name = "in_anchors_" + str(prod.name())
        om = cons.node(om_name)
        if om is None:
            om = cons.createNode("object_merge", om_name)
        rel = _rel(str(om.path()), str(prod_anchors.path()))
        om.parm("objpath1").set(rel)
        return {"edge": f"{producer_component} -> {consumer_component}",
                "object_merge": str(om.path()), "objpath": rel}

    res, err = hou_client.run(_fn)
    if err is not None:
        return {"ok": False, "error": err}
    return {"ok": True, **res}


def _build_edges(hou, work) -> List[List[str]]:
    """All anchor edges [producer_comp, consumer_comp] under work."""
    edges = []
    for comp_path in _components(hou, work):
        comp = hou.node(comp_path)
        for child in comp.children():
            if str(child.type().name()) == "object_merge":
                prod = _producer_of_object_merge(hou, child, work)
                if prod:
                    edges.append([prod, comp_path])
    return edges


def _reaches(edges: List[List[str]], src: str, dst: str) -> bool:
    """Does dst depend on src? i.e. is there a path src -> ... -> dst (producer->consumer)?
    Cycle when adding producer->consumer: occurs if consumer already reaches producer
    as a producer (consumer -> ... -> producer). We check: from consumer, can we reach
    producer following consumer->producer (reverse) edges? Equivalent: does producer
    already depend on consumer? Here edges are producer->consumer, so 'consumer depends
    on producer' = producer->...->consumer exists. We refuse if producer->consumer is
    already reachable (i.e. producer can already reach consumer)."""
    # producer can reach consumer already?
    adj = {}
    for p, c in edges:
        adj.setdefault(p, []).append(c)
    stack = [src]
    seen = set()
    while stack:
        n = stack.pop()
        if n == dst:
            return True
        if n in seen:
            continue
        seen.add(n)
        stack.extend(adj.get(n, []))
    return False


@tool
def set_expression(node_path: str, parm: str, root_parm: Optional[str] = None,
                   expr: Optional[str] = None, component: str = "x") -> dict:
    """Set a channel-reference expression on a parm, driving it from a root work parm.

    Prefer root_parm=: the tool computes the correct relative ``ch("../p_<name><component>")``
    path from this node to the work container (don't hand-write relative paths).
    Use expr= only for non-root expressions.

    Args:
      node_path: node whose parm to drive.
      parm: parm name on that node (e.g. "sizex").
      root_parm: root parm name (without p_ prefix), e.g. "width".
      component: which channel of the root parm ("x","y","z","w"); default x.
      expr: raw expression (overrides root_parm).
    """
    def _fn(hou):
        node = hou.node(node_path)
        if node is None:
            raise RuntimeError(f"node not found: {node_path}")
        p = node.parm(parm)
        if p is None:
            raise RuntimeError(f"parm not found: {parm} on {node_path}")
        if expr is not None:
            expression = expr
        elif root_parm is not None:
            work = _get_work(hou)
            rel = _rel(str(node.path()), str(work.path()))
            # scalar (size-1) parm is named p_<name>; multi-component uses p_<name>x/y/z.
            # Existence of the bare p_<name> parm tells us it's scalar -> no suffix.
            scalar = work.parm("p_" + root_parm)
            suffix = component if scalar is None else ""
            expression = f'ch("{rel}p_{root_parm}{suffix}")'
        else:
            raise RuntimeError("provide root_parm or expr")
        p.setExpression(expression)
        return {"node": node_path, "parm": parm, "expression": expression,
                "eval": float(p.eval())}

    res, err = hou_client.run(_fn)
    if err is not None:
        return {"ok": False, "error": err}
    return {"ok": True, **res}


@tool
def assemble_output(component_paths: List[str], name: str = "OUT") -> dict:
    """Merge every component's OUT_geo into the work container's final output.

    Uses an ``object_merge`` with path references (cross-subnet safe — ``merge`` via
    setInput cannot cross subnet boundaries, but object_merge path refs can). This is
    the final assembly step: call after all components are built and wired.
    """
    def _fn(hou):
        work = _get_work(hou)
        om = work.createNode("object_merge", name)
        om.parm("numobj").set(len(component_paths))
        paths = []
        for i, cp in enumerate(component_paths):
            out_geo_path = cp.rstrip("/") + "/OUT_geo"
            rel = _rel(str(om.path()), out_geo_path)
            om.parm("objpath" + str(i + 1)).set(rel)
            paths.append(out_geo_path)
        return {"out": str(om.path()), "merged": paths}

    res, err = hou_client.run(_fn)
    if err is not None:
        return {"ok": False, "error": err}
    return {"ok": True, **res}


@tool
def work_status() -> dict:
    """Dump the current work container's structure: spare parms (name/type/value),
    components (their OUT_geo/OUT_anchors + anchor_type), anchor edges (who->who),
    and cook health. Call this to know exactly where the build stands."""
    def _fn(hou):
        work = _get_work(hou)
        # spare parms (spareParms() — verified; spareParmTuples() does not exist)
        parms = []
        try:
            for p in work.spareParms():
                nm = str(p.name())
                if nm.startswith("__"):
                    continue  # internal marker
                try:
                    val = p.eval()
                except Exception:
                    val = None
                parms.append({"name": nm, "value": val})
        except Exception:
            pass
        # components
        comps = []
        for cp in _components(hou, work):
            comp = hou.node(cp)
            comps.append({
                "path": cp,
                "out_geo": (str(comp.node("OUT_geo").path()) if comp.node("OUT_geo") else None),
                "out_anchors": (str(comp.node("OUT_anchors").path()) if comp.node("OUT_anchors") else None),
                "comment": str(comp.comment() or ""),
            })
        # edges
        edges = _build_edges(hou, work)
        return {"work": str(work.path()), "parms": parms, "components": comps,
                "anchor_edges": edges}

    res, err = hou_client.run(_fn)
    if err is not None:
        return {"ok": False, "error": err}
    return {"ok": True, **res}


@tool
def anchor_graph() -> dict:
    """Report the anchor dependency DAG: edges, cycles, and unconsumed anchors
    (components whose OUT_anchors nobody consumes). Use to verify the build is
    wired correctly before exporting."""
    def _fn(hou):
        work = _get_work(hou)
        comps = _components(hou, work)
        edges = _build_edges(hou, work)
        consumed = {e[0] for e in edges}
        unconsumed = [c for c in comps
                      if c not in consumed
                      and hou.node(c).node("OUT_anchors") is not None]
        # cycle detection (DFS)
        adj = {}
        for p, c in edges:
            adj.setdefault(p, []).append(c)
        cycles = []
        for start in comps:
            stack = [(start, [start])]
            while stack:
                n, path = stack.pop()
                for nxt in adj.get(n, []):
                    if nxt in path:
                        cycles.append(path + [nxt])
                    else:
                        stack.append((nxt, path + [nxt]))
        return {"edges": edges, "unconsumed_anchors": unconsumed,
                "cycles": cycles[:5], "components": comps}

    res, err = hou_client.run(_fn)
    if err is not None:
        return {"ok": False, "error": err}
    return {"ok": True, **res}
