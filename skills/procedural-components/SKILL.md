---
name: procedural-components
description: How to build multi-part parametric assets in the sandbox workflow — structuring parts as separate component chains, computing parameter relationships yourself into literal parm values, and what the four commit gates (bake/structure/orientation/health) actually check. Use for configurable assets like furniture, buildings with knobs, any "parameter-driven" request.
---

# Procedural Components (multi-part, parameter-driven assets)

There are no expressions in the current surface: `set_parm` accepts literal
values only (no `ch()`, no VEX, no cross-node references). A "parametric"
asset therefore means: **you are the expression engine.** Keep the root
parameters in your plan, compute every derived number yourself, and send
literal results. Structure the asset as one node chain per component so each
part stays independent, merge the chains at the end, and commit through the
gates.

## Component structure in the sandbox
- A component = a named node chain inside the sandbox: generator
  (box/grid/line) → shaping nodes (xform/polyextrude2/boolean2/…) → chain
  end. Name nodes consistently: `<part>_<role>` (`top_box`, `leg_proto`,
  `leg_points`, `legs_copy`).
- Assembly = a `merge` wired to every chain end, then a final `null`
  ("OUT") as the LAST node created (the commit gates evaluate the
  container's display node — the last created node when no display flag
  is set).
- Repetition = `copytopoints2` (input 0 = proto, input 1 = point source)
  with `pack`=1. Point sources from the catalog: a `grid` (rows×cols
  lattice of points) or a `line` (`points`=N along `dir`, spacing
  `dist`/(N-1)).

## Parameter relationships without expressions
1. Write the root parameters into your todo/plan first: e.g.
   `width=1.2, depth=0.7, top_thick=0.04, leg_thick=0.05, leg_len=0.72`.
2. Derive every dependent value yourself before the `scratch_build` call:
   leg inset = leg_thick/2; leg grid size = width − leg_thick; leg center
   height = leg_len/2; tabletop center height = leg_len + top_thick/2.
3. Send literals. To "change a parameter" later, re-issue `set_parm` with
   the recomputed literals for EVERY affected node — nothing propagates
   automatically. The sandbox makes this cheap: rebuilds are free.

## Example: a parametric table (4 legs at the corners)
- `top_box` (box): `sizex=width`, `sizey=top_thick`, `sizez=depth`,
  `ty=leg_len + top_thick/2`.
- `leg_proto` (box): `sizex=leg_thick`, `sizey=leg_len`, `sizez=leg_thick`,
  `ty=-leg_len/2` (top of the leg sits at the copy point).
- `leg_points` (grid): `sizex=width-leg_thick`, `sizey=depth-leg_thick`,
  `rows=2`, `cols=2` → exactly 4 corner points; then an `xform`
  (`ty=leg_len`) to lift the points to the tabletop underside.
- `legs_copy` (copytopoints2): input 0 ← `leg_proto`, input 1 ← the xform;
  `pack`=1.
- `assembled` (merge) ← top_box, legs_copy → `OUT` (null).
- Variant: width 1.2 → 1.6? Recompute and re-set two literals:
  `top_box.sizex=1.6` and `leg_points.sizex=1.6-leg_thick`. Both
  `scratch_build` calls are small and observable.

## What the four commit gates actually check
`scratch_commit` runs these on your sandbox output; any hard failure
refuses the commit and preserves the sandbox:
- **bake** — every prim with a `component_id` attribute must carry a
  non-zero `edini_world_axis` (its construction axis). Skipped when no
  prim has `component_id`.
- **structure** — refuses monolithic assets: ≥3 components all coming from
  a single Python SOP with no modular assembly nodes. Structured-op builds
  are inherently modular (copytopoints2/sweep2/boolean2/polyextrude2 all
  count as modular), so this gate is about how you build, not what.
  `skip_structure_check=True` is only for genuinely simple single-piece
  assets.
- **orientation** — for each check you pass
  (`{component_id, kind ("radial"|"elongated"|"planar"), expected_axis
  ("X"|"Y"|"Z"|"-X"|"-Y"|"-Z"), tolerance_deg (default 15), signed,
  construction_axis (optional override)}`), compares the component's
  baked/declared axis against the expected one. Only meaningful for prims
  carrying `component_id`.
- **health** — orphan_points and open_curves are hard failures;
  degenerate / nonmanifold / open_boundary / coincident are advisory.

## The component_id limitation (important, current surface)
The catalog has NO node that writes prim attributes, so sandbox geometry
built from catalog nodes carries no `component_id` prims. Consequences:
- The bake and orientation gates pass vacuously on catalog builds. That is
  expected — not a loophole to exploit, just the current surface.
- Do NOT pass `orientation_checks` for a plain catalog build: with no
  `component_id` prims they are skipped, and checks referencing ids that
  don't exist as prims would fail hard if any `component_id` attribute
  were present.
- Keep "components" as a **structural** discipline instead: separate named
  chains per part, one merge, one OUT null. That is what keeps the
  structure gate happy and your builds repairable.

## Reading back your work
- `scratch_build` returns `applied_ops`, `output_node`, `errors`, and
  `geometry` (counts + bbox) — your primary evidence after each step.
- `geometry_stats(node_path)` / `query_scene(node_paths=[...])` read any
  sandbox path (they are absolute `/obj/eee_scratch_<run>/...` paths) when
  you need deeper inspection.
- `search_houdini_knowledge(query)` before using a node/parm you have not
  verified — never guess parm names.

## Gotchas
- No expressions anywhere: recompute and re-set every dependent literal
  when a root parameter changes. Forgetting one is the classic bug (a
  wider tabletop with legs still at the old corners).
- One small step per `scratch_build` call; verify counts/bbox after each.
- A refused commit is a diagnosis, not an error: read `reason` + `gates`,
  fix the named defect in the preserved sandbox, re-commit.
- Committed sandboxes no longer exist (renamed into the scene) — start a
  fresh `scratch_build` iteration for the next asset.
