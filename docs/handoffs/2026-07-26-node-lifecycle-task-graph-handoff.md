# Node lifecycle and task graph handoff — 2026-07-26

## Shipped

- Runtime database schema v8: bounded `task_steps` and `task_nodes` store.
- Scratch DTO lifecycle metadata (`purpose`, `note`, `annotations`, `warnings`).
- `scratch.v2` capability with typed topology queries and Runtime-allowlisted
  deletion.
- Deterministic commit finalization (layered layout, comments, display/render
  flags) with cosmetic warnings kept separate from hard gate verdicts.
- Two-phase `cleanup_nodes` and read-only `task_graph_status`.
- Async task-summary middleware and a panel task-graph text block.

## Verification

- Full offline gate before the final documentation pass: 3542 passed, 12
  skipped; the three expected stale-contract assertions were updated for the
  new tool/command surface.
- Targeted lifecycle/protocol tests: 2558 Runtime/Panel tests passed before
  Task 9 additions; new lifecycle/summary/panel tests also pass.
- `python -m eee_agent.cli versions`: passed.
- Chrome sketch quality smoke: passed with a nonblank 1440×900 PNG.
- Hython lifecycle smoke reaches build, commit, layout, topology, and
  allowlisted deletion. It currently reports SOP display/render flags false
  when freshly re-reading the promoted node after `scratch_commit` returns,
  although a direct post-return `setDisplayFlag`/`setRenderFlag` makes them
  visible. This is an open Houdini deferred-state risk, not a closed acceptance
  claim.

## Follow-up risks

1. Resolve the Houdini SOP network-flag persistence/observation behavior with a
   minimal standalone hython probe, then rerun the smoke without any external
   reassertion.
2. Run a real Provider two-turn journey after setting `EEE_LLM_API_KEY` (do not
   place credentials in chat). Without a key, provider tests remain explicitly
   `not_run`.
3. Review the panel task block manually inside Houdini; Qt-free parser and
   source wiring checks pass.

Preserve the user-owned untracked `docs/html/` worktree content; it is not part
of this handoff's commits.
