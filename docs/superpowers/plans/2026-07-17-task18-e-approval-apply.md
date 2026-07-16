# Task 18-E Approval-to-Apply Plan

## Goal

Make one exact user approval trigger the existing trusted Apply task without
adding a public Apply command or model write tool.

## Slices

1. Add a bounded application summary joining decision, ChangeSet state, and
   optional receipt status without exposing operations or values.
2. Commit approval first; only an exact Approved outcome starts/joins
   `apply_changeset_trusted()`.
3. Shield accepted Apply from WebSocket/client cancellation and stream existing
   durable state events.
4. Reject/expired/stale/digest-mismatch paths prove zero Bridge calls.
5. On bootstrap success, finalize the initial Workspace; normal Apply keeps the
   existing Workspace binding and produces a receipt.
6. Preserve restart reconciliation and single-write FIFO behavior.
7. Update panel reducers so Approve shows Applying/Applied/Recovery status;
   keep raw Apply unavailable.

## Acceptance

- fake-Bridge success, rollback, partial, stale, cancellation, duplicate-click,
  reconnect, restart, and concurrent-approval tests;
- exact durable event ordering;
- real hython success and forced rollback;
- full offline suite, lock, compileall, and diff checks.

