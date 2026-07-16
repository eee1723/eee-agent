# EEE Procedural Modeling Agent — Project Context

A **deepagents**-based AI agent that does procedural/parametric modeling in SideFX
Houdini 21. Read this first every session — it captures the hard-won facts (so we
don't re-discover them). Mirrors the auto-memory; kept in-repo so it travels with git.

## Current development handoff

The current source of truth is
`docs/handoffs/2026-07-16-modeling-18b-transfer.md`. The active branch is
`feature/runtime`. Task 16-E, Task 17-A, Task 17-B, Task 18-A, and the pure
Task 18-B proposal seam are accepted. Task 17-B
was implemented in `13e0782`: the docked panel now creates
and selects Sessions, starts/stops Runs, recovers bounded Run state/output,
lists bounded durable ChangeSet summaries, and sends exact approve/reject
decisions without exposing Apply. The first real test exposed binary Runtime
WebSocket frames being ignored by Qt's text-only signal; `423a0e4` makes new
servers send text frames and keeps binary compatibility in the panel. Its full
panel-reopen test then exposed a 256-item outbound queue rejecting the TEST
Session's 315+ event replay; `15b6c00` bootstraps Qt from a snapshot boundary
and adds server-side replay backpressure. `73c6214` replaces the static Session
title prompt with an IME-enabled non-blocking dialog, explicitly enables IME
for Run Request, and persists the last selected Session with highest-`last_seq`
fallback. The real IME retest showed candidate-confirmation Enter still
accepted the dialog; `7d8d552` removes Return acceptance, disables default
buttons, and consumes Enter in the title editor so only an explicit OK click
creates the Session. The next real test showed embedded `QPlainTextEdit` still
failed Chinese input while the title `QLineEdit` worked; `5174378` replaces
Run Request with an IME-safe 16000-character `QLineEdit` and consumes Enter so
Runs start only by button. Its full offline baseline is 2106 passed, 1 skipped;
the focused panel/server gate passes 174 tests. The complete real Houdini
21.0.440 gate passed: Chinese IME/default Session behavior, read-only Run,
high-volume panel reopen, Runtime restart recovery, cooperative Stop to
Cancelled, empty approvals/no Apply, Scene regression, and zero mutation.
The acceptance result is
`docs/superpowers/reviews/2026-07-16-task17-b-review-result.md`. Task 18-A
strict contracts and deterministic compiler are accepted at `09ea256`, with
design/plan `d37fba1`; its result is
`docs/superpowers/reviews/2026-07-16-task18-a-review-result.md`. The Task 18-B
seam is `23379df`, with review
`docs/superpowers/reviews/2026-07-16-task18-b-review-result.md`; Runtime graph
integration and real proposal testing have not started. Do not
rewrite or discard the accepted Task 16-E/17-A commits, `13e0782`, or
the Task 17-B hotfixes
`423a0e4`/`15b6c00`/`73c6214`/`7d8d552`/`5174378`,
merge
`main`, or weaken the trusted
Workspace, typed ChangeSet, approval, preflight, transactional Apply, receipt,
recovery, or single-FIFO boundaries while resuming work.

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

## Runtime (additive, loopback, read-only v1)

A **persistent, authenticated Runtime** lives in `eee_agent/runtime/` and runs
as its own process — additive to the existing CLI, which remains the supported
rollback path. It does **not** replace the Secure HoudiniBridge (deferred).

- **Start**: `uv run --extra eval python -m eee_agent.runtime serve`. Subcommand
  `serve`; `--host` must be `127.0.0.1`, `--port 0` = ephemeral, `--graceful-timeout`
  default 10s. Non-loopback hosts are rejected before any socket is created;
  `--help` documents the options.
- **Loopback only**: binds `127.0.0.1` exclusively; bearer-token handshake auth
  (HTTP 401 on failure). Wire protocol is `eee.runtime/1`.
- **Data home**: `%LOCALAPPDATA%\EEEAgent\` by default; override for local
  testing with `EEE_RUNTIME_HOME=<absolute dir>` (must be absolute). Layout
  under `<home>/state/`: `app.sqlite` (sessions/runs/events),
  `checkpoints.sqlite` (LangGraph), `runtime.lock`, `runtime.json` (discovery —
  host/port/pid/nonce + a token FINGERPRINT only), `runtime.token` (the full
  bearer token — the only place it ever lives).
- **Read-only Houdini agent boundary (v1)**: the Runtime agent uses an exact read-only
  tool allowlist (`hou_status`, `find_nodes`, `describe_node_type`,
  `geometry_stats`, `validate_geometry`, `work_status`, `anchor_graph`) — no
  write/save/export, no implicit general-purpose subagent. Checkpoint continuity
  uses `thread_id = session_id`.
- **Trusted Workspace lifecycle (Task 16-B2b)**: Runtime exposes exactly
  `workspace.create`, `workspace.bind`, `workspace.switch`, and
  `workspace.inspect`. Workspace is trusted scene context, never write
  permission. Create/bind may use exact selection facts; switch ignores
  selection and proves the stored complete manifest. Only nodes already carrying
  all six EEE executor ownership mirrors can enter a Workspace. See
  `docs/superpowers/reviews/2026-07-16-task16-b2b-review-result.md`.
- **Offline tests**: the full Runtime suite — including a real-subprocess
  restart E2E (`tests/runtime/runtime_process_fixture.py`) — runs with NO live
  LLM and NO Houdini. GLM-5.2 and Houdini read-only smokes are MANUAL only (plan
  §"Manual Acceptance") and never block offline acceptance.
- **Docked Runtime control (Task 17-B)**: the Houdini Python Panel owns no
  Runtime, graph, SQLite, or Apply path. It uses exact Session/Run/approval
  commands, bounded snapshot/event reducers, and a read-only `changeset.list`
  projection. Current Runtime agent tools still use the existing localhost
  rpyc bridge behind an exact read-only allowlist; this is separate from the
  panel's authenticated Secure Bridge selection inspector.
- **Don't commit**: runtime SQLite (`app.sqlite*`, `checkpoints.sqlite*`),
  `runtime.token`, `runtime.json`, `runtime.lock`, logs, `.env`, `.venv` — all
  `.gitignore`d. Approved spec:
  `docs/superpowers/specs/2026-07-14-runtime-design.md`; status:
  `docs/handoffs/2026-07-16-runtime-17b-transfer.md`.

## Known limitation
DeepSeek V4 Pro loops on long-horizon tasks (over-iteration). Architecture is proven
(parametric table: change width → legs move). Mitigations now in place: recursion
limit default 999 (`EEE_RECURSION_LIMIT`), deterministic loop guard, read-back
trimming, on-demand compaction — but the strongest single lever remains the MODEL:
Claude is the recommended swap for reliability (one-line via
`EEE_LLM_PROVIDER=anthropic`).

## Layout
`eee_agent/` — **core/** (Foundation: ids, errors, artifacts, events, versioning) ·
**providers/** (Foundation: contracts, registry, secrets, deepseek_v4, anthropic,
openai, factory, events, normalize) · **harness.py** (Foundation: disable implicit
general-purpose subagent) · **runtime/** (persistent loopback Runtime v1: paths, models, lock, database, migrations, sessions, runs, events, protocol, auth, checkpoints, agent_runner, service, server, __main__) · config, model, app, cli · `bridge/` (rpyc client +
plain-Python serialization, no proxies leak) · `tools/` (25 @tool functions:
scene/nodes/vex/compose/inspect/procedural) · context_store, context_trim, loop_guard,
tool_error_trace, workflow_middleware, tracing, system_prompt · `skills/`
(parametric-building, vex-patterns, sop-cookbook, procedural-components) ·
`memory/AGENTS.md` (agent conventions, in-repo) · `houdini_side/` (start_rpc,
chat_panel, launch, install_menu, start_phoenix) · `eval/` · `MainMenuCommon.xml`.
Foundation status: see `docs/handoffs/2026-07-13-foundation-migration.md`.
