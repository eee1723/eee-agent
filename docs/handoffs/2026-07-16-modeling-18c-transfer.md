# Task 18-C Runtime Proposal Integration Handoff - 2026-07-16

## Current state

- Branch: `feature/runtime`
- Task 18-A strict foundation: accepted at `09ea256`
- Task 18-B pure proposal seam: accepted at `23379df`
- Task 18-C implementation: offline-verified, not yet real-Houdini-accepted
- Full suite: `2144 passed, 1 skipped`
- `uv lock --check`, compileall, and `git diff --check`: passed

## What this slice adds

- A developer-owned `houdini_21_minimal_catalog()` verified with local
  Houdini 21.0.440 hython. It contains only `box`, `grid`, `merge`, `null`,
  and `xform` with literal numeric parameters and bounded input counts.
- `build_agent_runner(..., modeling=True)`, which adds only the
  `propose_modeling` tool and its `ModelingToolContext` schema. The default
  runner remains the exact read-only graph.
- Per-Run Runtime context construction from the trusted SceneBinding and
  healthy Workspace inspection. Missing, stale, conflicting, or unavailable
  context fails closed.
- Production `runtime serve` wiring for the opt-in modeling runner and
  verified catalog. The model can propose a typed ChangeSet through the
  existing trusted proposal/approval service seam; it cannot Apply.
- The Runtime panel now exposes bounded `WORKSPACE` controls for create from
  the current selection, inspect, and bind/refresh using the exact manifest
  revision. These commands are lifecycle operations already present in the
  Runtime protocol; they do not expose Apply or raw Houdini writes.

## Deliberately deferred

No Apply command, raw operation upload, VEX/source execution, panel modeling
editor, automatic approval, or model-controlled Houdini mutation was added.
The proposal path remains approval-gated and Workspace/SceneBinding-bound.
Workspace creation still requires a selected graph whose nodes already carry
EEE ownership metadata; a clean empty scene cannot bootstrap ownership. That
bootstrap/apply slice is intentionally the next design boundary.

The real test returned `The selection does not contain trusted EEE-owned
nodes.`, confirming this boundary. Interactive Houdini verification is now
deferred while autonomous offline/hython work follows
`docs/superpowers/plans/2026-07-17-autonomous-modeling-roadmap.md`.

## Manual Houdini gate (required next)

Restart the Runtime from the current checkout:

```powershell
uv run --frozen --extra eval python -m eee_agent.runtime serve
```

Using the existing Runtime WebSocket client/panel, create or select Session
`test`. Create and bind a Workspace with the normal `workspace.create` and
`workspace.bind` commands, then issue an explicit modeling request that asks
for a small `box`/`xform`/`null` graph and includes a complete strict Brief
and ProceduralSpec payload. Confirm:

1. exactly one bounded proposal summary is returned;
2. the summary is `AwaitingApproval`, includes one ChangeSet ID and digest,
   and contains only operation/effect/path counts;
3. the durable ChangeSet list contains the same ID/digest;
4. approving or rejecting requires the exact digest;
5. no Apply command is available or invoked, and Houdini scene state is
   unchanged before approval.

Also verify that a stale/unavailable Workspace produces a bounded failure and
does not mutate the scene. Record any Runtime/Houdini behavior here before
accepting Task 18-C.
