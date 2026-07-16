# Task 18-A Review Result - 2026-07-16

## Decision

Task 18-A is Codex-accepted locally at implementation commit `09ea256`, on
top of the design/plan commit `d37fba1`.

This is a pure-Python foundation gate. It intentionally does not claim a
Houdini or live-provider acceptance result.

## Scope reviewed

- strict modeling intent contracts;
- explicit component/node dependency validation;
- developer-owned node catalog;
- deterministic ProceduralSpec to typed ChangeSet compiler;
- policy evaluation and ChangeSet budget boundaries;
- repair budget maximum-two invariant; and
- import/dynamic-execution boundary tests.

## Evidence

- focused modeling + Task 16 contract/policy gate: **237 passed**;
- complete offline suite: **2132 passed, 1 skipped**;
- `uv lock --check`: passed, 69 packages;
- compileall: passed;
- `git diff --check`: passed;
- worktree clean after the implementation commit.

The single skip is the pre-existing optional WSL/Windows environment probe.

## Findings closed by the implementation

1. Model JSON cannot mint ChangeSet operations, expected-old values, risks,
   conditions, checkpoint plans, permission modes, Houdini paths, or stable
   executor IDs.
2. Component references must be explicit and acyclic; hidden cross-component
   dependencies fail closed.
3. Only catalog-approved node types, parameter names/shapes, input indexes, and
   output indexes compile.
4. Expected-old values come from catalog defaults, never from model output.
5. Scene binding, Workspace root/revision, node absence, postconditions, risk,
   affected nodes, and policy digest are compiler-derived.
6. Typed ChangeSet operation and condition budgets are checked before DTO
   construction, with bounded modeling error codes.
7. Repair attempts cannot exceed two per validator stage.

## Deferred work

Task 18-B is not started. It is the next bounded slice and requires its own
review before adding a model-facing proposal tool. Houdini catalog verification
and real pending-approval/Apply testing begin only after that integration
exists.

