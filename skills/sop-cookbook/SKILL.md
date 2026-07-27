---
name: sop-cookbook
description: Quick reference of node recipes for the sandbox modeling surface — each recipe is a scratch_build op sequence using only catalog node types and verified parms. Consult before building; confirm unknown parms with search_houdini_knowledge.
---

# SOP Cookbook (sandbox catalog recipes)

Every recipe below is a `scratch_build` op sequence. Only these catalog
node types exist — using anything else fails the call:
`geo, box, grid, line, xform, polyextrude2, boolean2, copytopoints2,
sweep2, merge, null, normal, fuse2, subdivide, resample`.

`set_parm` values are literals or bounded typed `expr` ASTs (number / bool /
string / list of <=16 numbers). Use a component `<component>_ctrl` box for
design-intent slots and relative channel refs for derived dimensions. One
small step per call; check the returned `geometry`
(counts + bbox) and `errors` after each. When a parm isn't listed here,
confirm it with `search_houdini_knowledge` before setting it.

## Sized box (props, slabs, massing)
```
create_node box "b"; set_parm sizex/sizey/sizez; set_parm tx/ty/tz
```
- Box is centered on `tx/ty/tz`; spans ±size/2 per axis. `ty=sizey/2`
  rests it on the ground. `rx/ry/rz` rotate it.
- Checkpoint: 8 points / 6 prims.

## Flat sheet or point lattice (facades, copy targets)
```
create_node grid "g"; set_parm sizex, sizey, rows, cols
```
- The catalog grid lies flat (XZ plane); it has no orientation parm —
  stand it up with an `xform` (`rx=90`, flip the sign if the bbox says
  the wall faces the wrong way).
- Points = rows*cols; prims = (rows-1)*(cols-1). With rows=2, cols=2 you
  get exactly 4 corner points — a copy target set for legs/posts.

## Move / rotate / scale
```
create_node xform "xf"; connect "xf" input 0 ← upstream; set_parm tx..tz / rx..rz / sx..sz
```
- Rotations are degrees. Verify orientation through the returned bbox,
  not by assumption.

## Sheet → solid slab (walls, tops, shelves)
```
create_node polyextrude2 "ex"; connect input 0 ← sheet; set_parm dist (thickness), inset (edge taper), divs
```
- Checkpoint: prim count rises. `dist=0` is a no-op — always set it.

## Row of evenly spaced points (railings, columns)
```
create_node line "pts"; set_parm originx/originy/originz, dirx/diry/dirz, dist, points=N
```
- N points along `dir` over length `dist`. Then feed as copy targets.

## Copy a proto onto points (windows, legs, repeated details)
```
create_node copytopoints2 "cp"
connect "cp" input 0 ← proto node;  connect "cp" input 1 ← point source
set_parm pack=1
```
- Input order matters: 0 = proto, 1 = target points. Swapped inputs copy
  your points onto the proto — the prim count exposes it instantly.
- `transform`=1 (default) applies point transforms; `pivot`=1 (default)
  uses the proto's pivot. Copies are uniform — no per-copy attribute
  variation in the catalog. For variants, use one proto + one copy node
  per variant.

## Boolean openings (window/door holes) and unions
```
cutter boxes → merge "cutters"
create_node boolean2 "bool"
connect "bool" input 0 ← base solid;  connect "bool" input 1 ← cutters
set_parm booleanop=2     # 0 = union (default), 2 = subtract
```
- Both inputs must be watertight solids; make cutters thicker than the
  wall so they cut clean through. Check `errors` and prim count after.
- Boolean is expensive — merge all cutters first and do ONE boolean.

## Tube along a path (pipes, rails, molding)
```
path:  line "path" (points=8, dist=L) → resample "rs" (dosegs=1, segs=8)
profile: line "prof" (dirx=1, diry=0, dist=r)
create_node sweep2 "sw"
connect "sw" input 0 ← "rs";  connect "sw" input 1 ← "prof"
```
- sweep2 input 0 = backbone (the path), input 1 = profile (cross-section).
  Key parm: `scale` (profile scale), `roll`, `surfacetype`.

## Cleanup and refinement
- `fuse2`: weld points — `tol3d` (default 0.001), `deldegen`=1 removes
  degenerates. Use after booleans/unions that leave coincident points.
- `subdivide`: `iterations` (1–2 is plenty), `algorithm`.
- `resample`: `length` (target segment length) or `dosegs=1`+`segs`.
- `normal`: `cuspangle` (default 60) for shading normals.
- `null`: stable chain-end bookmark; cheap, no geometry change.

## Finish every asset the same way
```
create_node merge "assembled"; connect inputs 0..N ← every chain end
create_node null "OUT"; connect "OUT" input 0 ← "assembled"   # LAST node created
```
- The commit gates evaluate the container's display node (the last
  created node when no display flag is set) — always end with your OUT.
- Then verify (errors empty, prims > 0, sane bbox) and `scratch_commit`.

## Golden rules
1. Catalog node types and their listed parms only; confirm the rest with
   `search_houdini_knowledge` / `get_houdini_knowledge`.
2. Literal values only — compute derived numbers yourself.
3. One small step per `scratch_build`; observe `geometry`/`errors` after
   each; fix the broken node, never rebuild the whole graph.
