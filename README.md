# EEE Procedural Modeling Agent

A **deepagents**-based AI agent that does procedural / parametric modeling in
**SideFX Houdini 21**. It plans, builds with native SOP nodes + VEX, reads
geometry back, validates, self-corrects, and exports — exposing tunables as
parms so a model can be reshaped without rebuilding.

[![CI](https://github.com/eee1723/eee-agent/actions/workflows/runtime-ci.yml/badge.svg)](https://github.com/eee1723/eee-agent/actions/workflows/runtime-ci.yml)
![Python 3.11](https://img.shields.io/badge/python-3.11-blue)
![Houdini 21.0.440](https://img.shields.io/badge/Houdini-21.0.440-orange)
![Status](https://img.shields.io/badge/status-MVP%20offline--complete-green)

**Highlights**

- **Conversation-first three-pane panel** (Session Sidebar · Conversation ·
  Inspector) in graphite-industrial tokens (cyan accent, amber gate), with an
  amber approval drawer and automatic
  backend spawn — docked inside Houdini, zero install in Houdini's Python.
- **Iterative sandbox + verify + commit workflow**: the agent builds one node at
  a time in an isolated sandbox container (`scratch_build`), observes the cooked
  result, and promotes verified geometry into the real scene through four hard
  quality gates (`scratch_commit` → bake / structure / orientation / health).
  No result reaches the real scene until the gates pass.
- **Hard quality gates**: deterministic verify gates (component bake, modular
  structure, PCA-based orientation, geometry health) must pass before a sandbox
  is committed; advisory checks never block.
- **Persistent, authenticated Runtime**: WebSocket `eee.runtime/1`, loopback
  only, SQLite sessions/runs/events + LangGraph checkpoints, multi-session,
  reconnect-safe.
- **Offline-first testing**: 3546 tests pass with no live LLM and no Houdini;
  real-provider and real-Houdini checks are explicit opt-in acceptance runs.

> **Read first each session:** `CLAUDE.md` (full context + gotchas) and the
> current handoff,
> `docs/handoffs/2026-07-27-cross-machine-development-handoff.md`. This README is the
> orientation map; older handoffs retain milestone history.

## Architecture (three processes, deps isolated)

```
Houdini 21 process
 ├─ authenticated Secure Bridge (typed requests on the Houdini main thread)
 └─ PySide6 Runtime Control panel
        ▲ authenticated WebSocket
        │
Agent process (this package, .venv, Python 3.11 — uv-managed)
 └─ deepagents + read-only + sandbox tools ─ authenticated Secure Bridge ─▶ Houdini
        └─ scratch_build → /obj/eee_scratch_<run> (isolated sandbox)
        └─ scratch_commit → 4 hard gates → promote to real scene (undo-group wrapped)
        └─ provider registry (DeepSeek V4 via official Anthropic endpoint)
        └─ reliability middleware: read-back trim · loop guard · tool-error trace · compact tool
        └─ normalized provider events → Runtime event stream
        └─ optional tracing → Phoenix (http://localhost:6006)  [deps not in Foundation lock]
```

- The agent's heavy deps (langchain/deepagents) live in `.venv`, **never in
  Houdini's Python**. Houdini side uses only its built-in PySide6 — zero extra
  install in Houdini.
- The Secure Bridge returns bounded plain dicts and requires per-install
  authentication. Legacy raw-write entrypoints are removed.

## Layout

```
eee_agent/
  config.py            env: LLM provider/model, recursion limit, thinking/effort
  model.py             provider-neutral factory -> ProviderRegistry (no concrete provider import)
  system_prompt.py     audited Runtime sandbox+verify+commit boundary
  app.py               create_deep_agent(...) + harness config + reliability middleware
  cli.py               versions (diagnostic report only; no agent execution)
  harness.py           disable Deep Agents' implicit general-purpose/task (Foundation)
  runtime/             persistent authenticated Runtime + read-only/sandbox tools
  core/                Foundation contracts: ids · errors · artifacts · events · versioning
  changesets/          typed ChangeSet policy · services · repositories (commit persistence seam)
  modeling/            scratch coordinator + scratch_build/scratch_commit tools · orientation_math · Brief/Spec compiler (retained)
  houdini_bridge/      authenticated typed Secure Bridge providers (incl. read-only + scratch DTOs)
  knowledge/           read-only Houdini knowledge cache (build · store · service)
  vision/              advisory post-Apply evaluation contracts · router
  panel/               Runtime panel state projection
  providers/           Foundation: contracts · registry · secrets · deepseek_v4 · anthropic
                       · openai · factory · events · normalize
  context_store.py     ContextSeek file-backed memory (EEE_CONTEXTSEEK)  [dep not in Foundation lock]
  context_trim.py      stubs stale read-back results (work_status/geometry_stats/…)
  loop_guard.py        deterministic repetition guard (soft@3 / hard@5)
  tool_error_trace.py  marks Phoenix spans ERROR on {ok:false}
  tracing.py           Phoenix / OpenInference OTel wiring  [dep not in Foundation lock]
skills/                parametric-building · procedural-components · sop-cookbook · vex-patterns
memory/AGENTS.md       project conventions (loaded into the agent)
houdini_side/          secure_bridge_host · runtime_panel/ (three-pane pkg) · changeset_executor (sandbox+verify) · scratch_verify (gates) · workspace_inspector · install_menu · start_phoenix · README_INSTALL
eval/                  geometry_assertions.py + run_eval.py + cases/ + knowledge/ (50-case golden-query KB evaluator)
docs/                  handoffs/ · superpowers/{specs,plans,reviews}/
scripts/env_probe.sh   session-start environment probe (run manually: bash scripts/env_probe.sh)
```

## Multi-machine development

This project is developed across **two machines**; the agent `.venv` is
**machine-specific**, so it is
`.gitignore`d and rebuilt per machine — see `SETUP.md`. The per-machine
environment inventory is kept in Claude's project memory.

At the **start of every session**, run `bash scripts/env_probe.sh` to print a
one-shot status: which Houdini path is present, whether `.venv` / `.env` exist,
whether the Secure Runtime is available, and whether the explicit read-only
`build_agent()` compiles. Set `EEE_PROBE_ENV_FILE` to probe a different env
file (default `.env`). **Read it and resolve any `[WARN]` before starting
dev.**

## Setup (per machine)

See `SETUP.md` for the full sequence. Short version (Foundation uses `uv`):

```powershell
# 1. venv from Houdini's bundled Python 3.11.
#    Houdini path is machine-specific — adjust to your build:
#      Machine A: C:\Program Files\Side Effects Software\Houdini 21.0.440
#      Machine B: D:\houdini
& "<Houdini>\python311\python.exe" -m venv .venv
# 2. sync exact locked deps
uv sync --extra eval --python 3.11
uv lock --check
# 3. configure
copy .env.example .env   # fill DEEPSEEK_API_KEY  (or switch EEE_LLM_PROVIDER)
```

> The authenticated Secure Bridge speaks the typed Runtime protocol and does
> not depend on Houdini's Python packages in the agent venv.

## Run

1. **Start the authenticated Runtime** from a repository terminal:
   ```powershell
   uv run --extra eval python -m eee_agent.runtime serve
   ```
2. **Versions** (locked runtime + dependency report):
   ```powershell
   uv run --extra eval python -m eee_agent.cli versions
   ```
3. **Tracing** (optional): `EEE_TRACING=phoenix` → http://localhost:6006 — **not on
   Foundation**; `openinference` is not in `uv.lock` (later milestone). See `CLAUDE.md`
   gotcha #2.

## Runtime (production, loopback, read-only MVP)

A second, **persistent and authenticated** Runtime (`eee_agent/runtime/`) runs as
its own process. It is the only production agent entrypoint and never falls back
to an unauthenticated or raw-write tool path.

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
- **Read-only + sandbox boundary (v2)** — the Runtime agent uses an exact
  read-only tool allowlist (`scene_status`, `query_scene`, `inspect_workspace`,
  `geometry_stats`, `work_status`, `search_houdini_knowledge`,
  `get_houdini_knowledge`) plus bounded modeling tools:
  `prepare_modeling_brief` (compile axes/components/detail/constraints/acceptance,
  with at most one batch of three component/detail questions), `render_sketch`
  (digest-bound HTML preview), `scratch_build` (build/observe in an isolated
  `/obj/eee_scratch_<run>` container), `verify_geometry`, and `scratch_commit`
  (promote verified geometry through hard gates).
  No raw write/save/export tool and no implicit general-purpose subagent.
  Stop/Cancelled, failed, and completed-but-uncommitted runs preserve their
  run-scoped sandbox for inspection or recovery. A successful commit promotes
  the container; `scratch.destroy` is reserved for explicit disposal.
  Conversation continuity uses `thread_id = session_id`.
- **Docked Runtime control** — the Houdini panel can create/select Sessions,
  start/stop Runs, recover bounded output/activity, and render bounded
  approval/receipt evidence. It exposes no Apply command, raw operation JSON,
  parameter values, SQLite, or agent graph.
- **Trusted Workspace lifecycle** — the public Runtime exposes exactly
  `workspace.create`, `workspace.bind`, `workspace.switch`, and
  `workspace.inspect`. A Workspace identifies trusted scene context; it does
  not grant write permission. Explicit user intent outranks incidental
  selection, and ordinary nodes without all six EEE executor ownership mirrors
  are never silently adopted.
- **Sandbox + verify + commit** — `scratch_commit` runs four hard gates
  (bake / structure / orientation / health) on the sandbox output; on pass it
  renames the sandbox into the real scene inside one `hou.undos.group` (atomic
  rollback on partial failure — stronger than blind rename). On refusal the
  sandbox is preserved for retry. The legacy typed-ChangeSet proposal/Apply/
  recovery internals remain as the commit persistence seam and cross-restart
  recovery path.
- **Offline tests** — the full Runtime suite (incl. a real-subprocess restart E2E)
  runs with **no live LLM and no Houdini**. Real-provider and real-Houdini
  smokes are **explicit opt-in acceptance runs** and never block offline
  acceptance.
- **CI quality gates** — `.github/workflows/runtime-ci.yml` runs on Windows with
  the checked-in lockfile: `uv sync --frozen --extra eval`, the full
  `uv run --frozen --extra eval pytest -q` gate, `uv lock --check`, compileall,
  and `git diff --check`. A separate job runs frozen `ruff`, focused `mypy`
  checks for Runtime DTO/tool boundaries, and `pip-audit` against an exported
  runtime requirements file. The optional Houdini job is enabled only when the
  repository variable `EEE_HFS_RUNNER=true` and a self-hosted
  `houdini-21.0.440` runner with `$env:HFS` are available; it never gates the
  no-Houdini job. When explicitly enabled, its failures are visible in that
  opt-in job rather than being silently ignored.
- **Never commit** runtime state — `app.sqlite*`, `checkpoints.sqlite*`,
  `runtime.token`, `runtime.json`, `runtime.lock`, logs, `.env`, `.venv` are all
  `.gitignore`d. Spec: `docs/superpowers/specs/2026-07-14-runtime-design.md`.

## Status

| Area | State |
|---|---|
| Phase 0 — typed Secure Bridge API | ✅ verified (source read + hython introspection) |
| Phase 1 — legacy Foundation bridge/tools/CLI | historical (removed from production Runtime) |
| Phase C — port-based parametric components | historical — the legacy `eee_agent.tools` implementation was removed; superseded by the strict modeling foundation below (the multi-output port gotcha survives in `CLAUDE.md` #3) |
| Reliability layers | ✅ read-back trim · loop guard · tool-error trace · compact tool; recursion 999 |
| Runtime UI | ✅ PySide6 **three-pane** panel (Session Sidebar · Conversation + composer · Inspector), graphite-industrial tokens (cyan accent, amber gate), auto-starting backend, amber approval drawer, **streaming assistant replies with a collapsible thinking block, kind-based cards (user bubbles vs assistant cards), multi-line Ctrl+Enter composer, auto-create + auto-title Sessions**. See the redesign row below. |
| Observability | ⚠️ Phoenix one-click launcher wired (menu → `start_phoenix.py`), but `arize-phoenix` is **not** in the frozen lockfile — install it as an optional extra. Tool-error span marking is in place; tracing emits only when `EEE_TRACING=phoenix`. |
| **Foundation milestone** | ✅ done — uv-locked deps, core contracts, provider registry (DeepSeek via official Anthropic endpoint), normalized events, explicit harness (no implicit `task`), `cli versions`. See `docs/handoffs/2026-07-13-foundation-migration.md` |
| **Live Runtime acceptance on current machine** | real provider journey **passed 2026-07-20** (DeepSeek + Houdini 21.0.440, strict evidence harness); the RC tag remains (the interactive GUI checklist passed in Stage B acceptance) — see `docs/handoffs/2026-07-20-runtime-development-transfer.md` |
| Runtime + typed Houdini ChangeSets | Complete through local Task 16-E acceptance (merged to `main`): trusted Workspace, ordered created references, transactional Apply, atomic receipts, and no-replay restart recovery. |
| **Sandbox + verify + commit (Pi model)** | ✅ merged to `main`: iterative `scratch_build` in an isolated `/obj/eee_scratch_<run>` container, four hard verify gates (`houdini_side/scratch_verify.py`: bake/structure/orientation/health), and `scratch_commit` promotion wrapped in one undo group. Stop/failure/uncommitted completion preserve the sandbox; `scratch.destroy` is explicit disposal only. Pure-Python orientation math ported to `eee_agent/modeling/orientation_math.py`. Legacy `propose_modeling` retired from the agent graph (module retained). Current full offline gate is **3618 passed, 12 skipped**. |
| **HTML-to-Houdini + node lifecycle wave** | 🚧 branch implementation complete through deterministic/offline coverage: scratch-native Provider evidence, task graph schema/projection, `scratch.v2`, safe two-phase cleanup, and panel task view. Real-Provider three-run acceptance, Waves B/C, panel manual review, and Houdini SOP display/render-flag persistence remain open. See `docs/handoffs/2026-07-27-cross-machine-development-handoff.md`. |
| Docked Runtime panel | Task 17-A and Task 17-B are accepted. The complete Houdini 21.0.440 gate passed Chinese IME/default Session behavior, read-only Run, high-volume reopen, Runtime restart recovery, Stop to Cancelled, empty approvals/no Apply, Scene regression, and zero mutation. See `docs/superpowers/reviews/2026-07-16-task17-b-review-result.md` and `docs/handoffs/2026-07-16-runtime-17b-transfer.md` |
| Strict modeling foundation | Task 18-A through 18-H, all deterministic validators (Spec/Graph/Cook/Geometry/Sensitivity/Semantic), bounded repair tickets, Golden Case catalog batches, and the MODEL/REVIEW product flow are implemented (merged to `main`): strict Brief/Spec contracts, catalog-gated compilation, trusted bootstrap persistence, approval-to-single-flight Apply, durable validation evidence, and verified assembly/surface/boolean replays. Dedicated Houdini 21 hython Golden Case replay passed. Richer asset batches remain iterative. See `docs/superpowers/plans/2026-07-17-task18-h-product-ui.md` and `docs/superpowers/plans/2026-07-17-task18-g-catalog-golden-cases.md` |
| Capture · Vision · Eval | Task 19-A content-addressed Artifact foundation and Task 19-B advisory Vision router are wired into the production post-Apply flow; the Vision real-provider journey passed in Stage B acceptance (B-08, qwen-vl-plus); the delivery/observability slice (19-C) remains open. Deterministic failure precedence holds — vision cannot override a hard validator failure. |
| **Three-pane panel redesign** | ✅ offline-complete (2026-07-21, merged to `main`): the 2134-line single file is now a `houdini_side/runtime_panel/` package — Qt-free `theme`/`view_models`/`backend_launcher` cores with real unit tests, thin Qt shells verified by source-boundary tests, automatic backend spawn, responsive drawers. **Conversation UX (2026-07-21):** streaming assistant replies (`model.text_delta`) + collapsible thinking block (`model.reasoning_delta`), kind-based cards (user accent bubbles vs assistant surface cards), multi-line Ctrl+Enter composer, and auto-create/auto-title Sessions (first run renames the placeholder via a best-effort LLM call). Full offline gate is **~2960 passed, 11 skipped**. The interactive Houdini 21 GUI checklist passed in Stage B acceptance. See `docs/superpowers/plans/2026-07-21-runtime-panel-three-pane.md` |
| B2 — per-component subagents | ⏳ deferred (largest change; after model swap) |
| Eval framework | ⚠️ geometry evaluator + 50-case knowledge golden-query evaluator (`eval/knowledge/`, wired into `tests/knowledge/test_eval.py`); live agent execution still deferred |

## Key verified facts (no guessing)

- Houdini 21.0.440. Install path is **machine-specific** — Machine A:
  `C:\Program Files\Side Effects Software\Houdini 21.0.440`; Machine B: `D:\houdini`
  (hython `bin\hython.exe`, bundled Python `python311\` = 3.11.7, PySide6).
  `scripts/env_probe.sh` detects which is present.
- The agent venv contains only the locked Runtime dependencies; Houdini-side
  integration uses the authenticated typed Secure Bridge protocol.
- Multi-output SOP subnets require internal `output` nodes with explicit
  `outputidx` (a vanilla subnet has one effective output) — see `CLAUDE.md` gotcha #3.
- DeepSeek V4 routes through `ChatAnthropic` on `https://api.deepseek.com/anthropic`
  (Foundation). Exact model ids only: `deepseek-v4-pro` / `deepseek-v4-flash`
  (legacy `deepseek-chat` / `deepseek-reasoner` deprecated 2026/07/24 — rejected by
  the adapter). Provider swap is one line: `EEE_LLM_PROVIDER=anthropic`.
- Deep Agents 0.6.12 auto-adds a `general-purpose` subagent + `task` tool unless a
  harness profile disables it — `eee_agent/harness.py` does so for both anthropic +
  openai keys (verified against the real 0.6.12 API).
- `create_deep_agent(model, tools, *, system_prompt, skills, memory)` signature
  confirmed.
