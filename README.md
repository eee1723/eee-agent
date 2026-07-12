# EEE Procedural Modeling Agent

A **deepagents**-based AI agent that does procedural modeling in **SideFX Houdini
21** — v1 focus: parametric **buildings**. It plans, builds with native SOP nodes
+ VEX, reads geometry back, validates, and self-corrects.

## Architecture (three processes, deps isolated)

```
Houdini 21 process
 ├─ rpyc RPC server  (127.0.0.1:18811, houdini_side/start_rpc.py)
 └─ PySide6 chat panel (thin client)        [Phase 3, coming]
        ▲ stdio JSON-lines
        │
Agent process (this package, .venv, Python 3.11)
 └─ deepagents + 13 Houdini tools ─ rpyc ─▶ Houdini
```

- The agent's heavy deps (langchain/deepagents/rpyc) live in `.venv`, **never in
  Houdini's Python**.
- Houdini side uses only its built-in PySide6 — zero extra install in Houdini.

## Layout

```
eee_agent/
  config.py            env: RPC host/port, LLM provider/model
  model.py             multi-provider factory (default DeepSeek V4 Pro)
  system_prompt.py     enforces plan→build→cook→stats→validate→export
  app.py               create_deep_agent(...) assembly
  cli.py               selftest | prompt | stdio
  bridge/              rpyc client + plain-Python serialization (no proxies leak)
  tools/               13 @tool functions (scene/nodes/vex/inspect)
skills/                parametric-building, vex-patterns (SKILL.md)
memory/AGENTS.md       project conventions
houdini_side/          start_rpc.py (+ panel, Phase 3)
eval/                  geometry assertions + regression (Phase 4)
```

## Setup

Already done in this repo (recorded for reference):

```powershell
# venv built from Houdini's bundled Python 3.11 (guaranteed version match)
& "C:\Program Files\Side Effects Software\Houdini 21.0.440\python311\python.exe" -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .
copy .env.example .env   # then fill in DEEPSEEK_API_KEY
```

## Run

1. **Start the bridge in Houdini** — see `houdini_side/README_INSTALL.md`.
2. **Verify the bridge** (no LLM needed):
   ```powershell
   .\.venv\Scripts\python.exe -m eee_agent.cli selftest
   ```
3. **Run the agent** (needs `DEEPSEEK_API_KEY` + bridge running):
   ```powershell
   .\.venv\Scripts\python.exe -m eee_agent.cli prompt "Build a 3-storey house with windows and a pitched roof, then export to output/house.obj"
   ```

## Status

- ✅ Phase 0 — hrpyc/hou API verified by reading source + hython introspection
- ✅ Phase 1 — bridge + 13 tools + CLI; static build verified
- ⏳ Phase 0/1 — **live** bridge smoke test pending (run `selftest`)
- ⏳ Phase 0 — DeepSeek V4 Pro tool-calling check pending (needs API key)
- ⏳ Phase 2 — live building generation pending (after bridge confirmed)
- ⏳ Phase 3 — PySide6 panel
- ⏳ Phase 4 — eval framework + regression suite
- ⏳ Phase 5 — building depth (doors/materials/variation)

## Key verified facts (no guessing)

- Houdini 21.0.440 at `C:\Program Files\Side Effects Software\Houdini 21.0.440`,
  hython at `bin\hython.exe`, Python 3.11, PySide6.
- `hrpyc.start_server(port, use_thread, quiet)` has **no host kwarg** and binds
  `0.0.0.0`; we bind `127.0.0.1` ourselves.
- Export via `node.geometry().saveToFile(path)`; wrangle parms: `snippet`/`class`
  (detail|primitive|point|vertex|number)/`group`; vector parms via `parmTuple`.
- DeepSeek model id `deepseek-v4-pro` (legacy `deepseek-chat` deprecated
  2026/07/24). `create_deep_agent(model, tools, *, system_prompt, skills, memory)`
  signature confirmed.
