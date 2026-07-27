---
name: procedural-modeling
description: End-to-end procedural modeling workflow for 程序化建模 / 参数化资产 / procedural asset requests — requirement analysis, Three.js HTML sketch, render_sketch image review gate with the user, translation to Houdini via scratch_build (see html-to-houdini skill), verify_geometry assertions before scratch_commit, and parameter decomposition. Consult whenever the user asks to build a parametric model or procedural asset.
---

# Procedural Modeling Pipeline

Six stages, each with hard entry/exit gates. **Do not skip stages.** The
design stance: LLM judgment stays in the model (guided here), deterministic
mechanics live in bounded tools.

```
需求 → [1 分析] → [2 草图 HTML] → [3 渲染审核门] → [4 翻译构建] → [5 机器验证] → [6 参数拆解]
```

## Stage 1 — Requirement analysis

Extract before writing any code:
- Object type and its natural **components** (自行车 → wheels/frame/cockpit/drivetrain)
- Key dimensions in meters, and which of them the user is likely to want as
  **parameters** later (candidate design intents)
- Repeated patterns (spokes, legs, slats) — these become copy recipes

Exit: you can state the component list and the candidate parameter list in
one breath. If the request is ambiguous on proportions, pick sane real-world
defaults and say so — do not block on questions.

## Stage 2 — Three.js sketch (quality gate)

Write a single self-contained HTML file. Requirements (quality here caps the
whole pipeline):

- **静态**: no animation, no interaction logic, no per-frame state — a pure
  declaration evaluable in one pass
- **高细节**: sensible proportions, material grouping, real dimensions
- **结构准确**:
  - Semantic part names (`tube_down`, `leg_fl` — never `mesh3`)
  - All dimension constants collected at the top of the file
  - Prefer mappable helpers — `tube(p1, p2, r)` for every rod — over raw
    `BufferGeometry` soup
- Three.js via CDN importmap (needs network at render time)

Exit: the checklist above passes on a re-read of your own file.

## Stage 3 — Render & user review gate (mandatory stop)

1. Call `render_sketch(html_content, sketch_name)` → headless Chrome PNG
   under `output/sketches/`.
2. Look at the image yourself first (if the tool returned it / it is
   viewable): fix obvious defects (lighting, wrong constant) by editing the
   HTML and re-rendering — max 3 self-rounds.
3. **Then stop and ask the user to review.** Show the image path. Do not
   proceed to Stage 4 until the user approves. Map their feedback to
   concrete HTML edits and re-render.

Failure handling: if `render_sketch` reports Chrome missing / timeout /
blank CDN render (offline), tell the user plainly and offer the choice:
review the HTML source directly, or skip visual review (record the skip).

## Stage 4 — Translation & build

Follow the `html-to-houdini` skill (search it via
`search_houdini_knowledge`) — its mapping table, pivot checklist, and
anti-patterns are binding. Key points:

- You are the **transpiler**: re-author the sketch's intent as
  `scratch_build` op sequences (catalog node types plus C1 typed `expr`
  parms). Use one SOP subnet per component and `declare_parm` for bounded
  public design-intent parameters.
- Group ops by component; keep component boundaries visible in node naming and
  record the Parameter Manifest (classification/tab/range/dependencies).
- Small steps; read the returned geometry (counts + bbox) and errors after
  each batch
- Confirm unknown parms with `search_houdini_knowledge` — never guess

Exit: the sandbox network cooks clean, bbox matches sketch intent.

## Stage 5 — Machine verification (before any commit)

Call `verify_geometry(node_path, expect)` on the scratch output with
expectations derived from the sketch (verts/faces ranges, bbox envelope —
must rest on ground, size within sanity bounds). If `ok` is false, fix and
re-verify. **Only after `ok: true` may you `scratch_commit`.**

## Stage 6 — Parameter decomposition record

`set_parm` supports typed expressions (`expr` instead of a literal `value`), so
**derived** dimensions can be built as live channel references, not just
recorded for later. An expression is a small JSON AST of: `num` literals,
`ref` parm references (relative like `../wheel_width` or absolute, always
resolving inside the sandbox), arithmetic (`add`/`sub`/`mul`/`div`/`neg`), and
whitelisted functions (`sin cos tan asin acos atan sqrt abs min max floor
ceil pow clamp`). Example — `box2.sizex` tracks `box1`:

```json
{"kind": "set_parm", "node_name": "box2", "parm": "sizex",
 "expr": {"kind": "op", "name": "mul", "args": [
   {"kind": "ref", "path": "../box1/sizex"},
   {"kind": "num", "value": 2.0}]}}
```

Still deliver the decomposition as a record:

- Every dimension classified: **design intent** (expose as parameter) /
  **derived** (expression over parameters — build it with `expr`) /
  **constant** (bake)
- Dependency patterns noted per component (tracker / proportional / offset)
- Components listed with their candidate parameter tabs

Present this record to the user as the close-out summary.

## Anti-patterns

- Skipping the Stage 3 user stop ("the render looked fine to me")
- Building in Houdini before the sketch is approved
- Committing without `verify_geometry` returning ok
- Asking the user proportion questions you could default sanely
- Translating messy sketch structure instead of fixing the sketch first
