# Task 18-H Product UI Convergence Plan

Status: first product-mode slice implemented locally; interactive Houdini GUI
acceptance remains deferred.

## Product direction

The subject is a procedural artist working inside Houdini; the panel's single
job is to turn one model description into one reviewable, validated scene
change. The visual system keeps the existing Houdini-adjacent graphite
instrument palette (`#17191D`, `#20242A`, `#63C7C9`, `#FF7A1A`) and uses
Bahnschrift SemiCondensed for state, Segoe UI for prose, and Consolas only for
diagnostics. Its signature element is the amber model-plan strip: it appears in
the main modeling flow only when a proposal genuinely needs review.

The generic control-plane framing was removed: normal mode now exposes MODEL
and REVIEW, with a review banner and the explicit action “Approve and build”.
Scene/Workspace tabs, instance/epoch/revision rail, raw affected paths, and
last-event text stay hidden until Details is enabled. This preserves the
existing industrial identity while spending visual emphasis only on the one
human decision that matters.

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
