# EEE Procedural Modeling Agent

A **deepagents**-based AI agent that does procedural / parametric modeling in
**SideFX Houdini 21**. It plans, builds with native SOP nodes + VEX, reads
geometry back, validates, self-corrects, and exports — exposing tunables as
parms so a model can be reshaped without rebuilding.

> **Read first each session:** `CLAUDE.md` (full context + gotchas) and the
> current cross-computer handoff,
> `docs/handoffs/2026-07-16-runtime-d1-transfer.md`. This README is the
> orientation map; older handoffs retain milestone history.

## Architecture (three processes, deps isolated)

```
Houdini 21 process
 ├─ rpyc RPC server  (127.0.0.1:18811, houdini_side/start_rpc.py)
 └─ PySide6 chat panel (dark, tool cards + todos + metrics + send/stop)
        ▲ stdio JSON-lines
        │
Agent process (this package, .venv, Python 3.11 — uv-managed)
 └─ deepagents + 25 Houdini tools ─ rpyc ─▶ Houdini
        └─ provider registry (DeepSeek V4 via official Anthropic endpoint)
        └─ reliability middleware: read-back trim · loop guard · tool-error trace · compact tool
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
  tools/               25 @tool functions
    scene.py  nodes.py  vex.py  compose.py  inspect.py  procedural.py (Phase C)
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

## Runtime (additive, loopback, read-only v1)

A second, **persistent and authenticated** Runtime (`eee_agent/runtime/`) runs as
its own process. It is **additive** — the existing `python -m eee_agent.cli …`
commands above remain available as the rollback path. It does **not** replace the
Secure HoudiniBridge (deferred).

```powershell
# Start the Runtime (binds 127.0.0.1 exclusively; ephemeral port by default).
uv run --extra eval python -m eee_agent.runtime serve
uv run --extra eval python -m eee_agent.runtime serve --help   # options
```

- **Loopback only** — `--host` must be `127.0.0.1` (rejected before any socket is
  created); `--port 0` = ephemeral; `--graceful-timeout` default 10s. Bearer-token
  handshake auth (HTTP 401 on failure); wire protocol `eee.runtime/1`.
- **Data home** — `%LOCALAPPDATA%\EEEAgent\` by default. Override for local
  testing/coverage with `EEE_RUNTIME_HOME=<absolute dir>`. Under `<home>/state/`:
  `app.sqlite` (sessions/runs/events), `checkpoints.sqlite` (LangGraph),
  `runtime.lock`, `runtime.json` (discovery: host/port/pid/nonce + a token
  **fingerprint** only), `runtime.token` (the full bearer token — its only home).
- **Read-only Houdini boundary (v1)** — the Runtime agent uses an exact read-only
  tool allowlist (`hou_status`, `find_nodes`, `describe_node_type`,
  `geometry_stats`, `validate_geometry`, `work_status`, `anchor_graph`); no
  write/save/export and no implicit general-purpose subagent. Conversation
  continuity uses `thread_id = session_id`.
- **Offline tests** — the full Runtime suite (incl. a real-subprocess restart E2E)
  runs with **no live LLM and no Houdini**. GLM-5.2 and Houdini read-only smokes
  are **manual only** and never block offline acceptance.
- **Never commit** runtime state — `app.sqlite*`, `checkpoints.sqlite*`,
  `runtime.token`, `runtime.json`, `runtime.lock`, logs, `.env`, `.venv` are all
  `.gitignore`d. Spec: `docs/superpowers/specs/2026-07-14-runtime-design.md`.

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
| **Live end-to-end agent run on current machine** | ⏳ pending — bridge must be started in Houdini, then `selftest` + a `prompt` |
| Runtime + typed Houdini ChangeSets | ✅ Tasks 1–13, 15-A/B/C/D, and 16-A/B1/B2a/C/D/D1 Codex-accepted on `feature/runtime`; D1 supports transactional create-under-created-parent and create-then-set/connect. B2b/16-E/UI remain separate planned slices; branch is not merged. See `docs/handoffs/2026-07-16-runtime-d1-transfer.md` |
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
