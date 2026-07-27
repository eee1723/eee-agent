---
name: parametric-building
description: Concrete recipe for a building in Houdini via the sandbox workflow (massing, walls with window openings, repeated elements, roof, commit through the verify gates). Load for any building/house/tower task.
---

# Parametric Building Recipe (sandbox → verify → commit)

A reliable building built one small step at a time in the scratch sandbox:
a box massing + facade walls with window openings (boolean subtract) +
repeated elements (copytopoints2) + a roof, merged into one output and
committed through the four hard gates. Every step is a `scratch_build` call
whose returned `geometry`/`errors` you check before moving on.

## The tool contract (read first)
- `scratch_build(operations=[...])` writes ONLY to the isolated sandbox
  (`/obj/eee_scratch_<run>`). Ops: `create_node` / `set_parm` / `connect`.
- `set_parm` values are literals or bounded C1 typed `expr` ASTs: numbers,
  booleans, strings, or a list of <=16 numbers. Use safe relative `ch()`
  references for derived dimensions; no VEX or file paths.
- One small step per call (a node or two + their parms). Read the returned
  `geometry` (point/prim/vertex counts + bbox) and `errors` before the next.
- When the cooked result matches the design, `scratch_commit(
  target_parent_path="/obj", target_name="building")` runs four hard gates
  (bake / structure / orientation / health). Refused = read `reason` +
  `gates`, fix the sandbox, re-commit. Do not retry blindly.

## Step 1 — massing box (reference volume)
```
ops = [
  {"kind": "create_node", "node_name": "mass", "node_type": "box"},
  {"kind": "set_parm", "node_name": "mass", "parm": "sizex", "value": 6.0},
  {"kind": "set_parm", "node_name": "mass", "parm": "sizey", "value": 9.0},
  {"kind": "set_parm", "node_name": "mass", "parm": "sizez", "value": 4.0},
  {"kind": "set_parm", "node_name": "mass", "parm": "ty", "value": 4.5},
]
```
Checkpoint: `geometry` shows 8 points / 6 prims, bbox y in [0, 9].
(box is centered on its `tx/ty/tz`; `ty = sizey/2` puts it on the ground.)

## Step 2 — one wall with window openings (repeat per side)
For each of front/back/left/right:
1. `grid` ("front_face"): `sizex`=wall width, `sizey`=wall height,
   `rows`/`cols` pick the panel density. The catalog grid lies flat (XZ);
   it has no orientation parm, so orient it with an `xform` (next step).
2. `xform` ("front_orient"), input 0 ← the grid: `rx`=90 to stand the grid
   up, then `tx/ty/tz` to place it on the box face. Checkpoint via bbox:
   a vertical wall has a thin bbox in exactly one horizontal axis.
   If the wall lies flat or faces the wrong way, flip the sign of `rx`.
3. `polyextrude2` ("front_slab"), input 0 ← the xform: `dist`=wall
   thickness (e.g. 0.2). Checkpoint: prim count rises (sheet → solid slab).
4. Window cutters: one `box` per opening sized to the hole (`sizex/sizey`
   = hole, `sizez` = thicker than the wall), positioned with `tx/ty/tz`.
   Combine cutters with a `merge` (input 0..N ← each cutter box).
5. `boolean2` ("front_wall"): input 0 ← `front_slab`, input 1 ← the cutter
   merge; `booleanop`=2 (subtract). Union is `booleanop`=0 (the default).
   Checkpoint: errors empty; prim count changes; bbox unchanged overall.

## Step 3 — repeated elements (window frames, columns, balconies)
- Point source: a `grid` with `rows*cols` = copy count, or a `line` with
  `points`=N, `dirx/diry/dirz`, `dist` for a 1-D row.
- `copytopoints2`: input 0 = the prototype node, input 1 = the point
  source. Set `pack`=1 for packed copies (cheaper, and counts as modular
  structure for the structure gate). `transform`=1 (default) applies point
  transforms.
- There is no per-copy attribute stamping in the catalog: copies are
  uniform. For two or three variants, build one proto per variant and one
  copytopoints2 per variant onto its own point set.

## Step 4 — roof
- Flat: a `box` sized to the footprint, `ty` just above the top floor.
- Slab: a `grid` footprint + `polyextrude2` (`dist` for thickness,
  `inset`>0 to taper the edge).

## Step 5 — finish: one merged output, then commit
1. `merge` ("assembled"): input 0..N ← every wall/roof/element chain end.
2. `null` ("OUT"): input 0 ← `assembled`. Make this the LAST node you
   create — the commit gates evaluate the container's display node (the
   last created node when no display flag is set), so end with your output.
3. Final observe: `errors` empty, prim count > 0, bbox plausible. Read
   `geometry_stats` / `query_scene` on the sandbox `output_node` path if
   you need more evidence.
4. `scratch_commit(target_parent_path="/obj", target_name="building")`.
   - committed=true → report `final_path` and the `receipt` fields. Done.
   - refused=true → read `reason` and `gates`, fix the named defect in the
     sandbox with more `scratch_build` calls, then re-commit.

## orientation_checks and skip_structure_check
- `orientation_checks` entries look like
  `{"component_id": "tower", "kind": "elongated", "expected_axis": "Y",
  "tolerance_deg": 15, "signed": false}` (`kind`: radial|elongated|planar;
  `expected_axis`: X|Y|Z|-X|-Y|-Z; optional `construction_axis` override).
  They are checked against prims carrying a `component_id` attribute.
  The current catalog has no node that writes prim attributes, so a
  catalog-built building has none and the bake/orientation gates pass
  vacuously — omit `orientation_checks` for a plain catalog build.
- `skip_structure_check=True` is ONLY for genuinely simple single-piece
  assets (one box, one slab). A multi-part building is exactly what the
  structure gate wants to see decomposed with merge/copytopoints2/boolean2
  nodes — do not skip it.

## Gotchas
- Literal values only. `ch(...)` text or an expression string in `value`
  does not compute anything — send the number.
- One step per `scratch_build` call (<=64 ops, but stay small). Iterate.
- Boolean needs watertight solids on both inputs; cut through the whole
  wall thickness or the hole is partial. Check `errors` after the boolean.
- copytopoints2 input order: input 0 = proto, input 1 = target points.
  Swapping them copies your point cloud onto the proto — prim count tells
  on you immediately.
- After a refused commit the sandbox is preserved: fix it, don't rebuild it.
- Before guessing a parm name, confirm it with `search_houdini_knowledge`
  (the catalog exposes only the verified parms listed per node type).
