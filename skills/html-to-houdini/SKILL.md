---
name: html-to-houdini
description: Translation reference for converting a Three.js/HTML sketch into Houdini geometry via scratch_build ops — the verified mapping table, translation workflow, pivot/coords checklist, resolution policy, and anti-patterns. Consult during the translation stage of a modeling task; the overall pipeline (sketch quality gate, user review, verification) lives in the procedural-modeling skill.
---

# HTML → Houdini Translation Reference

You are the **transpiler**: read the sketch's *intent* and re-author it in
catalog ops. Never write a JS parser. Reference case:
`docs/handoffs/threejs-to-houdini-bicycle-case.md`.

## Translation workflow (mandatory order)

1. Read the sketch; list its parts, constants, and repeated patterns.
2. For every catalog node type you intend to use, confirm parms against
   `skills/sop-cookbook` first; if a parm isn't listed there, confirm it
   with `search_houdini_knowledge`. **Never guess a parm name.**
3. Build with `scratch_build` op sequences — small steps, and after each
   step read the returned `geometry` (counts + bbox) and `errors`.
4. Check the result bbox against the sketch's intent before moving on.

### Mapping table

| Sketch construct | Catalog recipe | Status |
|---|---|---|
| `tube(p1,p2,r)` rod | `line` (origin/dir/dist) → `sweep2` tube mode (`surfaceshape=1`, `radius`, `cols`=截面分段, `endcaptype=1` 封口) | ✅ in catalog |
| Repeated radial copies (spokes) | one `line` → `copyxform` (`ncy`, `rz=360/N`, `px/py/pz`=旋转轴心,**必须显式给 pivot**) → single `sweep2` | ✅ in catalog |
| `TorusGeometry` | `torus` (`radx`/`rady`, `rows`/`cols`; XY 平面用 `orient=2`) | ✅ in catalog |
| `SphereGeometry` (scaled) | `sphere` (`radx/y/z`, `rows`/`cols`) + `xform` scale | ✅ in catalog |
| `BoxGeometry` | `box` (sizex/y/z, tx/ty/tz) | ✅ in catalog |
| Flat ground | `grid` | ✅ in catalog |
| Sheet → solid (ExtrudeGeometry) | `grid`/profile → `polyextrude2` (dist) | ✅ in catalog |
| CSG union/subtract | merge cutters → ONE `boolean2` (booleanop) | ✅ in catalog |
| Instancing on points | proto → `copytopoints2` (pack=1), inputs 0=proto 1=points | ✅ in catalog |
| Smooth shading | trailing `normal` node (cuspangle=60 default) | ✅ in catalog |
| `LatheGeometry` | revolve — unmapped, confirm with search_houdini_knowledge | ❓ untested |
| Noise displacement | mountain/noise — unmapped | ❓ untested |

Menu parms are set as integer indices (catalog is numeric-literal only):
`surfaceshape`: input=0, tube=1, square=2, ribbon=3 · torus `orient`: x=0,
y=1, z=2 · `endcaptype`: none=0, single=1.

### Resolution / subdivision policy

- Get smoothness from the **generator's own resolution**: sweep `cols`,
  torus `rows`/`cols`, sphere `rows`/`cols`. Do NOT bolt on `subdivide` as a
  fix — it fights parameterization and bloats geometry.
- Scale resolution to part size: frame tubes 24 sides, forks/stays 16,
  spokes 6–8, chains 8.
- Finish with one `normal` node (cusp 60°) for smooth shading; 90° box
  edges stay hard automatically.

### Pivot & coordinate checklist

- Both Three.js and Houdini are Y-up; keep sketch coordinates verbatim.
- Any rotation/scale about an object's own center needs an **explicit
  pivot** — defaults sit at the world origin. (Bicycle: copyxform pivot =
  wheel center, xform scale pivot = saddle center.)
- Torus lies in XZ by default; `orient=2` puts it in the XY plane
  (bicycle wheels).
- When rewiring an existing network: create the new node, replace the
  merge input **at the same input index**, then delete the old node —
  downstream references never dangle.

### Anti-patterns

- Guessing parm names or node types (the #1 translation failure).
- One giant build with no intermediate geometry/bbox checks.
- Baking a value that is derivable from an exposed parameter.
- Diagnosing from a screenshot without first reading current parm values —
  in a shared session you may be looking at a human's mid-edit state.

## Parameter decomposition note

Catalog parms are literal-only; channel references are a future compiler
feature. While translating, still **record** the parameterization intent
(design intent / derived / constant per dimension, plus dependency
patterns) — the procedural-modeling skill's Stage 6 delivers this record.
