# Task 17-B Interactive Runtime Panel Plan

Status: implemented and offline-accepted in `13e0782`, with Qt WebSocket frame
delivery corrected in `423a0e4` and high-volume Session reopen corrected in
`15b6c00` after the first real tests. Chinese IME and remembered/default
Session selection are corrected in `73c6214`; `7d8d552` additionally prevents
IME candidate-confirmation Enter from accepting the Session title dialog.
`5174378` replaces the embedded Run Request `QPlainTextEdit` with the
IME-verified single-line editor. The focused panel/server gate passes 174
tests and the full suite passes 2106 tests with the single existing optional
WSL skip. Real Houdini
Run/reconnect/stop/empty-approval acceptance is pending.

## Boundary

Implement only
`docs/superpowers/specs/2026-07-16-task17-b-interactive-runtime-panel-design.md`.

Do not add public `changeset.apply`, operation JSON input/output, direct HOM,
SQLite access from the panel, agent graph ownership, a legacy rpyc fallback,
automatic approval, or Task 18 compiler behavior.

Preserve the accepted Task 17-A Scene surface and rollback panel.

## Slice 17-B1: Bounded ChangeSet read model

Production files:

- `eee_agent/changesets/repository.py`
- `eee_agent/changesets/service.py`
- `eee_agent/runtime/protocol.py`
- `eee_agent/runtime/server.py`

Tests:

- `tests/runtime/test_changeset_repository.py`
- `tests/runtime/test_changeset_service.py`
- `tests/runtime/test_protocol.py`
- `tests/runtime/test_server.py`

RED:

- `changeset.list` rejects wrong Session IDs, bool limits, limits outside
  `1..50`, missing/extra fields, and unknown protocol shapes;
- summaries are scoped, newest-first, and limited;
- affected paths/effects truncate at 12 with exact full counts;
- summaries never contain operations, parameter values, checkpoints,
  conditions, tokens, or tracebacks;
- optional approval and receipt records are integrity-checked in the same
  repository view;
- corruption fails closed.

GREEN:

- add one consistent recent ChangeSet view query;
- add frozen bounded summary DTOs;
- add service routing and exact server validation;
- keep public `changeset.apply` rejected.

Gate:

```powershell
uv run --frozen --extra eval pytest tests/runtime/test_changeset_repository.py tests/runtime/test_changeset_service.py tests/runtime/test_protocol.py tests/runtime/test_server.py -q
```

## Slice 17-B2: Interactive client state

Production files:

- `eee_agent/panel/client_state.py`
- `eee_agent/panel/runtime_state.py` (new)
- `eee_agent/panel/__init__.py`

Tests:

- `tests/panel/test_client_state.py`
- `tests/panel/test_runtime_state.py` (new)

RED:

- exact UI command allowlist and per-command payload validation;
- token never enters repr/errors;
- snapshot validates Sessions, Runs, active Run, and boundary;
- Run reducer handles create/state/delta/tool/final/failure events;
- stale/duplicate events do not regress state;
- bounded transcript/activity memory;
- ChangeSet summaries validate exact shapes and truncation facts;
- relevant ChangeSet events request authoritative refresh.

GREEN:

- expand strict command construction without an unrestricted serializer;
- implement pure snapshot/event reducers;
- expose immutable UI snapshots and decision eligibility.

Gate:

```powershell
uv run --frozen --extra eval pytest tests/panel -q
```

## Slice 17-B3: Docked Run and approval surfaces

Production files:

- `houdini_side/runtime_panel.py`
- installation documentation only if controls change

Tests:

- `tests/panel/test_panel_package.py`
- focused Qt/offscreen tests where the environment supports them

GREEN:

- retain the common status/epoch rail;
- add `RUN`, `APPROVALS`, and `SCENE` work surfaces;
- implement Session chooser/new Session;
- implement prompt composer, Start, Stop, bounded output/activity;
- implement the approval gate ticket and exact approve/reject actions;
- refresh authoritative ChangeSet summaries after relevant events/decisions;
- render terminal receipt and critical-recovery evidence;
- preserve panel-close lifecycle behavior and the accepted Scene inspector.

Visual review:

- render populated Run, pending approval, critical recovery, and empty states
  offscreen at a narrow dock size;
- verify only pending authorization uses the amber gate emphasis;
- verify no generic dashboard tiles or chat bubbles were introduced.

## Slice 17-B4: Cross-slice acceptance

Run:

```powershell
uv lock --check
uv run --frozen --extra eval pytest -q
uv run --frozen python -m compileall -q eee_agent houdini_side tests
git diff --check
git status --short --branch
```

Real Houdini 21.0.440 test:

- create/select a Session;
- start one read-only Run;
- observe status, tool activity, output, and terminal state;
- close/reopen the panel;
- restart Runtime and verify recovery;
- request Stop during an active Run;
- verify approval empty state before Task 18;
- compare scene and filesystem facts for zero mutation.

After user acceptance, record exact evidence in a Task 17-B handoff/review.
Do not push or merge without a separate user decision.
