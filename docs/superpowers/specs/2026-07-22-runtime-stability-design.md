# Runtime Stability and Failure Visibility Design

Date: 2026-07-22
Stage: A
Baseline: `origin/main` at `5a6880b`

## Goal

Restore a warning-free mainline quality baseline and guarantee that every
failed Run produces bounded, useful, user-visible evidence in both the
conversation and the structured Run inspector.

## Non-goals

- Redesigning the three-pane layout or its visual language.
- Changing the Runtime wire protocol when existing snapshot/event fields are
  sufficient.
- Exposing raw exceptions, tracebacks, prompts, secrets, or provider output.
- Changing ChangeSet approval, Apply, rollback, recovery, or write authority.
- Implementing Task 19-C delivery aggregation.

## Confirmed defects

### Ruff regression

`tests/panel/test_main_window_history_behavior.py` imports `pytest` but never
uses it. The repository's own Ruff command therefore rejects the current
mainline.

### Database test resource leak

`test_d2_update_todos_rejects_non_list` discards the database returned by
`_open()` and has `finally: pass`. The aiosqlite worker survives
`asyncio.run()`, then tries to notify a closed event loop. Running the following
two tests with `PytestUnhandledThreadExceptionWarning` promoted to an error
reproduces the failure deterministically:

```text
test_d2_update_todos_rejects_non_list
test_d2_update_todos_filters_non_mapping_items
```

### Silent failed Run

Runtime emits and persists `run.failed` with an `AgentError`. Panel state stores
the error in `selected_run.failure_json`. `RuntimePanel._maybe_render_output()`
ignores that field and settles the card from `snapshot["output"]`. A failure
before model output therefore becomes an empty Assistant card. History replay
has the same behavior.

## Design

### Quality-gate repair

The history behavior test drops the unused import. The Todo validation test
retains the returned database and closes it in `finally`.

The test configuration promotes unhandled thread exceptions to failures. This
turns resource leaks into an immediate, correctly attributed gate rather than
a warning that may surface in a later test. The rule remains narrow enough not
to convert unrelated third-party deprecations into release blockers.

No production database workaround is added because the confirmed root cause is
the test's missing close, not `RuntimeDatabase.close()`.

### Strict failure view model

`houdini_side.runtime_panel.view_models` gains a Qt-free failure representation
built from a Run snapshot. It accepts only a mapping and extracts these bounded
fields:

- `code`: short stable error identifier;
- `message_for_user`: primary user-facing explanation;
- `retryable`: exact boolean;
- a safe fallback message when the structure is missing or malformed.

The view model never renders `technical_detail_ref`, a traceback, exception
repr, model/provider raw text, or unrecognized nested data. Strings use the
existing title/body bounds.

The result can build an error-tone conversation card and a structured error
section in `RunView`. This keeps all normalization and tone decisions out of
the Qt layer.

### Live terminal rendering

When a Run becomes terminal:

- `Completed` with output settles to the normal Assistant card.
- `Cancelled` without an explicit response produces a bounded cancellation
  notice rather than an unexplained empty card.
- `Failed` with no output replaces the streaming placeholder with the failure
  card.
- `Failed` after partial output preserves the assistant text and appends the
  failure card exactly once.

The panel tracks the terminal result per Run so repeated snapshots cannot
duplicate cards. Session switching clears only view-specific markers; durable
Run evidence comes back from the snapshot.

### History replay

History and live rendering share the same pure terminal-result selection
function. Replay emits the user message followed by one of:

- a final assistant response;
- an assistant response plus failure card;
- a failure card;
- a cancellation notice.

A historical failed Run no longer creates `assistant_message("")`.

### Structured Run inspector

`RunView` gains an optional failure view. `InspectorPane` renders it in the
existing Run tab using theme tokens and semantic error coloring. The existing
status, duration, environment, dependency, Activity, Apply outcome, and Todo
sections remain intact. No mapping is converted into a raw text dump.

Apply receipt errors and Run execution failures remain distinct concepts. The
Run tab may display both when both occurred.

## Data flow

```text
RuntimeService run.failed
  -> durable AgentError payload
  -> RuntimePanelState selected_run.failure_json
  -> Qt-free failure view
  -> conversation terminal result + Run inspector failure section
```

No new privileged capability or write path is introduced.

## Error handling

- A malformed optional failure mapping must not crash or disconnect the panel.
- A `Failed` status with no valid mapping renders a generic bounded failure
  message.
- A non-failed Run ignores stale failure data rather than presenting a
  contradictory state.
- Unknown error codes remain displayable as bounded text but do not select new
  behavior.
- Retry affordance is descriptive only in Stage A; no automatic retry command
  is added.

## Tests

### Gate-focused tests

- Ruff passes on the history behavior module.
- The two-test aiosqlite reproduction passes with thread warnings treated as
  errors.
- The full Run repository suite passes under the same warning rule.

### View-model tests

- valid retryable and non-retryable failures;
- malformed and missing failure mappings;
- title/body truncation;
- no raw technical detail leakage;
- `RunView` retains existing structured sections.

### State and behavior tests

- `run.failed` survives event projection and snapshot reload;
- failed before output;
- failed after partial output;
- repeated terminal snapshots do not duplicate cards;
- historical failed Runs render the same result as live Runs;
- Cancelled and Completed behavior remains correct;
- Session switching cannot mix failure evidence between sessions.

### Stage acceptance

Stage A closes only with a fresh full offline suite, Ruff, Mypy, compileall,
lock, diff, and clean-status evidence. Unexpected warnings count as failure.

## Discovered-work policy

Any additional panel data-flow gap or test lifecycle leak found while working
in these exact components is triaged under the roadmap policy. A defect that
can make a terminal Run invisible, corrupt acceptance evidence, or leak a
background resource blocks Stage A. Independent visual enhancements and new
commands are recorded for a later stage rather than bundled into this repair.
