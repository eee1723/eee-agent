# Task 16-E Runtime Apply and Recovery Plan

## Boundary

Implement only the design in
`docs/superpowers/specs/2026-07-16-task16-e-runtime-apply-recovery-design.md`.
Do not add a public Apply command, UI/compiler behavior, arbitrary execution,
new effect type, dependency, or merge to `main`.

Authorized production files:

- `eee_agent/changesets/repository.py`
- `eee_agent/changesets/service.py`
- `eee_agent/changesets/__init__.py`
- `eee_agent/houdini_bridge/changeset_provider.py` (new)
- `eee_agent/houdini_bridge/__init__.py`
- `eee_agent/runtime/service.py`
- `eee_agent/runtime/__main__.py`
- `eee_agent/runtime/__init__.py`

`eee_agent/runtime/server.py` and `eee_agent/runtime/protocol.py` may change
only if a regression test requires an explicit continued rejection; no Apply
route may be added.

Authorized tests/docs:

- `tests/runtime/test_changeset_recovery.py` (new)
- focused ChangeSet repository/service/provider/Runtime tests
- runtime process fixture/E2E and disposable Houdini smoke
- Task 16-E review, status, and handoff documents after acceptance

## Task 1: Atomic Repository Primitives

RED:

- fresh begin consumes `Approved`, transitions to `Applying`, and appends the
  state event atomically;
- expiry, digest/binding mismatch, wrong state, or event failure leaves all
  records unchanged;
- recovered `Consumed + Approved` begins without a second consumption;
- receipt completion maps all statuses and commits receipt/state/events in one
  transaction;
- idempotent and conflicting completions behave distinctly;
- before-state recovery transitions `Applying -> Approved` atomically.

GREEN:

- add frozen result records for begin/completion/recovery;
- add connection-scoped approval consumption and receipt insertion helpers;
- add bounded event payload helpers;
- preserve all existing plain persistence methods.

Gate:

```powershell
uv run --frozen --extra eval pytest tests/runtime/test_changeset_repository.py tests/runtime/test_changeset_recovery.py -q
```

## Task 2: ChangeSet Orchestration

RED:

- no Bridge call before begin commit;
- correct Workspace manifest is loaded and passed;
- receipt result commits and notifies only after commit;
- apply transport failure queries receipt, then facts, with no replay;
- before/post/ambiguous/transient outcomes match the design;
- any unrelated Applying/Critical record blocks a new write;
- async production binding and existing sync test binding both work.

GREEN:

- define the narrow async provider protocol;
- implement trusted propose/apply/recover methods;
- implement exact receipt validation and bounded fact evaluation;
- synthesize only the two recovery receipts allowed by the design.

Gate:

```powershell
uv run --frozen --extra eval pytest tests/runtime/test_changeset_service.py tests/runtime/test_changeset_recovery.py -q
```

## Task 3: Production Bridge Provider

RED:

- current binding uses a read-only workspace inspection with nullable epoch;
- preflight/apply/receipt build exact typed requests;
- every call uses discovered Bridge identity, bounded deadline, and short-lived
  client cleanup;
- transport/auth/protocol failures become bounded errors without secrets;
- old Bridge capability fails closed before a write frame.

GREEN:

- add `BridgeChangeSetProvider` beside the accepted Workspace provider;
- inject it from Runtime CLI startup;
- export only the provider class and protocol-relevant types.

Gate:

```powershell
uv run --frozen --extra eval pytest tests/runtime/test_houdini_bridge_client.py tests/runtime/test_changeset_recovery.py -q
```

## Task 4: Runtime Lifetime

RED:

- one task per `change_id`, concurrent callers join it;
- caller cancellation leaves the task running;
- graceful shutdown waits for completion;
- timeout cancellation leaves `Applying` for restart recovery;
- startup recovery runs before serving and never replays;
- public `changeset.apply` remains rejected.

GREEN:

- add trusted proposal/apply/recovery methods to `RuntimeService`;
- track Apply tasks separately from agent Run tasks;
- reconcile ChangeSets after interrupted Run reconciliation;
- keep read-only service available when transient recovery is pending.

Gate:

```powershell
uv run --frozen --extra eval pytest tests/runtime/test_service.py tests/runtime/test_server.py tests/runtime/test_runtime_e2e.py tests/runtime/test_changeset_recovery.py -q
```

## Task 5: Cross-Slice Acceptance

Run:

```powershell
uv lock --check
uv run --frozen --extra eval pytest -q
uv run --frozen python -m compileall -q eee_agent houdini_side tests
uv run --frozen python -m eee_agent.cli versions
uv run --frozen python -m eee_agent.runtime --help
git diff --check
git status --short --branch
```

Run the disposable Houdini smoke through the detected Houdini 21.0.440
installation. Scan changed files for secrets, tokens, Runtime state, broad
execution surfaces, new skips/xfails, and accidental public Apply routing.

Only after the gate passes, update README/CLAUDE/handoff/roadmap with exact
evidence and prepare an independent review result. Do not push or merge without
a separate user request.
