# Runtime panel three-pane redesign (H22 Pluto visual language)

Date: 2026-07-21
Workspace: `E:\eee-agent\.worktrees\runtime`
Branch: `feature/runtime`
Status: approved direction, pending implementation plan

Supersedes the layout portion of
`2026-07-16-task17-b-interactive-runtime-panel-design.md` and implements the
panel vision of section 11 in
`2026-07-13-houdini-general-agent-architecture-design.md`
(Session Sidebar | Conversation + Run Activity | Inspector).

## 1. Goal and scope

Rebuild the Houdini Runtime panel view layer as a three-pane dockable panel
with a conversation-centric agent surface, using the Houdini 22 Pluto dark
theme as the visual reference. The redesign runs on Houdini 21 (Qt 6.5.3,
PySide6); Houdini 22 (Qt 6.8.3, PySide6) compatibility is a free consequence
of using PySide6 only — no H22-only APIs, no H22 migration work in scope.

In scope:

- Three-pane layout with responsive drawer collapse.
- Top context bar with runtime/bridge/run status.
- Static conversation record (no fabricated streaming).
- Approval gate as a dedicated drawer.
- Automatic backend startup chain (panel spawns `eee_agent.runtime serve`
  when discovery is missing).
- Visual theme from H22 Pluto design tokens.

Out of scope (explicitly deferred):

- Streaming reasoning / text deltas (the Runtime does not emit them; the UI
  must not fake them).
- Houdini 22 migration or H22-only features.
- Changes to `eee_agent/panel/client_state.py` and
  `eee_agent/panel/runtime_state.py` (the pure state layer is frozen).
- Changes to the Runtime protocol, commands, or event payloads.
- Houdini Knowledge, Vision provider, and modeling logic.

Sequencing decision (agreed): the UI rebuild lands **before** the S9
interactive GUI checklist, so the checklist runs exactly once against the
final UI. The real provider journey already passed; the backend logic is
considered proven and is not re-gated by this work.

## 2. Current state

- `houdini_side/runtime_panel.py` is a 2134-line single file implementing the
  task17-b narrow dock: QTabWidget with MODEL / REVIEW visible and SCENE /
  WORKSPACE / ARTIFACTS gated behind a developer-details toggle.
- The panel connects to an external Runtime process over WebSocket using the
  discovery file in `runtime_state_dir` (`EEE_RUNTIME_HOME` override). It
  never starts the backend; a missing backend shows a generic offline state.
- The Houdini-side Secure Bridge host runs in-process
  (`houdini_side/secure_bridge_host.py`) and publishes a bridge handoff into
  the same state dir; the Runtime discovers it from there
  (`eee_agent/runtime/__main__.py` wires `BridgeReadOnlyProvider`,
  `BridgeWorkspaceFactProvider`, `BridgeChangeSetProvider` from
  `paths.state_dir`).
- Available event surface for the UI: run state machine (`Created`,
  `PreparingContext`, `Planning`, `Finalizing`, `Completed`, `StopRequested`,
  `Stopping`, `Cancelled`, `Retrying`, `Failed`), ChangeSet lifecycle
  (`Proposed` … `AwaitingApproval` … `Applied` / `RolledBack` /
  `CriticalRecovery` / `Rejected` / `Expired`), approval decisions, artifact
  lifecycle (`pending`, `available`, `pending_eviction`, `evicted`,
  `missing`, `failed`), `vision.evaluation_completed`, workspace facts,
  bridge capability/availability failures. No reasoning or text delta events
  exist.

## 3. Architecture

The Qt view layer is rewritten as a package; the pure state layer is reused
unchanged. View-model code that does not need Qt is kept Qt-free and unit
testable.

```text
houdini_side/runtime_panel/
    __init__.py            create_panel() entry, pypanel contract unchanged
    main_window.py         context bar + 3-pane QSplitter + drawer behavior
    context_bar.py         HIP > Session > Workspace; Runtime | Bridge | Run
    session_sidebar.py     session list, new/rename/archive/delete, active mark
    conversation.py        message flow widget + composer (Send/Stop/Stopping)
    run_activity.py        structured activity entries embedded in the flow
    inspector.py           Run / Workspace / Validation / Artifacts tabs
    approval_drawer.py     gate ticket drawer (Approve bound / Reject)
    backend_launcher.py    discovery probe, spawn, readiness wait, diagnostics
    theme.py               Pluto tokens + QSS builder + font/icon helpers
    view_models.py         Qt-free builders: message items, status aggregates
    ime.py                 Windows IME support (moved from the old file)
```

Boundaries:

- `view_models.py` imports `eee_agent.panel.runtime_state` only — no Qt. All
  message-item and status-aggregation logic lives here and is unit tested
  offscreen-free.
- Qt modules are thin shells: they render what view-models return and forward
  user intents to the existing panel client (`eee_agent.panel.client_state`).
- `theme.py` is the only place that knows colors, fonts, and spacing. No hex
  literals outside `theme.py`.
- The existing `EEEAgentRuntime.pypanel` interface and
  `runtime_panel.create_panel()` signature are unchanged; the pypanel file
  keeps importing `houdini_side.runtime_panel`.

### 3.1 Layout

```text
+--------------------------------------------------------------------+
| HIP > Session > Workspace                                          |
| Runtime: online | Bridge: ready | Run: Planning   [sidebars][insp] |
+------------+---------------------------------------+---------------+
| Session    |  conversation flow                    | Inspector     |
| Sidebar    |  - user message                       | Run           |
|  ~220px    |  - proposal card                      | Workspace     |
|            |  - approval result card               | Validation    |
|            |  - validation / vision / artifact     | Artifacts     |
|            |                                       |               |
|            +---------------------------------------+               |
|            |  composer (multiline, Send/Stop)      |               |
+------------+---------------------------------------+---------------+
```

> **Implementation note (2026-07-21):** the Inspector ships with three tabs —
> **Run / Workspace / Artifacts** — not four. The `Validation` tab shown above
> was dropped because the Runtime protocol currently exposes validation
> evidence only through the ChangeSet lifecycle and the deterministic
> `vision.evaluation_completed` event, not as a standalone snapshot channel;
> there was no data to populate a dedicated tab. The absence is locked by
> `tests/panel/test_runtime_panel_sources.py` (`assert "VALIDATION" not in
> source`). Add the tab back only when a structured validation-report channel
> is introduced.

Responsive rule (measured on the panel widget width):

- width >= 900px: all three panes visible.
- 700px <= width < 900px: Inspector collapses to a drawer toggled from the
  context bar.
- width < 700px: Session Sidebar also collapses to a drawer; the
  conversation pane always stays.

The approval drawer overlays the conversation pane (right-anchored) whenever
a ChangeSet is `AwaitingApproval`; it carries the existing gate-ticket
semantics: amber strip, exact operation summary, bounded path list, expiry
countdown, Reject / Approve bound. Approval is never a background tab — it
interrupts.

### 3.2 Conversation model (static, honest)

Messages are append-only records built from real events and command results
by `view_models.py`:

- user prompt (echo of `run.start` input);
- proposal summary card (operations count, permission mode, digest prefix);
- approval outcome card (approved / rejected / expired, with timestamp);
- validation report card (deterministic verdict, bounded failure list);
- Vision evaluation card (status, decision, bounded summary; reuses the
  existing `parse_vision_event()` contract);
- artifact card (bounded thumbnail/list rows, lifecycle state);
- error / notice strip (bounded, never raw tracebacks).

No typing indicators, no fake reasoning blocks, no animated "thinking"
placeholders. When the Runtime gains streaming events later, the message
model gains a streaming item type — the container does not change.

Composer note: the first iteration keeps the proven single-line
`RunRequestEdit` input (explicit Windows IME handling, runs start only from
the Send button). A multiline composer is deferred until IME behavior is
verified on the new layout, because the single-line edit is the input
control the existing IME acceptance evidence covers.

### 3.3 Visual theme (H22 Pluto tokens)

Source: `/d/Houdini22/houdini/config/Themes/PlutoThemeDefaults.json`
(default Pluto dark theme) and `houdini/config/Styles/base.qss` for control
metrics. The user machine runs the "Green" Pluto theme; the panel adopts the
**default** Pluto palette (not the user's Green) so it looks native on any
Houdini install.

Core tokens:

- background `#2d2d2d`; surfaces `#242424 / #282828 / #313131 / #333333 /
  #353535 / #383838` (lowest→highest); tooltip surface `#171717`
- divider `#202020`; pane divider `#222222`
- text `#dddddd`; prominent `#ffffff`; dim `#7f7f7f`; dimmer `#5a5a5a`;
  field text `#f9f9f9`
- input field `#434343`; field alt `#3e3e3e`; view surface `#2f2f2f`
- button `#474e62`; hover `#777f95`; pressed `#313f71` / pressed fg
  `#f7f9ff`
- primary `#7082b9`; secondary `#7c849a`; checked surface `#47578b` /
  fg `#e3ebff`
- highlight (amber, reserved for the approval gate and checked accents)
  `#fdba00`; highlight fg `#271900`; highlight surface `#755400`
- semantic states keep the task17-b mapping: cyan/blue-grey = read-only /
  ready, amber = pending authorization, red = failed / critical recovery.
  Red/green for validation verdicts follow the existing bounded palette.

Typography and metrics:

- UI font: SideFX Source Sans Pro (ships in `$HFS/houdini/fonts`,
  registered via `QFontDatabase.addApplicationFont`), fallback Segoe UI.
- Monospace: SideFX Source Code Pro, fallback Consolas — for session/run/
  changeset IDs, digests, statuses, counters.
- Sizes follow `resources_ui`: base 9pt, titles 11–13pt.
- Controls follow base.qss metrics: tool buttons 17–19px, tab height 20px,
  input height 17px, group box radius 5px, tool button radius 4px,
  scrollbar 15px.
- Icons: `hou.qt.Icon("BUTTONS_*.svg")` / `STATUS_*.svg` / `MISC_*.svg`
  built-ins only; no bundled custom assets.

Qt binding: PySide6 on both H21 (6.5.3) and H22 (6.8.3). The code imports
PySide6 directly (as the current panel does); no `hutil.PySide` shim is
needed. No Qt 6.6+ APIs may be used (H21 floors at 6.5.3).

### 3.4 Backend startup chain

Fully automatic, discovery-driven:

1. Panel create → Secure Bridge host starts in-process and publishes the
   bridge handoff (unchanged existing behavior).
2. `backend_launcher` probes the runtime discovery file in
   `runtime_state_dir`. Present and fresh → connect (unchanged path).
3. Missing or stale → spawn
   `EEE_PATH/.venv/Scripts/python.exe -m eee_agent.runtime serve`
   (fall back to `uv run --frozen python -m eee_agent.runtime serve` when the
   venv interpreter is absent), cwd = `EEE_PATH`, env inherits plus
   `EEE_RUNTIME_HOME` when set. stderr is captured to a bounded log file
   under the state dir.
4. The launcher waits for the discovery file to appear (10s timeout) and
   then lets the normal connect path proceed. The Runtime itself
   auto-connects to the Bridge via the handoff files.

Ownership and lifecycle:

- Whoever spawns, reaps: a backend started by the panel is gracefully shut
  down when the panel is destroyed / Houdini exits (`onDestroyInterface` +
  `atexit`, using the runtime's graceful shutdown). A backend the panel found
  already running is used but never killed.
- The offline card distinguishes three states with a retry action:
  not running (start/retry), started but timed out (show log path), and
  bridge unavailable (backend runs but reads fail). No generic "offline".

Security bounds (unchanged posture): tokens stay in the state dir files;
spawned process env adds no credentials; the log file is truncated/bounded
and must not echo discovery tokens.

### 3.5 Error handling

- WebSocket disconnect: existing reconnect timer and replay logic are kept;
  the context bar shows Runtime: reconnecting instead of a tab-level banner.
- Bridge unavailable: read-only tools degrade to `bridge.unavailable`
  (existing behavior); the context bar shows Bridge: unavailable in red.
- Approval expiry: drawer countdown reaches zero → drawer closes with an
  expired notice card appended to the flow.
- Spawn failure (missing venv, non-zero exit): bounded error card with the
  log path; retry button; never raises through the pypanel.

### 3.6 Testing

- `eee_agent/panel/*` and its tests: untouched.
- `view_models.py`: pure unit tests (message construction from event
  sequences, status aggregation, bounded truncation rules).
- Qt components: PySide6 is deliberately not a project dependency (the
  frozen `uv lock` gate forbids adding it), so Qt code is verified the same
  way the current panel is: source-boundary tests (AST/text contracts like
  `tests/panel/test_panel_package.py`) plus the manual GUI checklist inside
  Houdini. All logic that needs automated tests must live in the Qt-free
  modules (`view_models.py`, `backend_launcher.py`, `theme.py` tokens).
- IME: the existing Windows IME handling moves to `ime.py` unchanged in
  behavior; the GUI checklist re-verifies it.
- Final gate: the S9 interactive GUI checklist runs once against the new UI
  (narrow/docked layout, focus, Chinese IME, review/approval mouse flow,
  reconnect/restart, artifacts, recovery, full
  MODEL → REVIEW → Approve and build → RESULT journey).

## 4. Migration notes

- The old single-file `runtime_panel.py` is replaced by the package; the
  pypanel interface name, label, and install package JSON are unchanged, so
  existing Houdini package installs keep working.
- The SCENE / WORKSPACE / ARTIFACTS developer-details tabs are absorbed into
  the Inspector; the developer-details toggle disappears (Inspector is a
  first-class pane).
- Panel state that was tab-local (artifact summaries, vision parse) moves
  behind view-models with the same bounded caps (`_MAX_ARTIFACTS = 50`, 16KiB
  evidence bounds, etc.).

## 5. Acceptance criteria

1. Panel opens in Houdini 21 with no manual backend start: bridge handoff,
   backend spawn, discovery wait, and WebSocket connect complete without
   user action; failure paths show the three distinct offline cards.
2. Three panes visible at wide widths; both drawer thresholds behave per
   3.1; the conversation pane never collapses.
3. Conversation flow renders the full real event sequence of the provider
   journey scenario with correct cards and bounded truncation, no fake
   streaming.
4. Approval drawer interrupts on `AwaitingApproval`, enforces expiry, and
   Approve bound / Reject produce the same durable events as today.
5. Visual review against Pluto tokens: no hex outside `theme.py`; fonts and
   icons resolve from Houdini resources on both H21 and H22.
6. Full frozen test suite passes; new view-model and component tests pass;
   the S9 GUI checklist is executed once against this UI.
