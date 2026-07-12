# Project Conventions (always loaded into the agent)

## Scene hygiene
- Build under `/obj/building_AGENT` (a `geo` container). Call `scene_reset("/obj")`
  before a fresh build; do not accumulate stale nodes.
- Name nodes by purpose: `footprint`, `floor_plate`, `walls`, `windows`, `roof`,
  `out`. End the chain in a node named `out` and export THAT node.

## Verification is mandatory
- After any structural change: `cook_node` then `geometry_stats` then
  `validate_geometry`. Never stack new work on unvalidated geometry.
- A box has 8 points / 6 prims; a unit grid (2x2 div) has 9 points / 4 prims. Use
  these as sanity checks.

## Tool-bridge facts (do not fight these)
- Tools return plain dicts, never node objects. Compare nodes by path string.
- Multi-channel parms take lists: `{"size": [w, h, d]}`.
- rpyc has no auth and the server is bound to localhost — if you see a connection
  error, the user must restart `houdini_side/start_rpc.py`.

## Modeling preferences
- Native SOP nodes first, VEX for detail, Python only as glue (the tools enforce
  this — there is no per-point Python tool).
- Expose tunables as parms (floors, floor_height, width, window_density) so the
  user can re-shape without rebuilding.

## Export
- Final deliverable: `export_geometry("/obj/building_AGENT/out", "<path>.obj")`
  and `save_hip("<path>.hip")`. Confirm both succeed before finishing.

## Component / parametric conventions (Phase C)
- When the model must expose adjustable params, work inside a work-container subnet
  (`ensure_work_container`), not loose nodes under /obj.
- Root params = spare parms `p_<name>` on the work container (single source of truth).
  Drive dims with `set_expression(node, parm, root_parm=...)` — never hand-write
  `ch("../..")` relative paths (the tool computes them).
- Components are `comp_<name>` subnets with `OUT_geo` + `OUT_anchors`. Anchors are
  point clouds with `s@anchor_type`. Wire dependencies with `wire_anchor` (DAG;
  cycles are refused). Repeat units via `copy_to_points` (pack=on), not copy-stamp.
- After structure changes: `work_status` + `anchor_graph` + `cook_node` to verify
  wiring and propagation. Change a `p_*` parm and re-cook to confirm it propagates
  before exporting.
