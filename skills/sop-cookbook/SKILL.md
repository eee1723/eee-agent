---
name: sop-cookbook
description: Quick reference for common Houdini SOPs used in procedural modeling — what they do, key parms, gotchas. Consult before guessing parms (and call describe_node_type to confirm).
---

# SOP Cookbook (quick reference)

When a node isn't listed here or you need exact parms, call
`describe_node_type("typename")` — it returns the live parm list.

## Primitives
- **box**: parms `sizex/sizey/sizez` or parmTuple `size` [w,h,d]; center `t`.
- **grid**: `rows`, `cols`, `size` [w,h]; planar point grid (great for facades/scatter).
- **circle / tube / sphere**: standard primitives.

## Transform
- **xform** (Transform): `t` translate, `r` rotate (deg), `s` scale, `shear`.
  Use to position/orient walls and parts.

## Composition / instancing
- **merge**: many inputs → one stream. Use merge_nodes().
- **copytopoints**: input 0 = proto to copy, input 1 = target points. Use
  copy_to_points(). Drive per-copy variation with `@pscale`, `@N`, `@up` on points.
- **foreach** (Begin/End): loop over pieces; Metadata node gives `@iteration`/`@numiter`.

## Editing geometry
- **blast**: delete a group (`group`, `grouptype` menu: prims/points/edges,
  `negate` to invert). Robust way to cut window holes.
- **polyextrude::2.0**: ~131 parms — ALWAYS describe_node_type first. Key: the
  extrude distance + inset live in its transform groups.
- **boolean**: 66 parms, input A=0 / B=1, `op` chooses union/subtract/intersect.
  Needs watertight solids. Check stats after.
- **remesh / subdivide / Facet / Normal**: cleanup.

## VEX (via set_vex)
- attribwrangle: `snippet` = code, `class` = point|primitive|vertex|detail,
  `group` = optional group filter. `ch("x")/chi("x")` auto-create parms.

## Scatter / points
- **scatter**: generates random points on surface (input = prims). `density`.
- **add**: can create explicit points or delete geometry.

## Groups
- **groupexpression / group by range**: build groups by rule.

## Output
- **file**: read/write geometry; filemode menu: auto/read/write/none.
- Or export_geometry() which uses hou.Geometry.saveToFile.

## Golden rules
1. describe_node_type before setting parms you don't know.
2. cook + geometry_stats after every change.
3. Fix one node, never nuke the whole graph.
