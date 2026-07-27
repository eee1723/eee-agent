---
name: procedural-modeling
description: End-to-end procedural modeling workflow for 程序化建模 / 参数化资产 / procedural asset requests — requirement analysis, bounded modeling-brief compilation, Three.js HTML sketch, render_sketch image review gate with the user, translation to Houdini via scratch_build (see html-to-houdini skill), verify_geometry assertions before scratch_commit, and parameter decomposition. Consult whenever the user asks to build a parametric model or procedural asset.
---

# Procedural Modeling Pipeline

Seven stages, each with hard entry/exit gates. **Do not skip stages.** The
design stance: LLM judgment stays in the model (guided here), deterministic
mechanics live in bounded tools.

```
需求 → [1 分析] → [2 建模简报] → [3 草图 HTML] → [4 渲染审核门] → [5 翻译构建] → [6 机器验证] → [7 参数拆解]
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

## Stage 2 — Modeling brief compilation

Before writing HTML, call `prepare_modeling_brief`. This is a contract
compiler, not prose polishing. The ready brief must fix:

- units plus signed up/forward axes (the tool derives the left axis);
- detail level (`blockout` / `standard` / `high`);
- semantic component inventory and repeated-part counts;
- positive real dimensions and explicit assumptions;
- orientation rules, structural constraints, review views, and executable
  acceptance statements.

Default decisively from asset-family conventions. Questions are allowed only
when the answer would materially change **component scope** or **detail
level**. Put every question into `clarification_questions` in one call:
**zero to three questions total, one batch only**. If the tool returns
`ready=false`, show exactly those questions and stop. After the user's answer,
fill all remaining gaps with stated defaults and call the tool again with no
questions; do not start a second clarification round.

Exit: `prepare_modeling_brief` returns `ready=true` plus a `brief_digest`.

## Stage 3 — Three.js sketch (quality gate)

Write a single self-contained HTML file. Requirements (quality here caps the
whole pipeline):

- **Brief-bound**: reuse the ready brief's coordinate frame, components,
  dimensions, detail level, constraints, views, and acceptance statements;
  do not reinterpret the original request independently
- **静态**: no animation, no interaction logic, no per-frame state — a pure
  declaration evaluable in one pass
- **高细节**: sensible proportions, material grouping, real dimensions
- **结构准确**:
  - Semantic part names (`tube_down`, `leg_fl` — never `mesh3`)
  - All dimension constants collected at the top of the file
  - Prefer mappable helpers — `tube(p1, p2, r)` for every rod — over raw
    `BufferGeometry` soup
- **方向稳定**: construct rods from endpoints, mirror paired parts, avoid
  guessed Euler rotations, and render every review view named by the brief
- **Self-auditing**: encode the brief's orientation and structural acceptance
  statements as deterministic JavaScript assertions before render
- Three.js via CDN importmap (needs network at render time)

Exit: the checklist above passes on a re-read of your own file.

## Stage 4 — Render & user review gate (mandatory stop)

1. Call `render_sketch(html_content, sketch_name, brief_digest)` with the exact
   ready-brief digest → headless Chrome PNG under `output/sketches/`.
2. Look at the image yourself first (if the tool returned it / it is
   viewable): fix obvious defects (lighting, wrong constant) by editing the
   HTML and re-rendering — max 3 self-rounds.
3. **Then stop and ask the user to review.** Show the image path. Do not
   proceed to Stage 5 until the user approves. Map their feedback to
   concrete HTML edits and re-render.

Failure handling: if `render_sketch` reports Chrome missing / timeout /
blank CDN render (offline), tell the user plainly and offer the choice:
review the HTML source directly, or skip visual review (record the skip).

## Stage 5 — Translation & build

Follow the `html-to-houdini` skill (search it via
`search_houdini_knowledge`) — its mapping table, pivot checklist, and
anti-patterns are binding. Key points:

- You are the **transpiler**: re-author the approved brief and sketch intent as
  `scratch_build` op sequences (catalog node types plus C1 typed `expr`
  parms). Use one SOP subnet per component and `declare_parm` for bounded
  public design-intent parameters.
- Group ops by component; keep component boundaries visible in node naming and
  record the Parameter Manifest (classification/tab/range/dependencies).
- Small steps; read the returned geometry (counts + bbox) and errors after
  each batch
- Confirm unknown parms with `search_houdini_knowledge` — never guess

Exit: the sandbox network cooks clean, bbox matches sketch intent.

## Stage 6 — Machine verification (before any commit)

Call `verify_geometry(node_path, expect)` on the scratch output with
expectations derived from the approved brief and sketch (verts/faces ranges, bbox envelope —
must rest on ground, size within sanity bounds). If `ok` is false, fix and
re-verify. **Only after `ok: true` may you `scratch_commit`.**

## Stage 7 — Parameter decomposition record

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

- Skipping the Stage 4 user stop ("the render looked fine to me")
- Writing HTML before `prepare_modeling_brief` returns `ready=true`
- Asking more than three clarification questions or splitting them across rounds
- Reinterpreting components/detail/axes after the brief digest is fixed
- Building in Houdini before the sketch is approved
- Committing without `verify_geometry` returning ok
- Asking the user proportion questions you could default sanely
- Translating messy sketch structure instead of fixing the sketch first
