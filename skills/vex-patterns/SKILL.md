---
name: vex-patterns
description: Reusable VEX snippets for procedural modeling in Houdini wrangles. Load when you need per-point/prim/detail math.
---

# VEX Patterns (snippets for set_vex)

## Set a detail attribute once (run_over = "detail")
```c
f@floor_height = 3.0;
i@floors = 5;
```

## Per-point floor index from Y position (run_over = "point")
```c
i@floor = floor(@P.y / f@floor_height);
```

## Random per-prim value (run_over = "primitive")
```c
int seed = chi("seed");              // add a 'seed' parm on the wrangle
f@rnd = rand(@primnum * 1.3 + seed);
```

## Place window bays on a grid facade (run_over = "primitive")
```c
// expects u@uv or i@row/i@col set earlier
i@row = floor(@P.y / f@floor_height);
i@col = @primnum % chi("cols");
i@is_window = (i@row % 2 == 1) && (i@col % 2 == 1);
```

## Normalize / range map
```c
f@t = fit(@P.x, ch("min"), ch("max"), 0, 1);
```

## Scatter offsets for copy-to-points variation (run_over = "point")
```c
f@pscale = fit01(rand(@ptnum), 0.5, 1.5);
@P.y += rand(@ptnum + 11) * ch("jitter");
```

## Window / facade marking (verified to compile in Houdini 21, run_over="primitive")
Use these directly instead of guessing VEX. After marking, Blast with group
`@is_window==1`, grouptype=prims to cut the holes.

```c
// checkerboard: every other prim is a window
i@is_window = (@primnum % 2);
```

```c
// band by prim centroid Y (the centroid method, written correctly):
int pts[] = primpoints(0, @primnum);
vector c = {0,0,0};
foreach (int pt; pts) c += point(0, "P", pt);
c /= len(pts);
i@is_window = (c.y > 1.0 && c.y < 2.0) ? 1 : 0;
```

Note: `getbbox(0, @primnum, ...)` and `primvertexcount` have different signatures
than you might expect — if a call fails, set_vex returns the matching-function
candidates in vex_errors; use them.

## Tips
- `chi("name")`/`ch("name")` auto-create parms on the wrangle referenced by name.
- Use `@` prefixes correctly: `f@` float, `i@` int, `v@` vector, `s@`/`@` string.
- Test snippets on a small grid first; cook + geometry_stats to confirm.
