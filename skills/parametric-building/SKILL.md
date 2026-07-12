---
name: parametric-building
description: Concrete, copy-paste recipe for a procedural building in Houdini (massing, 4 walls with windows, roof, merge, export). Load for any building/house/tower task.
---

# Parametric Building Recipe (concrete)

A reliable, minimum-fuss building: a box massing + 4 facade walls with window
holes (grid → xform → mark wrangle → blast → polyextrude) + a roof, then merge
and export. Each step has a checkpoint stat so you know it's right before moving on.

## Step 0 — container
- scene_reset("/obj"); create geo container `/obj/building_AGENT`.
- All nodes below go INSIDE `/obj/building_AGENT`.

## Step 1 — massing box (reference volume)
- create_node("box", "mass"), set_parms {"size": [W, H, D]} (e.g. [6, 9, 4]).
- Checkpoint: geometry_stats → 8 points / 6 prims.

## Step 2 — one wall facade pattern (repeat per side)
For each of front/back/left/right, build the same chain:
1. `grid` (e.g. "front_grid"): set_parms {"rows": 6, "cols": 4, "size": [W, H]}.
   Checkpoint: points = rows*cols in a planar grid.
2. `xform` ("front_wall"): connect from the grid; set_parms {"r": [...]} to rotate
   it into wall orientation (front/back: rotate X 90°; left/right: rotate Y 90°
   then X 90°), and {"t": [...]} to position it on the box face.
3. `attribwrangle` ("mark_front", run_over="primitive") connected from the xform:
   mark window prims, e.g. `i@window = (i@curprim % 2 == 1);` then a Blast keeps
   only non-window OR you blast the window prims.
4. `blast` ("front_holes") from the mark wrangle: set group to `@window==1`,
     grouptype "prims", negate off → removes window prims. Checkpoint: prim count drops.
5. `polyextrude::2.0` ("front_3d") from the blast: gives the wall thickness.
   CALL describe_node_type("polyextrude::2.0") and set the distance parm (do NOT
   guess). Checkpoint: point count rises (extrusion adds verts).

## Step 3 — roof
- Flat roof: a `grid` or `box` sized to the footprint, positioned at y=H+thin.
- Pitched: a `polyextrude::2.0` of the footprint inward+up, or node type `roof`
  (describe_node_type("roof") for its slope parm).

## Step 4 — FINISH (do not skip — this is where you complete)
1. merge_nodes([".../front_3d",".../back_3d",".../left_3d",".../right_3d",".../roof"],
   parent_path="/obj/building_AGENT", name="out").
2. geometry_stats on "out" → must be non-empty, finite bbox.
3. validate_geometry on "out" (min_points > 8).
4. export_geometry("/obj/building_AGENT/out", "output/house.obj").
5. save_hip("output/house.hip").
6. PRINT final summary and STOP.

## Expose as parms (optional, on a null or the geo)
floors, floor_height, width, depth, window_density, roof_type — so the user can
re-shape without rebuilding.

## Gotchas
- describe_node_type BEFORE setting parms on polyextrude/boolean/roof.
- Boolean needs watertight solids or it silently breaks — prefer the grid+blast
  hole method above (more robust than boolean for windows).
- copytopoints input order: input 0 = proto, input 1 = target points.
- Always cook + geometry_stats after each step; fix only the broken node.
