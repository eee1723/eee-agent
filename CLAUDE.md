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
- **Parametric architecture (Phase C)**: work subnet + spare parms (`p_<name>`) as
  single source of truth + component subnets (`comp_*` with `OUT_geo`/`OUT_anchors`)
  + anchor point clouds + `object_merge` cross-subnet assembly + `ch("../p_..")`
  expressions. See [[procedural-components]] below.

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
5. **Cross-subnet assembly = object_merge**: `merge`/`setInput` CANNOT cross subnet
   boundaries (`setInput(0, subnet/internal)` raises OperationFailed). Use
   `procedural.assemble_output` (object_merge by path). Connecting to the subnet node
   itself (`setInput(0, subnet)`) IS allowed.
6. **Subnet spare parms**: add via `addSpareParmTuple(hou.FloatParmTemplate(name,label,n,(defaults)))`.
   Getter is `node.spareParms()` — `spareParmTuples()` does NOT exist. ch() ref: scalar
   (size-1) parm `p_width` has NO suffix; multi-component uses `p_widthx/y/z`.

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
- ContextSeek memory: `EEE_CONTEXTSEEK=true`. Workflow status middleware: default on
  (`EEE_WORKFLOW_STATUS`).
- Cross-machine setup: see `SETUP.md`.

## Known limitation
DeepSeek V4 Pro loops on long-horizon tasks (saw 120-step limit, over-iteration).
Architecture is proven (parametric table: change width → legs move). The remaining
lever is the MODEL — Claude is the recommended swap for reliability (one-line via
`EEE_LLM_PROVIDER=anthropic`).

## Layout
`eee_agent/` (config, model, app, cli, bridge/, tools/ — 25 tools, context_store,
workflow_middleware, tracing, system_prompt) · `skills/` (parametric-building,
vex-patterns, sop-cookbook, procedural-components) · `memory/AGENTS.md` (agent
conventions, in-repo) · `houdini_side/` (start_rpc, chat_panel, launch, install_menu)
· `eval/` · `MainMenuCommon.xml`.
