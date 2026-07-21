# Task 16-C Independent Review Result

## Decision

Accepted at `6050a00` on `feature/runtime`.

Implementation chain:

- `86a6bb6 feat: preflight typed houdini changesets`
- `357d862 fix: resolve owned changeset nodes by stable id before path`
- `6050a00 fix: close two fail-closed gaps in changeset preflight`

## Closed findings

1. Owned references originally resolved `hou.node(ref.path)` before mirrored
   stable ID. The follow-up adds one read-only scene index, resolves unique
   `eee.node_id` first, rejects full-scene duplicate IDs, follows moved nodes,
   and validates mirrored `eee.schema_version`/`eee.created_by_run`.
2. Workspace ownership could be skipped when a NodeRef omitted
   `expected_workspace_id`. The final follow-up requires the current mirrored
   workspace to match the supplied manifest independently of that request hint.
3. Create targets were skipped as expected-absent references. The final
   follow-up rejects existing stable-ID reuse and derived-path collisions,
   including ProjectChange without a manifest.

## Independent evidence

- Authorized implementation delta from `7b3bff8`: exactly 10 Task 16-C files.
- Focused contracts/preflight/client/queue/transport plus Task 16-A regression:
  410 passed.
- Full offline suite: 1769 passed, 1 skipped (the pre-existing optional WSL
  environment probe), no new skip/xfail.
- `uv lock --check`: 69 packages, OK.
- `python -m compileall -q eee_agent houdini_side tests`: OK.
- `git diff --check 7b3bff8..6050a00`: OK.
- Mutation-spy tests prove zero preflight writes; scene reads and preflight use
  the accepted shared FIFO.

## Residual boundary

Real Houdini 21.0.440 HOM behavior and transaction writes remain Task 16-D
smoke/acceptance work. Task 16-C itself advertises and executes only read-only
`changeset.preflight`; it does not add Apply, receipt, rollback, Runtime
orchestration, workspace public commands, or durable success claims.
