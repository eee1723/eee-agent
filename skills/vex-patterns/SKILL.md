---
name: vex-patterns
description: VEX-pattern equivalents in the sandbox catalog. There is no VEX/wrangle surface in the current tool set — this file maps each classic VEX pattern to its node-based equivalent, or states plainly when the pattern is not expressible and what to do instead.
---

# VEX Patterns → sandbox catalog equivalents

There is NO VEX surface in the current tool set: no wrangle node type in
the catalog, no code-snippet parm, and `set_parm` accepts literal values
only (no expressions, no `ch()` references, no VEX text). Snippets cannot
be executed anywhere. This file maps each classic pattern to what you do
instead inside the `scratch_build` → observe → `scratch_commit` workflow.

The general principle: **you are the expression engine.** All math happens
in your reasoning; only literal numbers cross the wire.

## Per-prim marking (`i@is_window = @primnum % 2`)
NOT expressible — the catalog has no attribute-writing node.
- Alternative: select by construction, not by attribute. Build exactly the
  geometry you want: place window frames only on the points where windows
  belong (a `grid` with the right rows/cols as the copy-target set), and
  cut openings only where you built cutters (`boolean2`, subtract).
- If you would have marked "every other panel", build two point sets and
  two `copytopoints2` chains — one per alternating role.

## Attribute-driven delete (blast on `@group==1`)
NOT expressible — there is no delete-by-group node in the catalog at all.
- Alternative: never create the geometry you would have deleted. Design
  the point sources and solids so the "kept" set is what gets built.
- For holes in a solid, subtract with `boolean2` instead of deleting prims
  off a sheet.

## bbox iteration / `getbbox()` math
NOT expressible as geometry-processing code.
- Alternative: read the bbox as data and do the math yourself. Every
  `scratch_build` returns `geometry.bbox_min/bbox_max`; `geometry_stats`
  reads any sandbox path. Compute centers, spans, and offsets from those
  numbers, then set literal parms (e.g. `tx = (bbox_min.x+bbox_max.x)/2`).

## Scattering points + per-point random offsets (`rand(@ptnum)`, `@pscale`)
NOT expressible — no scatter node, no random, no point attributes.
- Alternative: deterministic point sources. `grid` gives a rows×cols
  lattice; `line` gives N points along a direction (`points`, `dist`,
  `dirx/diry/dirz`). Jitter/variation must be authored by you: compute the
  offsets yourself and position pieces with `xform` literals.
- Per-copy scale/orientation variation via point attributes is also out
  (catalog copytopoints2 exposes only `pack`/`pivot`/`transform`). Use one
  proto + one `copytopoints2` per variant instead.

## Range mapping / `fit()` / `fit01()`
NOT expressible on the wire.
- Alternative: compute the fitted value yourself and set the literal
  result. This is the most common "VEX" need in practice (e.g. inset =
  thickness/2, grid size = width − leg_thick) and it is just arithmetic in
  your plan.

## Detail attributes as global constants (`f@floor_height = 3.0`)
NOT expressible — no detail attributes.
- Alternative: keep the constants in your todo list / plan as the root
  parameter table, and derive every dependent literal from it. When a root
  value changes, re-issue `set_parm` with the recomputed literals for every
  affected node — nothing propagates automatically.

## Parm expressions (`ch("../p_width")`)
NOT expressible — expression strings are rejected by design; `value` is a
literal JSON scalar (or a small list of numbers).
- Alternative: same as above — compute it, set it, and re-set dependents
  when it changes. The sandbox makes recomputation cheap.

## What IS available instead of VEX (the whole palette)
- Generators: `box`, `grid`, `line` — with literal size/orientation parms.
- Shaping: `xform`, `polyextrude2`, `boolean2`, `subdivide`, `resample`,
  `fuse2`, `normal`.
- Repetition/assembly: `copytopoints2` (proto onto points), `sweep2`
  (profile along backbone), `merge`, `null`.
- Read-back evidence: the `geometry` summary from `scratch_build`, plus
  `geometry_stats` / `query_scene` on sandbox paths.

## Practical rules
- Never emit code as a parm value. If a value needs computing, compute it
  first, then send the number.
- Selection problems become construction problems: build the kept set,
  don't mark-and-delete the rejected set.
- Variation problems become enumeration problems: a few explicit variants
  (a few protos, a few point sets) instead of a random field.
- Verify with counts and bbox after every step — that read-back loop is
  the replacement for "run the snippet and print the attribute".
