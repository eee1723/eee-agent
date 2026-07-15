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
  in a **uv-managed** venv (`.venv`, Python 3.11, `uv.lock`). Model: DeepSeek V4 Pro
  default, multi-provider (`EEE_LLM_PROVIDER`).
- **Provider layer (Foundation)**: model construction flows through `ProviderRegistry`
  (`eee_agent.providers`). DeepSeek V4 uses `ChatAnthropic` against the official
  Anthropic-compatible endpoint `https://api.deepseek.com/anthropic` — NOT the old
  `ChatOpenAI(base_url=...)` path (that path dropped `reasoning_content`). Exact model
  names only (`deepseek-v4-pro`/`deepseek-v4-flash`); deprecated aliases
  (`deepseek-chat`/`deepseek-reasoner`) are rejected. Standard Anthropic/OpenAI
  adapters construct their native LangChain classes. `eee_agent.model` imports no
  concrete provider class.
- **Deep Agents harness (Foundation)**: `configure_deepagents_harness()` (in
  `eee_agent/harness.py`, called before `create_deep_agent`) registers a
  `HarnessProfile(general_purpose_subagent=enabled=False)` for both the "anthropic"
  key (DeepSeek is built on `ChatAnthropic`, so Deep Agents resolves its provider as
  anthropic) and "openai". The implicit `task` tool / `general-purpose` subagent is
  disabled until a restricted general capability is designed. Contract test:
  `tests/test_harness.py`.
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
   4.1.0** (check `<Houdini>\python311\lib\site-packages\rpyc\__init__.py`). `uv.lock`
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
7. **Foundation venv is lean — no Phoenix/ContextSeek deps**: `uv.lock` intentionally
   omits `openinference` (Phoenix) and `seekdb`/`pyseekdb` (ContextSeek) — those are
   later milestones. The `tracing.py` / `context_store.py` code is still present, but
   `EEE_TRACING=phoenix` or `EEE_CONTEXTSEEK=true` will raise `ModuleNotFoundError` on
   Foundation. Keep both OFF (`.env.example` keeps them commented). `build_agent()` is
   otherwise fully functional; all Foundation tests self-contain this via monkeypatch.

## Houdini install (machine-specific — both machines are actively used; confirm which via `scripts/env_probe.sh` before developing)
- Machine A: `C:\Program Files\Side Effects Software\Houdini 21.0.440`
  (hython `bin\hython.exe`, bundled Python `python311\` = 3.11.7, UI PySide6, `HFS`
  env not set).
- Machine B: `D:\houdini` (same Houdini 21.0.440 build). The user switches between
  these two machines frequently — at the start of each session, check which Houdini
  path is present (env_probe prints it) before any Houdini/RPC work. On either machine
  the `.venv` is rebuilt from the local Houdini's `python311\python.exe` for a
  guaranteed version/rpyc match — do not copy `.venv` between machines.
- rpyc in Houdini's bundle = **4.1.0** (matches the `uv.lock` pin) — verified at
  `<Houdini>\python311\lib\site-packages\rpyc\version.py`.
- hrpyc.py at `<Houdini>\houdini\python3.11libs\hrpyc.py` (NOT `python311\libs`).

## How to run
- Bridge: in Houdini run `houdini_side/start_rpc.py` (or use the **EEE Agent** menu —
  installed via `houdini_side/install_menu.py`).
- Deps: `uv sync --extra eval --python 3.11` (rebuilds `.venv` from `uv.lock`;
  `uv lock --check` verifies the lock is in sync).
- CLI: `uv run --extra eval python -m eee_agent.cli {selftest|prompt|stdio|versions}`.
  `versions` prints the locked runtime + dependency versions as JSON.
- Tracing (Phoenix) / semantic memory (ContextSeek): **off on Foundation** — deps not
  in `uv.lock` (gotcha #7). They return in a later milestone.
- Reliability middleware (all default ON; see `eee_agent/app.py`): read-back trimming
  (`EEE_TRIM_READBACKS`), deterministic loop guard (`EEE_LOOP_GUARD`,
  `EEE_LOOP_REPEAT=3`, `EEE_LOOP_HARD=5`), tool-error span tracing, on-demand
  `compact_conversation` tool (`EEE_COMPACT_TOOL`). The workflow-status system-prompt
  injection is OFF by default (`EEE_WORKFLOW_STATUS=true` to opt back in) — appending
  to the system prompt every turn broke prompt caching.
- Cross-machine setup: see `SETUP.md`.

## Known limitation
DeepSeek V4 Pro loops on long-horizon tasks (over-iteration). Architecture is proven
(parametric table: change width → legs move). Mitigations now in place: recursion
limit default 999 (`EEE_RECURSION_LIMIT`), deterministic loop guard, read-back
trimming, on-demand compaction — but the strongest single lever remains the MODEL:
Claude is the recommended swap for reliability (one-line via
`EEE_LLM_PROVIDER=anthropic`).

## Houdini documentation knowledge graph (read-only, offline)
An independent `feature/houdini-knowledge-graph` milestone adds an **offline,
read-only** knowledge graph over the docs that ship in the local Houdini install
(SOP nodes, VEX functions, `hou.*` classes/functions/methods + the 4 project
skills). Built once into a single versioned **SQLite + FTS5** cache; no Neo4j,
no embeddings, no vector DB, no new runtime deps.

- **Build it explicitly** (does NOT auto-build on first query; does NOT need the
  RPC bridge or GUI, only `<HFS>/bin/hython.exe`):
  ```bash
  uv run python -m eee_agent.knowledge.build --hfs 'D:\houdini' --out <path>.sqlite3
  uv run python -m eee_agent.knowledge.build --selftest   # synthetic corpus, no HFS
  ```
- **Default cache location:**
  `%LOCALAPPDATA%\EEEAgent\cache\knowledge\houdini\21.0.440\knowledge.sqlite3`
  (shared, rebuildable, machine-local — **never committed, never inside the
  package**). Override via env: `EEE_KB_ENABLED` (strict bool, default `true`,
  only gates the query tools), `EEE_KB_PATH` (absolute, or relative to repo
  root — never Houdini's cwd), `EEE_HFS` (source HFS for build/stale-check).
  A repo-local `.knowledge-cache/` override is gitignored.
- **Two read-only Agent tools** (`eee_agent/tools/knowledge.py`, registered after
  the 25 Houdini tools → 27 project tools, no implicit `task`):
  - `search_houdini_knowledge(symbol|query, kinds/context/tag/superclass,
    predicate/direction, include_historical, limit)` — exact/alias symbol
    resolution + filtered FTS + one-hop relations; never returns full text.
  - `get_houdini_knowledge(entity_id, section, max_chars)` — bounded body/section
    read (default 4 KB, hard cap 8 KB); `entity_id` is a cache PK, never a path.
  Both construct `KnowledgeService` lazily, convert DTOs via `to_dict()`, and
  **never raise** (expected/unexpected failures → stable `{ok:false, code,
  error}`). `get_houdini_knowledge` is deliberately NOT in `READBACK_TOOLS`
  (different entities' bodies aren't interchangeable).
- **Authority boundary (critical):** the cache documents **what the official
  Houdini docs record** — it does NOT prove a node is creatable here or reveal
  its real parms. Before creating a node the agent MUST still call
  `describe_node_type(type)`; **live introspection is the final authority**, and
  if it disagrees with the cache the cache is reported as possibly stale.
- **Stale / rebuild:** the query path is read-only (`mode=ro`, `query_only=ON`);
  a fingerprint stale-checker compares the three current HFS source archives to
  the stored manifest and returns `KB_STALE` on mismatch (never invents
  staleness — schema/integrity stay owned by the service). To refresh, just
  rebuild; the build atomically `os.replace`-swaps the cache and never breaks the
  previous one on failure.
- **Tests are HFS-independent by default.** The real-corpus contract
  (`tests/knowledge/test_hfs_contract.py`, marker `houdini_kb`) skips unless
  `EEE_RUN_HOUDINI_KB_TESTS=true`; run it with `EEE_HFS` set. The golden
  evaluator (`eval/knowledge/run_eval.py`, 46 cases) runs against any built
  cache: `uv run --extra eval python eval/knowledge/run_eval.py --kb <path>`.
- **No Runtime integration on this branch.** Wiring (`read_only_tools`
  allowlist, RuntimePaths shared cache, startup KB status, Run snapshot
  manifest, restricted Research Capability, combined contract tests) is a later,
  separately-reviewed merge — see
  `docs/handoffs/2026-07-14-houdini-knowledge-graph.md`.

## Layout
`eee_agent/` — **core/** (Foundation: ids, errors, artifacts, events, versioning) ·
**providers/** (Foundation: contracts, registry, secrets, deepseek_v4, anthropic,
openai, factory, events, normalize) · **harness.py** (Foundation: disable implicit
general-purpose subagent) · config, model, app, cli · `bridge/` (rpyc client +
plain-Python serialization, no proxies leak) · `tools/` (27 @tool functions:
scene/nodes/vex/compose/inspect/procedural + the two read-only `knowledge` KB
tools) · **knowledge/** (offline docs graph: parse_*/graph/store/service/build +
SQLite/FTS5 cache — see section above) · context_store, context_trim, loop_guard,
tool_error_trace, workflow_middleware, tracing, system_prompt · `skills/`
(parametric-building, vex-patterns, sop-cookbook, procedural-components) ·
`memory/AGENTS.md` (agent conventions, in-repo) · `houdini_side/` (start_rpc,
chat_panel, launch, install_menu, start_phoenix) · `eval/` · `MainMenuCommon.xml`.
Foundation status: see `docs/handoffs/2026-07-13-foundation-migration.md`.
