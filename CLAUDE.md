# EEE Procedural Modeling Agent — Project Context

This repository contains a Houdini 21 procedural-modeling agent. The supported
production path is the persistent Runtime plus the authenticated, typed Secure
Bridge. Read this file before changing Runtime, Houdini integration, or the
Runtime Control panel.

## Current development handoff

The source of truth is
`docs/handoffs/2026-07-23-runtime-stage-b-pause-handoff.md`. Stage A is accepted
on `feature/a-stability`; the active delivery line is
`feature/b-release-acceptance`. Stage B's deterministic offline, HFS, and
disposable hython gates pass. The exact B candidate still needs the interactive
Houdini GUI checklist and a newly verified real Vision-provider journey before
the final release-readiness decision. A provider result from an older candidate
does not satisfy this gate.
Offline tests and disposable Houdini 21.0.440 `hython` are the default
verification path until
the documented GUI gate is required.

Do not merge `main`, rewrite accepted history, or weaken trusted Workspace,
typed ChangeSet, exact approval, preflight, transactional Apply, receipt,
rollback, restart recovery, model/public write-tool exclusion, or single-FIFO
boundaries. Preserve the accepted Runtime reconnect and Chinese IME fixes.

## Golden rule

Verify Houdini, `hou`, and provider behavior from the local installation or
authoritative documentation; never rely on memory for version/API details.

## Stack and locked decisions

- `deepagents` runs in a uv-managed Python 3.11 environment (`uv.lock`).
- Model construction goes through `eee_agent.providers.ProviderRegistry` and
  `EEE_LLM_PROVIDER`; `eee_agent.model` imports no concrete provider class.
- `configure_deepagents_harness()` disables the implicit general-purpose
  subagent. The contract is covered by `tests/test_harness.py`.
- Modeling layering is native SOP nodes > VEX > HOM (Python only as glue).
- Parametric components use spare parameters (`p_<name>`), real `output` nodes,
  typed anchor wires, and a final merge of component geometry ports.

## Secure Bridge and Runtime Control

- **Bridge**: Runtime scene queries use the authenticated loopback Secure Bridge
  and typed DTOs in `eee_agent.houdini_bridge` (auth, client, queue, contracts,
  capture, changeset, workspace, read-only, and sensitivity providers). Calls are
  main-thread serialized and return bounded plain data; raw HOM objects and RPC
  proxies never cross the agent boundary.
- **Panel**: Houdini's PySide6 **three-pane Runtime Control** panel in
  `houdini_side/runtime_panel/` (a package: `theme`/`view_models`/
  `backend_launcher` Qt-free core + thin `context_bar`/`session_sidebar`/
  `conversation`/`approval_drawer`/`inspector`/`main_window` widgets +
  `client`) starts the authenticated Secure Runtime automatically and submits
  bounded Session/Run/approval commands. The panel owns no agent graph,
  SQLite, or Apply implementation.
- **Agent boundary**: `eee_agent.runtime.agent_context` injects a trusted
  `ReadOnlyProvider`; `eee_agent.runtime.agent_tools.build_read_only_tools()`
  returns exactly five tools: `scene_status`, `query_scene`,
  `inspect_workspace`, `geometry_stats`, and `work_status`.

## Critical gotchas

1. Legacy raw-write modules are historical only. `eee_agent/bridge`,
   `eee_agent/tools`, and the former Houdini-side raw-write entrypoints are not
   importable or supported. New Runtime code must use typed Secure Bridge
   providers or local bounded capabilities.
2. ContextSeek on Windows requires the FILE backend; the embedded seekdb backend
   is Linux-only. Keep Phoenix/ContextSeek disabled when their optional extras
   are absent from `uv.lock`.
3. A vanilla SOP subnet has one effective output. Distinct routable ports use
   internal `output` SOP nodes with explicit `outputidx` values; connect at the
   subnet level and preserve real anchor wires.
4. Spare parameters are added with `addSpareParmTuple` and inspected through
   `spareParms()`. Scalar parameters have no suffix; vector components use
   `x/y/z`. Strict slider ranges use `setMinValue`, `setMaxValue`,
   `setMinIsStrict`, and `setMaxIsStrict`.

## Houdini installation

Both supported machines use Houdini 21.0.440. Probe the active installation
with `scripts/env_probe.sh` before Houdini work:

- `C:\Program Files\Side Effects Software\Houdini 21.0.440`
- `D:\houdini`

Use that installation's `bin\hython.exe`; do not copy `.venv` between machines.

## How to run and verify

- Dependency/version probe: `uv run --frozen --extra eval python -m eee_agent.cli versions`.
- Runtime server: `uv run --frozen --extra eval python -m eee_agent.runtime serve`.
  It must bind loopback only and use the authenticated wire protocol.
- Secure Bridge smoke (from a Houdini installation):
  `tests/runtime/houdini_bridge_smoke.py --state-dir <fresh-temp-dir> --host 127.0.0.1`.
- Offline Runtime tests never require a live LLM or Houdini. Real-provider and
  real-Houdini checks are explicit acceptance smokes.
- Keep Phoenix and ContextSeek off unless their optional extras are installed.
- Runtime data (`app.sqlite*`, `checkpoints.sqlite*`, `runtime.token`,
  `runtime.json`, `runtime.lock`, logs, `.env`, and `.venv`) is local state and
  must not be committed.

## Runtime status

Runtime is persistent, authenticated, loopback-only, and read-only at the agent
scene boundary. It stores sessions/runs/events and LangGraph checkpoints under
`EEE_RUNTIME_HOME` (absolute path) or `%LOCALAPPDATA%\EEEAgent`. The five secure
read-only tools are the only scene-query tools exposed to the model. Workspace
creation/binding/switching/inspection is trusted scene context, never write
permission. Typed ChangeSet proposal, exact approval, preflight, Apply, receipt,
rollback, and restart recovery remain separate guarded capabilities.

## Historical archive (not a supported path)

The former `eee_agent/bridge` and `eee_agent/tools` packages and the old
`houdini_side/start_rpc.py`, `houdini_side/chat_panel.py`, and
`houdini_side/launch.py` raw-write/stdio entrypoints were removed from the
tracked project. They may appear only in migration notes or ignored bytecode
directories; do not restore or invoke them.

## Layout

`eee_agent/core` (IDs, errors, artifacts, events, versioning) ·
`eee_agent/providers` (contracts, registry, secrets, adapters) ·
`eee_agent/houdini_bridge` (authenticated typed bridge) ·
`eee_agent/changesets` (typed ChangeSet policy, services, repositories) ·
`eee_agent/modeling` (strict Brief/Spec contracts, catalog compiler, validation) ·
`eee_agent/knowledge` (read-only Houdini knowledge cache build/store/service) ·
`eee_agent/vision` (advisory post-Apply evaluation contracts/router) ·
`eee_agent/panel` (Runtime panel state projection) ·
`eee_agent/runtime` (paths, models, lock, database, migrations, sessions, runs,
events, protocol, auth, checkpoints, agent runner, service, server) ·
`eee_agent/model`, `eee_agent/app`, `eee_agent/cli` ·
`houdini_side/secure_bridge.py`, `secure_bridge_host.py`, `runtime_panel/`
(three-pane panel package), `changeset_executor.py`, `workspace_inspector.py`,
`install_menu.py`, and `start_phoenix.py` · `eval/` · `skills/` ·
`MainMenuCommon.xml`.

Foundation and Runtime handoffs live under `docs/handoffs/`; approved designs
and execution plans live under `docs/superpowers/`.
