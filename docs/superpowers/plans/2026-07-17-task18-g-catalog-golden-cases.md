# Task 18-G Catalog Expansion and Golden Cases Plan

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

