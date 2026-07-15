# EEE Procedural Modeling Agent

A **deepagents**-based AI agent that does procedural / parametric modeling in
**SideFX Houdini 21**. It plans, builds with native SOP nodes + VEX, reads
geometry back, validates, self-corrects, and exports — exposing tunables as
parms so a model can be reshaped without rebuilding.

> **Read first each session:** `CLAUDE.md` (full context + gotchas) and
> `docs/handoffs/2026-07-13-foundation-migration.md` (Foundation status + what's
> done + open caveats). This README is the orientation map.

## Architecture (three processes, deps isolated)

```
Houdini 21 process
 ├─ rpyc RPC server  (127.0.0.1:18811, houdini_side/start_rpc.py)
 └─ PySide6 chat panel (dark, tool cards + todos + metrics + send/stop)
        ▲ stdio JSON-lines
        │
Agent process (this package, .venv, Python 3.11 — uv-managed)
 └─ deepagents + 27 tools ─ rpyc ─▶ Houdini  (25 Houdini tools + 2 read-only KB tools)
        └─ provider registry (DeepSeek V4 via official Anthropic endpoint)
        └─ reliability middleware: read-back trim · loop guard · tool-error trace · compact tool
        └─ Houdini docs knowledge graph (offline SQLite/FTS5 cache, read-only query tools)
        └─ normalized provider events → legacy stdio JSON-lines
        └─ optional tracing → Phoenix (http://localhost:6006)  [deps not in Foundation lock]
```

- The agent's heavy deps (langchain/deepagents/rpyc) live in `.venv`, **never in
  Houdini's Python**. Houdini side uses only its built-in PySide6 — zero extra
  install in Houdini.
- The bridge returns plain dicts (never rpyc proxies) and binds localhost with no
  auth — see `CLAUDE.md` gotcha #1/#2.

## Layout

```
eee_agent/
  config.py            env: RPC host/port, LLM provider/model, recursion limit, thinking/effort
  model.py             provider-neutral factory -> ProviderRegistry (no concrete provider import)
  system_prompt.py     enforces plan→build→cook→stats→validate→export; on-demand status
  app.py               create_deep_agent(...) + harness config + reliability middleware
  cli.py               selftest | prompt | stdio | versions   (stdio = multi-mode JSON-lines)
  harness.py           disable Deep Agents' implicit general-purpose/task (Foundation)
  bridge/              rpyc client + plain-Python serialization (no proxies leak)
  tools/               27 @tool functions
    scene.py  nodes.py  vex.py  compose.py  inspect.py  procedural.py (Phase C)
    knowledge.py        read-only KB query tools (search_houdini_knowledge / get_houdini_knowledge)
  knowledge/            offline docs knowledge graph: parsers · graph · store · service · build
                       (SQLite/FTS5 cache; read-only query; no RPC/GUI at query time)
  core/                Foundation contracts: ids · errors · artifacts · events · versioning
  providers/           Foundation: contracts · registry · secrets · deepseek_v4 · anthropic
                       · openai · factory · events · normalize
  context_store.py     ContextSeek file-backed memory (EEE_CONTEXTSEEK)  [dep not in Foundation lock]
  context_trim.py      stubs stale read-back results (work_status/geometry_stats/…)
  loop_guard.py        deterministic repetition guard (soft@3 / hard@5)
  tool_error_trace.py  marks Phoenix spans ERROR on {ok:false}
  workflow_middleware.py (off by default — per-turn prompt mutation broke caching)
  tracing.py           Phoenix / OpenInference OTel wiring  [dep not in Foundation lock]
skills/                parametric-building · procedural-components · sop-cookbook · vex-patterns
memory/AGENTS.md       project conventions (loaded into the agent)
houdini_side/          start_rpc · chat_panel · launch · install_menu · start_phoenix · README_INSTALL
eval/                  geometry_assertions.py + run_eval.py + cases/
docs/                  handoffs/ · superpowers/{specs,plans}/ · AGENT_FIX_PLAN.md (legacy)
scripts/env_probe.sh   session-start environment probe (runs via .claude/settings.json hook)
```

## Multi-machine development

This project is developed across **two machines**; the agent `.venv` is
**machine-specific** (it must match the local Houdini's bundled `rpyc`), so it is
`.gitignore`d and rebuilt per machine — see `SETUP.md`. The per-machine
environment inventory is kept in Claude's project memory.

At the **start of every session**, a `SessionStart` hook (`.claude/settings.json`)
runs `scripts/env_probe.sh` and prints a one-shot status: which Houdini path is
present, whether `.venv` / `.env` exist and `rpyc` matches, whether the RPC bridge
is up, and whether `build_agent()` compiles. **Read it and resolve any `[WARN]`
before starting dev.**

## Setup (per machine)

See `SETUP.md` for the full sequence. Short version (Foundation uses `uv`):

```powershell
# 1. venv from Houdini's bundled Python 3.11 (guarantees version/rpyc match).
#    Houdini path is machine-specific — adjust to your build:
#      Machine A: C:\Program Files\Side Effects Software\Houdini 21.0.440
#      Machine B: D:\houdini
& "<Houdini>\python311\python.exe" -m venv .venv
# 2. sync exact locked deps (pins rpyc==4.1.0 for Houdini 21.0.440)
uv sync --extra eval --python 3.11
uv lock --check
# 3. configure
copy .env.example .env   # fill DEEPSEEK_API_KEY  (or switch EEE_LLM_PROVIDER)
```

> ⚠️ `rpyc` in the venv **must equal** Houdini's bundled rpyc or RPC fails with
> `ValueError: invalid message type: 18`. Houdini 21.0.440 ships **rpyc 4.1.0**
> (`uv.lock` pins it). Re-check + re-pin if Houdini is upgraded.

## Run

1. **Start the bridge in Houdini** — EEE Agent menu → Start RPC (install the menu
   via `houdini_side/install_menu.py`; see `houdini_side/README_INSTALL.md`), or
   in Houdini's Python Source Editor: `import start_rpc; start_rpc.start()`.
2. **Verify the bridge** (no LLM needed):
   ```powershell
   uv run --extra eval python -m eee_agent.cli selftest
   ```
3. **Run the agent** (needs `DEEPSEEK_API_KEY` + bridge running):
   ```powershell
   uv run --extra eval python -m eee_agent.cli prompt "Build a parametric table: top + 4 legs, expose width/length/height as p_ parms, then export to output/table.obj"
   ```
4. **Versions** (locked runtime + dependency report):
   ```powershell
   uv run --extra eval python -m eee_agent.cli versions
   ```
5. **Tracing** (optional): `EEE_TRACING=phoenix` → http://localhost:6006 — **not on
   Foundation**; `openinference` is not in `uv.lock` (later milestone). See `CLAUDE.md`
   gotcha #7.

## Houdini documentation knowledge graph (offline, read-only)

The agent can query a **local, offline** knowledge graph built from the docs that
ship with the Houdini install (SOP nodes, VEX functions, `hou.*`
classes/functions/methods + the 4 project skills). It is a single versioned
**SQLite + FTS5** cache — no Neo4j, no embeddings, no vector DB, no new runtime
deps, and the query path never touches the rpyc bridge.

**Build it explicitly** (needs only `<HFS>/bin/hython.exe`, not the bridge or GUI;
does NOT auto-build on first query):

```powershell
# to the default %LOCALAPPDATA% location (EEE_KB_PATH overrides it):
uv run python -m eee_agent.knowledge.build --hfs 'D:\houdini'
# to an explicit path:
uv run python -m eee_agent.knowledge.build --hfs 'D:\houdini' --out <path>.sqlite3
# synthetic corpus, no HFS — used by CI/smoke:
uv run python -m eee_agent.knowledge.build --selftest
```

- **Default cache:** `%LOCALAPPDATA%\EEEAgent\cache\knowledge\houdini\21.0.440\knowledge.sqlite3`
  — machine-local, rebuildable, **never committed** (a repo-local
  `.knowledge-cache/` override is gitignored).
- **Env overrides:** `EEE_KB_ENABLED` (strict bool, default `true`; only gates the
  query tools), `EEE_KB_PATH` (absolute, or relative to the repo root — never
  Houdini's cwd), `EEE_HFS` (source HFS for build/stale-check).
- **Query tools** (read-only, never raise; disabled returns `kb_disabled`):
  - `search_houdini_knowledge` — exact/alias symbol resolution + filtered
    free-text + one-hop relations; returns summaries, never full text.
  - `get_houdini_knowledge` — bounded body/section read (default 4 KB, cap 8 KB).
- **Authority:** the cache documents **what the official Houdini docs record**. It
  does NOT confirm a node is creatable here or its real parms — before creating a
  node the agent still calls `describe_node_type(type)`; **live introspection is
  final**, and a mismatch reports the cache as possibly stale.
- **Stale / rebuild:** a read-only fingerprint check compares the current HFS
  source archives to the manifest and returns `kb_stale` on change; rebuild to
  refresh (atomic swap, never breaks the previous cache on failure).
- **Tests:** default suite is HFS-independent. The real-corpus contract
  (`tests/knowledge/test_hfs_contract.py`) skips unless `EEE_RUN_HOUDINI_KB_TESTS=true`:
  ```powershell
  $env:EEE_RUN_HOUDINI_KB_TESTS='true'; $env:EEE_HFS='D:\houdini'
  uv run --extra eval pytest tests/knowledge/test_hfs_contract.py -m houdini_kb -v
  Remove-Item Env:EEE_RUN_HOUDINI_KB_TESTS
  ```
  Golden retrieval evaluator (against any built cache):
  ```powershell
  uv run --extra eval python eval/knowledge/run_eval.py --kb <path>.sqlite3
  ```
- **No Runtime integration on this branch** — see
  `docs/handoffs/2026-07-14-houdini-knowledge-graph.md` for the merge checklist.

## Status

| Area | State |
|---|---|
| Phase 0 — hrpyc/hou API | ✅ verified (source read + hython introspection) |
| Phase 1 — bridge + 25 tools + CLI | ✅ done |
| Phase C — port-based parametric components | ✅ done (`make_component` geo/anchors ports, `wire_anchor`, `assemble_output`, ranged `p_*` parms) |
| Reliability layers | ✅ read-back trim · loop guard · tool-error trace · compact tool; recursion 999 |
| Runtime UI | ✅ PySide6 panel (dark, tool cards / todos / metrics / send-stop) |
| Observability | ✅ Phoenix one-click launcher + tool-error spans (runtime deps return in a later milestone) |
| **Foundation milestone** | ✅ done — uv-locked deps, core contracts, provider registry (DeepSeek via official Anthropic endpoint), normalized events, explicit harness (no implicit `task`), `cli versions`. 369 tests pass. See `docs/handoffs/2026-07-13-foundation-migration.md` |
| **Houdini docs knowledge graph** | ✅ done (independent branch) — offline SQLite/FTS5 cache + 2 read-only tools; 21.0.440 corpus contract + golden retrieval pass; no Runtime integration yet. See `docs/handoffs/2026-07-14-houdini-knowledge-graph.md` |
| **Live end-to-end agent run on current machine** | ⏳ pending — bridge must be started in Houdini, then `selftest` + a `prompt` |
| Runtime milestone (Session/Run, SQLite, WebSocket) | ⏳ next — plan not yet written (spec §18.2) |
| B2 — per-component subagents | ⏳ deferred (largest change; after model swap) |
| Eval framework | ⏳ scaffold (`eval/`), cases minimal |

## Key verified facts (no guessing)

- Houdini 21.0.440. Install path is **machine-specific** — Machine A:
  `C:\Program Files\Side Effects Software\Houdini 21.0.440`; Machine B: `D:\houdini`
  (hython `bin\hython.exe`, bundled Python `python311\` = 3.11.7, PySide6).
  `scripts/env_probe.sh` detects which is present.
- rpyc **4.1.0** in both Houdini's bundle and the venv (must match); pinned in `uv.lock`.
- Multi-output SOP subnets require internal `output` nodes with explicit
  `outputidx` (a vanilla subnet has one effective output) — see `CLAUDE.md` gotcha #5.
- DeepSeek V4 routes through `ChatAnthropic` on `https://api.deepseek.com/anthropic`
  (Foundation). Exact model ids only: `deepseek-v4-pro` / `deepseek-v4-flash`
  (legacy `deepseek-chat` / `deepseek-reasoner` deprecated 2026/07/24 — rejected by
  the adapter). Provider swap is one line: `EEE_LLM_PROVIDER=anthropic`.
- Deep Agents 0.6.12 auto-adds a `general-purpose` subagent + `task` tool unless a
  harness profile disables it — `eee_agent/harness.py` does so for both anthropic +
  openai keys (verified against the real 0.6.12 API).
- `create_deep_agent(model, tools, *, system_prompt, skills, memory)` signature
  confirmed.
