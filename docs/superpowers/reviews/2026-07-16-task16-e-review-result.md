# Task 16-E Independent Review Result

## Status

Task 16-E is accepted and included in the focused local Task 16-E commit. It has
not been pushed. Task 17/UI and Task 18/compiler work did not start.

## Accepted Behavior

1. Fresh Apply consumes the exact approval, transitions
   `Approved -> Applying`, and appends the durable state event in one
   transaction before Bridge I/O.
2. `Applied`, `AlreadyApplied`, `RolledBack`, `Partial`, and
   `CriticalRecovery` receipts map truthfully to durable state and commit with
   state/outcome events atomically.
3. Runtime owns one background Apply task per `change_id`. Caller disconnect,
   cancellation, and Run Stop do not cancel a transaction past the durable
   boundary. Graceful shutdown waits, then leaves timed-out work `Applying`.
4. Startup recovery queries receipt before preflight and never replays a write.
   Proven before state returns to `Approved` with the approval still
   `Consumed`; proven post state records `AlreadyApplied`; contradictory or
   incomplete evidence records `CriticalRecovery`.
5. Receipt identity/binding and preflight binding, Workspace revision,
   condition-result consistency, and fact-key uniqueness are cross-checked.
   Mismatches fail closed and never become success.
6. Transient Bridge absence leaves `Applying` and blocks new writes without
   permanently inventing scene corruption. Partial/Critical outcomes persist a
   global write freeze.
7. Production uses a discovered, authenticated, short-lived
   `BridgeChangeSetProvider`. The public Runtime still rejects
   `changeset.apply`; no raw operation upload or LLM write tool was added.

## Review Corrections

The first implementation delayed Apply notifications in the caller task, so a
disconnect could leave committed events unbroadcast. Runtime now owns execution
and notification in the same shielded background task.

The first recovery pass checked receipt identity but did not correlate every
preflight response fact with the stored Workspace/condition set. The final
implementation validates Workspace revision, exact condition kinds and
aggregate truth, and unique node/parameter/wire fact keys before accepting
before/post evidence.

Invalid receipt identity/binding now enters bounded recovery immediately rather
than waiting silently for a future restart.

## Evidence

- Final targeted recovery/process gate: `20 passed`.
- Full offline suite: `2037 passed, 1 skipped` in 97.14 seconds.
- Existing skip only: optional WSL/Windows probe unavailable; no new skip or
  xfail.
- `uv lock --check`: 69 packages, exit 0.
- `compileall`: exit 0 for `eee_agent`, `houdini_side`, and `tests`.
- `git diff --check`: exit 0.
- CLI versions and Runtime help: exit 0.
- Process hard-crash evidence: after external effect the database held
  `Applying + Consumed` and no app receipt; restart persisted `Applied`, one
  `changeset.applied`, and external `apply_count == 1`.
- Houdini 21.0.440 smoke: exit 0 with `B2B SMOKE OK` and `SMOKE OK`; D1
  create/set/connect, cached receipt, idempotent replay, Workspace no-write
  lifecycle, and disposable cleanup all passed.

## Residual Boundary

The Houdini smoke exercises the real executor/receipt/FIFO and the process test
exercises Runtime crash/recovery through a file-backed external Bridge evidence
provider. A single automatic smoke that drives trusted Runtime Apply through the
live network Bridge into Houdini is not added in this slice; there is still no
public Apply command by design. The strict provider DTO/client/transport tests
cover that composition boundary.

Task 17 UI remains separate. Pushing this commit and any merge to `main` require
separate user decisions.
