# Task 18-G Catalog Expansion and Golden Cases Plan

Status: existing core SOP batch plus verified `normal`, `subdivide`, versioned
`polyextrude::2.0`, `fuse::2.0`, `line`, `resample`, `sweep::2.0`, and
`copytopoints::2.0`, and `boolean::2.0` entries are implemented. Eight deterministic
Golden Cases (box chain, grid chain, merged sources, subdivided surface,
extruded/fused grid, copied boxes, swept lines, boolean union) replay
successfully in Houdini 21.0.440 hython. Richer asset-level batches remain.

## Catalog batches

1. Bootstrap/object: `geo` and verified SOP root behavior.
2. Core SOP: primitive/box/grid/xform/null/merge.
3. Surface: polyextrude/fuse/normal/subdivide.
4. Assembly: line/curve/resample/sweep/copytopoints.
5. Boolean: boolean with bounded input and cook cases.

Each entry records exact category, internal type name, safe parameter names,
scalar/tuple shape, defaults, input/output bounds, and Houdini build evidence.
Unsupported/file/source/expression fields remain absent.

## Golden Cases

- parameterized box prop;
- table/chair-like repeated assembly without VEX;
- sweep profile along path;
- boolean cut with valid and invalid intersection;
- deterministic compile replay;
- stale scene and occupied-name refusal;
- forced cook error and rollback;
- parameter sensitivity with scene restoration.

Every batch is independently committed after offline and hython gates.
