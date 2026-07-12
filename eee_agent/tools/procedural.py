"""Component-based procedural modeling tools.

Implements the locked architecture (Phase C): a SOP **subnet** work container
under a geo, with **spare parms** (with min/max ranges) as the user-facing
parameter interface (the single source of truth), and **component subnets**
(`comp_*`) that expose real OUTPUT PORTS via internal `output` nodes:
`geo_port` (port 0 = the component's geometry) and `anchors_port`
(port 1 = its anchor points). Anchor dependencies are wired as REAL connections
between component subnets (consumer input <- producer anchors port) — no
object_merge path refs — so the work subnet's auto-layout follows the DAG.
Components reference root parms via `ch("../p_nameX")` expressions (relative
path computed by the tool, never hand-written by the agent).

All HOM APIs below were verified against Houdini 21.0.440 via hython:
subnet, addSpareParmTuple, setExpression('ch("../p_x")'), the `output` SOP node +
its `outputidx` parm (multi-output subnet ports — a vanilla subnet's *effective*
output is normally just the render-flag node; `output` nodes with explicit
outputidx promote distinct routable ports), setInput(input_idx, node, output_idx),
subnet indirectInputs(), copytopoints pack, FloatParmTemplate
setMinValue/setMaxValue/setMinIsStrict.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from langchain_core.tools import tool

from eee_agent.bridge import hou_client, serialize

MARKER_PARM = "__proc_root__"
COMP_PREFIX = "comp_"
DEFAULT_GEO = "proc_geo"

# component subnet OUTPUT PORTS — promoted by internal `output` nodes
GEO_PORT = "geo_port"          # outputidx 0 — the component's geometry
ANCHORS_PORT = "anchors_port"  # outputidx 1 — anchor points for children
GEO_OUTIDX = 0
ANCHORS_OUTIDX = 1

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


def _spare_parm_templates(hou, name: str, parm_type: str, size: int, default, label: str,
                          mn: Optional[float], mx: Optional[float], strict: bool):
    """Build the right ParmTemplate for the type, applying min/max range limits.

    Float/int parms get a slider range (mn..mx); if strict, the bounds are hard
    clamps (user cannot exceed them). Verified: FloatParmTemplate has
    setMinValue/setMaxValue + setMinIsStrict/setMaxIsStrict.
    """
    label = label or name

    def _apply_rng(tpl):
        try:
            if mn is not None:
                tpl.setMinValue(float(mn))
                if strict:
                    tpl.setMinIsStrict(True)
            if mx is not None:
                tpl.setMaxValue(float(mx))
                if strict:
                    tpl.setMaxIsStrict(True)
        except Exception:
            pass  # range not applicable to this template type
        return tpl

    if parm_type == "float":
        dv = tuple(float(x) for x in (default or [0.0] * size))
        return _apply_rng(hou.FloatParmTemplate(name, label, size, dv))
    if parm_type == "int":
        dv = tuple(int(x) for x in (default or [0] * size))
        return _apply_rng(hou.IntParmTemplate(name, label, size, dv))
    if parm_type == "string":
        dv = tuple(str(x) for x in (default or [""] * size))
        return hou.StringParmTemplate(name, label, size, dv)
    if parm_type == "toggle":
        return hou.IntParmTemplate(name, label, 1, (1 if default else 0,))
    raise ValueError(f"unsupported parm_type: {parm_type}")


def _components(hou, work) -> List[str]:
    return [str(c.path()) for c in work.children()
            if str(c.name()).startswith(COMP_PREFIX) and str(c.type().name()) == "subnet"]


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
                  default: Optional[List[float]] = None, label: str = "",
                  min: Optional[float] = None, max: Optional[float] = None,
                  strict: bool = False) -> dict:
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
      min: minimum value (sets the slider range; applies to all channels). float/int only.
      max: maximum value (sets the slider range; applies to all channels). float/int only.
      strict: if True, min/max are hard clamps (user cannot exceed them); else soft range.
    """
    pname = "p_" + name
    comps = [pname + ("xyzw"[i] if size > 1 else "") for i in range(size)]

    def _fn(hou):
        work = _get_work(hou)
        if work.parmTuple(pname) is not None:
            return {"name": pname, "exists": True, "components": comps}
        tpl = _spare_parm_templates(hou, pname, parm_type, size, default, label, min, max, strict)
        work.addSpareParmTuple(tpl)
        return {"name": pname, "components": comps, "default": default,
                "min": min, "max": max, "strict": strict}

    res, err = hou_client.run(_fn)
    if err is not None:
        return {"ok": False, "error": err}
    return {"ok": True, **res}


@tool
def make_component(name: str) -> dict:
    """Create a component subnet inside the work container.

    Scaffolds two null taps — ``OUT_geo`` (your final geometry) and ``OUT_anchors``
    (your anchor points) — promoted to real subnet OUTPUT PORTS by two ``output``
    nodes: ``geo_port`` (output port 0) and ``anchors_port`` (output port 1). Wire
    your final geo into ``OUT_geo``; expose your anchor node via expose_anchors
    (which wires it into ``OUT_anchors``). Other components consume your anchors by
    connecting their input to your port 1 (see wire_anchor). assemble_output merges
    every component's port 0 (geo).
    """
    comp_name = COMP_PREFIX + name

    def _fn(hou):
        work = _get_work(hou)
        if work.node(comp_name) is not None:
            return {"component": str(work.path()) + "/" + comp_name, "exists": True}
        comp = work.createNode("subnet", comp_name)
        out_geo = comp.createNode("null", "OUT_geo")
        out_anc = comp.createNode("null", "OUT_anchors")
        # promote ports via `output` nodes with explicit outputidx (verified: a
        # vanilla subnet's distinct outputs require explicit outputidx on each)
        gp = comp.createNode("output", GEO_PORT)
        gp.setInput(0, out_geo)
        gp.parm("outputidx").set(GEO_OUTIDX)
        ap = comp.createNode("output", ANCHORS_PORT)
        ap.setInput(0, out_anc)
        ap.parm("outputidx").set(ANCHORS_OUTIDX)
        return {"component": str(comp.path()),
                "out_geo": str(out_geo.path()),
                "out_anchors": str(out_anc.path()),
                "geo_port": GEO_OUTIDX, "anchors_port": ANCHORS_OUTIDX}

    res, err = hou_client.run(_fn)
    if err is not None:
        return {"ok": False, "error": err}
    return {"ok": True, **res}


@tool
def expose_anchors(component_path: str, node_path: str, anchor_type: str = "") -> dict:
    """Designate `node_path` as the anchor output of `component_path`.

    Wires node_path -> the component's OUT_anchors (which is already promoted to
    the component's anchors_port, output port 1). The anchor points should carry
    ``s@anchor_type`` (set via set_vex) so consumers can filter. Records anchor_type
    as metadata on the component.
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
        # ensure the anchors_port promoter exists (defensive for older components)
        ap = comp.node(ANCHORS_PORT)
        if ap is None:
            ap = comp.createNode("output", ANCHORS_PORT)
            ap.setInput(0, out)
            ap.parm("outputidx").set(ANCHORS_OUTIDX)
        if anchor_type:
            try:
                comp.setComment("anchor_type=" + anchor_type)
            except Exception:
                pass
        return {"component": component_path, "out_anchors": str(out.path()),
                "anchors_port": ANCHORS_OUTIDX, "anchor_type": anchor_type}

    res, err = hou_client.run(_fn)
    if err is not None:
        return {"ok": False, "error": err}
    return {"ok": True, **res}


@tool
def wire_anchor(producer_component: str, consumer_component: str,
                consumer_input: int = 0) -> dict:
    """Connect a consumer's INPUT port to a producer's anchor OUTPUT port, so the
    consumer can instance children onto the producer's anchors. This is a REAL wire
    in the work subnet (not an object_merge path ref) — the dependency is visible
    to auto-layout.

    Creates an ``in_anchors`` null inside the consumer (wired from the consumer's
    external input connector). Connect your ``copy_to_points`` "points" input to
    that node (its path is in the result) to consume the anchors. Establishes the
    dependency edge producer -> consumer. Refuses if it would create a cycle.

    Args:
      producer_component: path of the comp_* that produces anchors (OUT_anchors wired).
      consumer_component: path of the comp_* that consumes them.
      consumer_input: which input connector on the consumer to use (0..3); default 0.
    """
    def _fn(hou):
        work = _get_work(hou)
        prod = hou.node(producer_component)
        cons = hou.node(consumer_component)
        if prod is None or cons is None:
            raise RuntimeError("producer or consumer not found")
        prod_anchors_port = prod.node(ANCHORS_PORT)
        out_anc = prod.node("OUT_anchors")
        if prod_anchors_port is None or out_anc is None or out_anc.input(0) is None:
            raise RuntimeError(f"producer has no anchors exposed: {producer_component}")

        # cycle check: adding producer->consumer must not close a loop
        edges = _build_edges(hou, work)
        if _reaches(edges, consumer_component, producer_component):
            raise RuntimeError("cycle: consumer already depends on producer")

        # real wire in the WORK subnet: consumer input <- producer anchors port
        out_idx = int(prod_anchors_port.parm("outputidx").eval())
        cons.setInput(consumer_input, prod, out_idx)

        # internal tap so the agent can reference the incoming anchors by path
        prod_short = str(prod.name())[len(COMP_PREFIX):] or "src"
        in_name = "in_anchors_" + prod_short
        in_node = cons.node(in_name)
        if in_node is None:
            in_node = cons.createNode("null", in_name)
        in_node.setInput(0, cons.indirectInputs()[consumer_input])
        return {"edge": f"{producer_component} -> {consumer_component}",
                "consumer_anchors_input": str(in_node.path()),
                "producer_anchors_port": out_idx,
                "consumer_input": consumer_input}

    res, err = hou_client.run(_fn)
    if err is not None:
        return {"ok": False, "error": err}
    return {"ok": True, **res}


def _build_edges(hou, work) -> List[List[str]]:
    """All anchor edges [producer_comp, consumer_comp] under work, read from the
    REAL input wires between component subnets (consumer input <- producer).
    Inter-component wires are anchor wires by convention (geometry only flows to
    the final merge, which is not a component)."""
    comps = _components(hou, work)
    compset = set(comps)
    edges = []
    for cp in comps:
        cons = hou.node(cp)
        try:
            for src, _out_idx, _in_idx in cons.inputsWithIndices():
                if src is None:
                    continue
                sp = str(src.path())
                if sp in compset and sp != cp:
                    edges.append([sp, cp])
        except Exception:
            pass
    return edges


def _reaches(edges: List[List[str]], src: str, dst: str) -> bool:
    """Following producer->consumer edges, can we reach dst from src?
    Used for cycle detection: adding producer->consumer closes a loop iff consumer
    already reaches producer, i.e. _reaches(edges, consumer, producer)."""
    adj: Dict[str, List[str]] = {}
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
    """Merge every component's geometry (output port 0) into the work container's
    final output via a real ``merge`` node wired to each component. Real wires (no
    object_merge) so the work subnet auto-layouts in dependency order. Call after
    all components are built and wired.
    """
    def _fn(hou):
        work = _get_work(hou)
        mg = work.createNode("merge", name)
        for i, cp in enumerate(component_paths):
            comp = hou.node(cp)
            if comp is None:
                raise RuntimeError(f"component not found: {cp}")
            mg.setInput(i, comp, GEO_OUTIDX)  # each component's geo port (output 0)
        return {"out": str(mg.path()), "merged": list(component_paths)}

    res, err = hou_client.run(_fn)
    if err is not None:
        return {"ok": False, "error": err}
    return {"ok": True, **res}


@tool
def work_status() -> dict:
    """Dump the current work container's structure: spare parms (name/value),
    components (their OUT_geo/OUT_anchors ports + anchor_type), and anchor edges
    (who->who, read from the real wires). Call this to know where the build stands."""
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
            out_anc = comp.node("OUT_anchors")
            has_anchors = out_anc is not None and out_anc.input(0) is not None
            comps.append({
                "path": cp,
                "out_geo": (str(comp.node("OUT_geo").path()) if comp.node("OUT_geo") else None),
                "anchors_port": ANCHORS_OUTIDX if has_anchors else None,
                "comment": str(comp.comment() or ""),
            })
        # edges (real wires)
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
    (components that PRODUCE anchors — OUT_anchors wired — that nobody consumes).
    Use to verify the build is wired correctly before exporting."""
    def _fn(hou):
        work = _get_work(hou)
        comps = _components(hou, work)
        edges = _build_edges(hou, work)
        consumed = {e[0] for e in edges}
        unconsumed = []
        for c in comps:
            out_anc = hou.node(c).node("OUT_anchors")
            produces = out_anc is not None and out_anc.input(0) is not None
            if produces and c not in consumed:
                unconsumed.append(c)
        # cycle detection (DFS)
        adj: Dict[str, List[str]] = {}
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
