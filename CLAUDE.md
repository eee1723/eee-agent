# EEE Procedural Modeling Agent — Project Context

A **deepagents**-based AI agent that does procedural/parametric modeling in SideFX
Houdini 21. Read this first every session — it captures the hard-won facts (so we
don't re-discover them). Mirrors the auto-memory; kept in-repo so it travels with git.

## Golden rule
**Verify, don't guess.** The user insists: when unsure about a Houdini/hou/deepagents
fact, verify by inspecting the local Houdini install or the web — never assert from
memory. Houdini version/API mismatches break everything.

## Stack & locked decisions
- **Agent**: `deepagents` (`create_deep_agent(model, tools, system_prompt, middleware=...)`),
  in an independent venv (`.venv`, Python 3.11). Model: DeepSeek V4 Pro default,
  multi-provider (`EEE_LLM_PROVIDER`).
- **Bridge**: agent → rpyc → Houdini. `houdini_side/start_rpc.py` runs an rpyc
  `ThreadedServer` bound to **127.0.0.1:18811** inside Houdini (NOT `hrpyc.start_server`
  — that binds 0.0.0.0 with no auth). Client reimplements `import_remote_module` inline
  (`rpyc.classic.connect` + `connection.modules["hou"]`).
- **Panel**: PySide6 chat panel in Houdini (`houdini_side/chat_panel.py`), spawns the
  agent via QProcess + JSON-lines (`python -m eee_agent.cli stdio`).
- **Modeling layering**: native SOP nodes > VEX > HOM(Python as glue only).
- **Parametric architecture (Phase C, port-based)**: work subnet + spare parms
  (`p_<name>`, with min/max ranges) as single source of truth + component subnets
  (`comp_*`) that expose real OUTPUT PORTS via internal `output` nodes — `geo_port`
  (port 0) and `anchors_port` (port 1). Anchor deps are REAL wires (consumer input ←
  producer `anchors_port`), NOT `object_merge`; final assembly is a `merge` of every
  component's `geo_port`. `ch("../p_..")` expressions. See [[procedural-components]].

## Critical gotchas (each cost a debugging session)

1. **rpyc version pin**: the venv's `rpyc` MUST == Houdini's bundled rpyc, or RPC
   fails with `ValueError: invalid message type: 18`. Houdini 21.0.440 ships **rpyc
   4.1.0** (check `<Houdini>\python311\lib\site-packages\rpyc\__init__.py`). pyproject
   pins `rpyc==4.1.0`. On a different Houdini build, re-check + re-pin.
2. **Never return rpyc proxies to the agent**: bridge layer serializes everything to
   plain dicts; compare nodes by `.path()` (proxies don't support operators).
3. **VEX cook errors must be clean**: `tools/inspect.py::cook_and_diagnose()` reads
   `node.errors()` after a failed cook → returns the actionable VEX error (function +
   line:col + matching-function candidates), NOT the rpyc remote traceback. `set_vex`
   auto-cooks and returns `vex_errors` in the same call.
4. **ContextSeek on Windows = FILE backend**: the seekdb embedded backend needs
   `pylibseekdb` (Linux-only); on Windows it silently falls back to in-memory `memory`
   (loses data per process). `context_store._build_persistent_ctx` forces
   `storage.backend='file'` at `<repo>/.contextseek/store`. The middleware's
   `@traceable` is LangSmith-only → its spans do NOT appear in Phoenix.
5. **Multi-output subnets via `output` nodes (VERIFIED in 21.0.440)**: a vanilla SOP
   subnet has ONE effective output (the render-flag node) — `setInput(idx, subnet,
   out_idx)` always returns output 0, and `setInput(0, subnet/internal)` raises
   OperationFailed. To get distinct routable ports, create `output` SOP nodes INSIDE
   the subnet and set each one's **`outputidx`** parm explicitly (0, 1, …).
   `make_component` does this for `geo_port`(0)/`anchors_port`(1); `wire_anchor`
   connects consumer input ← producer `anchors_port` via `setInput(in, prod, idx)`
   (real wire — replaces object_merge; cycle-checked); the consumer reads anchors via
   `indirectInputs()[in]` (tapped as `in_anchors_<prod>`). `assemble_output` is a
   `merge` of every component's `geo_port`. Connect at the subnet-node level, never
   into a subnet's internals.
6. **Subnet spare parms**: add via `addSpareParmTuple(hou.FloatParmTemplate(name,label,n,(defaults)))`.
   Getter is `node.spareParms()` — `spareParmTuples()` does NOT exist. ch() ref: scalar
   (size-1) parm `p_width` has NO suffix; multi-component uses `p_widthx/y/z`. **Ranges**
   (req #3): `FloatParmTemplate.setMinValue/setMaxValue` (slider range) +
   `setMinIsStrict/setMaxIsStrict` (hard clamp); getters are `minValue()/maxValue()`
   (NOT `min/max`). `add_root_parm(min=, max=, strict=)` applies these.

## Houdini install (this machine)
- `C:\Program Files\Side Effects Software\Houdini 21.0.440`, hython at `bin\hython.exe`,
  bundled Python `python311\` (3.11), UI PySide6, `HFS` env not set.
- hrpyc.py at `<root>\houdini\python3.11libs\hrpyc.py` (NOT `python3.11\libs`).

## How to run
- Bridge: in Houdini run `houdini_side/start_rpc.py` (or use the **EEE Agent** menu —
  installed via `houdini_side/install_menu.py`).
- CLI: `.\.venv\Scripts\python.exe -m eee_agent.cli {selftest|prompt|stdio}`.
- Tracing (Phoenix, no Docker): `EEE_TRACING=phoenix` → OTel/OpenInference →
  `http://localhost:6006`. Pull spans via GraphQL `/graphql` (project=eee-agent).
- ContextSeek memory: `EEE_CONTEXTSEEK=true`. Agent middleware (all default ON; see
  `eee_agent/app.py`): read-back trimming (`EEE_TRIM_READBACKS`), deterministic loop
  guard (`EEE_LOOP_GUARD`, `EEE_LOOP_REPEAT=3`, `EEE_LOOP_HARD=5`), tool-error span
  tracing, on-demand `compact_conversation` tool (`EEE_COMPACT_TOOL`). The workflow-
  status system-prompt injection is now OFF by default (`EEE_WORKFLOW_STATUS=true` to
  opt back in) — appending to the system prompt every turn broke prompt caching.
- Cross-machine setup: see `SETUP.md`.

## Known limitation
DeepSeek V4 Pro loops on long-horizon tasks (over-iteration). Architecture is proven
(parametric table: change width → legs move). Mitigations now in place: recursion
limit default 999 (`EEE_RECURSION_LIMIT`), deterministic loop guard, read-back
trimming, on-demand compaction — but the strongest single lever remains the MODEL:
Claude is the recommended swap for reliability (one-line via
`EEE_LLM_PROVIDER=anthropic`).

## Layout
`eee_agent/` (config, model, app, cli, bridge/, tools/ — 25 tools, context_store,
context_trim, loop_guard, tool_error_trace, workflow_middleware, tracing,
system_prompt) · `skills/` (parametric-building,
vex-patterns, sop-cookbook, procedural-components) · `memory/AGENTS.md` (agent
conventions, in-repo) · `houdini_side/` (start_rpc, chat_panel, launch, install_menu)
· `eval/` · `MainMenuCommon.xml`.
