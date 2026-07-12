---
name: procedural-components
description: Component + anchor + parameter DAG recipe for parametric models that expose adjustable user parameters. Use when the model must be parameter-driven (user tweaks a param → connected parts update correctly), e.g. furniture, buildings with knobs, any "configurable" asset.
---

# Procedural Components (parametric, adjustable-output modeling)

Build a model as a **DAG of components** inside one work-container subnet, where a
set of **root parameters** (exposed on the container) drive everything via `ch()`
expressions, and components pass **anchor point clouds** to their children. Change
any root param → cook propagates → all dependents update correctly.

## Contract & naming (enforced by tools)
- Work container = a subnet created by `ensure_work_container`, marked `__proc_root__`.
- Root params = spare parms named `p_<name>` (added by `add_root_parm`). Single source of truth.
- Component = subnet `comp_<name>` (made by `make_component`), containing `OUT_geo` (its geometry) and `OUT_anchors` (point cloud it produces).
- Anchors = points with `@P @orient @scale @N @up` + `s@anchor_type` (e.g. "leg","mat") + `s@piece` (variant id).
- Dependency = `wire_anchor(producer, consumer)` creates an `object_merge` in the consumer referencing the producer's `OUT_anchors` (relative path). DAG — cycles are refused.
- Drive a parm with `set_expression(node, parm, root_parm=...)` — the tool computes the relative `ch("../p_<name>")` path; NEVER hand-write relative paths.

## Workflow
1. `ensure_work_container()` — get/create the work subnet. Call first.
2. `add_root_parm(name, type, size, default)` for every user-facing knob (e.g. width, depth, thickness, leg_count, leg_thickness).
3. `make_component("<root>")` — the root component (e.g. tabletop). Build its geometry with `set_expression(..., root_parm=...)` so its dims follow root params.
4. Generate the root's **anchor points** (a node producing points at child attach positions), with positions driven by root params via `ch()`. `expose_anchors(root_comp, anchor_node, anchor_type=...)` to wire it into `OUT_anchors`.
5. For each child: `make_component("<child>")` → `wire_anchor(parent, child)` (consumes parent anchors) → build child geometry, usually `copy_to_points` (pack=on) instancing a unit at the anchor points, with its own dims driven by root params. `expose_anchors` if the child itself has grandchild anchors.
6. `assemble_output([all comp_*])` → an object_merge in the work container that merges
   each component's `OUT_geo` (cross-subnet safe — `merge`/setInput CANNOT cross subnet
   boundaries; object_merge path refs can). This is the final output.
7. `work_status()` / `anchor_graph()` to verify wiring (no cycles, no unconsumed anchors you intended to consume). `cook_node` + `geometry_stats` after each step.
8. `export_geometry` + `save_hip`. To produce a variant: change a root `p_*` parm (via `set_parms` on the work container) and re-export.

## Example: a parametric table
- Root params: `p_width`, `p_depth`, `p_top_thick`, `p_leg_thick`, `p_leg_len`, `p_leg_count`.
- `comp_tabletop`: a box with `sizex=ch(p_width)`, `sizey=ch(p_depth)`, `sizez=ch(p_top_thick)`.
- Anchors: generate `p_leg_count` points at the tabletop corners, each `@P` = (±width/2, -top_thick/2, ±depth/2) via a wrangle using `ch("p_width")`/`ch("p_depth")`; tag `s@anchor_type="leg"`. `expose_anchors`.
- `comp_legs`: `wire_anchor(tabletop, legs)`; a leg box (`sizex=sizey=ch(p_leg_thick)`, `sizez=ch(p_leg_len)`); `copy_to_points` (pack) onto the anchors.
- merge OUT_geo of both → output.
- Result: change `p_width` → tabletop widens AND the 4 legs slide to the new corners (anchors recomputed). Change `p_leg_thick` → only legs thicken. This is the parametric stability you want.

## Gotchas
- `set_expression` computes relative paths — always pass `root_parm=`, never write `ch("../..")` yourself.
- Anchors are POINTS; if a component has no children, it doesn't need OUT_anchors.
- `anchor_graph` flags unconsumed OUT_anchors — intentional for leaves, a wiring miss for intermediates.
- cook after every parm/expression change; `work_status` shows current param values + edges so you don't lose track.
