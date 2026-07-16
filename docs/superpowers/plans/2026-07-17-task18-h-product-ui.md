# Task 18-H Product UI Convergence Plan

## Final normal workflow

```text
request -> plan/progress -> bounded preview -> approve -> applying/validating
        -> completed result or actionable recovery
```

## Changes

- Preserve Session/Run recovery but default to the last active Session.
- Collapse Workspace create/bind fields into Advanced Inspector.
- Automatically select bootstrap vs existing Workspace.
- Replace permanent approval tab emphasis with an approval drawer/card.
- Show one status lane for Planning, AwaitingApproval, Applying, Validating,
  Repairing, Completed, Failed, or RecoveryRequired.
- Keep Scene, IDs, revisions, raw receipts, and manual inspect/bind under an
  explicit diagnostics surface.
- Add Validation and Artifacts inspector sections.
- Maintain Chinese IME-safe composer behavior and narrow dock support.

## Independent work vs deferred work

Reducers, command schemas, snapshots, reconnect behavior, rendering state, and
source-boundary tests can be completed autonomously. Final focus, IME, layout,
mouse, and visual acceptance remain a later real Houdini GUI gate.

