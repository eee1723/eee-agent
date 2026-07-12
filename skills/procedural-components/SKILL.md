---
name: procedural-components
description: Component + anchor + parameter DAG recipe for parametric models that expose adjustable user parameters. Use when the model must be parameter-driven (user tweaks a param → connected parts update correctly), e.g. furniture, buildings with knobs, any "configurable" asset.
---

# Procedural Components (parametric, adjustable-output modeling)

Build a model as a **DAG of components** inside one work-container subnet, where a
set of **root parameters** (exposed on the container, with min/max ranges) drive
everything via `ch()` expressions, and components pass **anchor point clouds** to
their children over **real output→input port wires** (not object_merge). Change
any root param → cook propagates → all dependents update correctly.

## How ports work (verified Houdini 21)
A component is a SOP **subnet** that exposes real **OUTPUT PORTS** via internal
`output` nodes (a vanilla subnet otherwise has only one effective output):
- `geo_port` — **output port 0** = the component's geometry (`OUT_geo` tap).
- `anchors_port` — **output port 1** = its anchor points (`OUT_anchors` tap).

A child consumes a parent's anchors by connecting its own **input** port to the
parent's `anchors_port` (real wire in the work subnet — `wire_anchor` does this
and creates an `in_anchors_<parent>` tap inside the child). The final assembly is
a real `merge` of every component's `geo_port` (port 0). Because every dependency
is a real wire, the work subnet's **auto-layout follows the DAG**.

## Contract & naming (enforced by tools)
- Work container = a subnet created by `ensure_work_container`, marked `__proc_root__`.
- Root params = spare parms named `p_<name>` (added by `add_root_parm`, with min/max/strict). Single source of truth.
- Component = subnet `comp_<name>` (made by `make_component`), containing `OUT_geo` + `OUT_anchors` nulls promoted to ports `geo_port`(0) and `anchors_port`(1).
- Anchors = points with `@P @orient @scale @N @up` + `s@anchor_type` (e.g. "leg","mat") + `s@piece` (variant id).
- Dependency = `wire_anchor(producer, consumer)` connects consumer input ← producer `anchors_port`; returns the consumer's `in_anchors_<producer>` node to read the anchors from. DAG — cycles are refused.
- Drive a parm with `set_expression(node, parm, root_parm=...)` — the tool computes the relative `ch("../p_<name>")` path; NEVER hand-write relative paths.

## Workflow
1. `ensure_work_container()` — get/create the work subnet. Call first.
2. `add_root_parm(name, type, size, default, min, max, strict)` for every user-facing knob (e.g. width, depth, thickness, leg_count). **Give each a sensible min/max range** so users can adjust it (strict=True to clamp).
3. `make_component("<root>")` — the root component (e.g. tabletop). It now has `geo_port`(0) + `anchors_port`(1). Build its geometry with `set_expression(..., root_parm=...)` so dims follow root params; wire your final geo node into `OUT_geo`.
4. Generate the root's **anchor points** (a node producing points at child attach positions, positions driven by root params via `ch()`). `expose_anchors(root_comp, anchor_node, anchor_type=...)` to wire it into `OUT_anchors` (→ port 1).
5. For each child: `make_component("<child>")` → `wire_anchor(parent, child)` (connects the child's input to the parent's `anchors_port`; returns the child's `in_anchors_<parent>` node) → build the child geometry, usually `copy_to_points` (pack=on) instancing a unit onto the anchors — connect its **"points" input to the `in_anchors_<parent>` node** the call returned. `expose_anchors` if the child itself has grandchild anchors.
6. `assemble_output([all comp_*])` → a real `merge` in the work container wired to each component's `geo_port` (port 0). This is the final output.
7. `work_status()` / `anchor_graph()` to verify wiring (no cycles, no unconsumed anchors you intended to consume). `cook_node` + `geometry_stats` after each step — but only when you actually need to verify; not after every micro-step.
8. `export_geometry` + `save_hip`. To produce a variant: change a root `p_*` parm (via `set_parms` on the work container) and re-export.

## Example: a parametric table
- Root params: `p_width`(0.2..5), `p_depth`(0.2..3), `p_top_thick`(0.01..0.5), `p_leg_thick`(0.02..0.3), `p_leg_len`(0.2..2), `p_leg_count`(3..8 int).
- `comp_tabletop`: a box with `sizex=ch(p_width)`, `sizey=ch(p_depth)`, `sizez=ch(p_top_thick)`, wired into `OUT_geo`.
- Anchors: generate `p_leg_count` points at the tabletop corners, each `@P` = (±width/2, -top_thick/2, ±depth/2) via a wrangle using `ch("p_width")`/`ch("p_depth")`; tag `s@anchor_type="leg"`. `expose_anchors`.
- `comp_legs`: `wire_anchor(tabletop, legs)` → returns `…/comp_legs/in_anchors_tabletop`; a leg box (`sizex=sizey=ch(p_leg_thick)`, `sizez=ch(p_leg_len)`); `copy_to_points` (pack) with its points input wired to `in_anchors_tabletop`; result into `OUT_geo`.
- `assemble_output([comp_tabletop, comp_legs])` → final merge.
- Result: change `p_width` → tabletop widens AND the 4 legs slide to the new corners (anchors recomputed). Change `p_leg_thick` → only legs thicken. This is the parametric stability you want.

## Gotchas
- Read the anchors inside a child from the `in_anchors_<parent>` node returned by `wire_anchor` — never hand-wire to a parent's internals and never use `object_merge` for anchors.
- `make_component` pre-creates both ports; a leaf component's `anchors_port` simply stays empty (harmless).
- `set_expression` computes relative paths — always pass `root_parm=`, never write `ch("../..")` yourself.
- Anchors are POINTS; if a component has no children, don't bother generating anchors.
- `anchor_graph` flags unconsumed anchors only for components that actually produce them (OUT_anchors wired) — intentional for leaves, a wiring miss for intermediates.
- cook when you need to confirm a result (counts/bbox, after a boolean, after VEX) — not reflexively after every parm tweak.
