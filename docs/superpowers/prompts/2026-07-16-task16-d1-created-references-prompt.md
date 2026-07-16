# Task 16-D1 Claude Implementation Prompt

You are the implementation engineer. Work RED-first and implement only Task
16-D1 on the current `feature/runtime` worktree. Codex owns architecture,
review, acceptance, and commits.

## Read first

1. `CLAUDE.md`
2. `docs/superpowers/specs/2026-07-15-typed-changeset-policy-design.md`
3. `docs/superpowers/specs/2026-07-16-task16-d1-created-reference-design.md`
4. `docs/superpowers/plans/2026-07-16-task16-d1-created-references.md`
5. Accepted contracts/policy, Houdini executor/preflight, and focused tests
6. `docs/superpowers/reviews/2026-07-16-task16-d-review-result.md`

Preserve all Codex-owned uncommitted files under `docs/`. Do not edit, stage,
restore, clean, or commit documentation. Do not commit any implementation;
Codex will review and commit after independent acceptance.

## Authorized files

- `eee_agent/changesets/contracts.py`
- `eee_agent/changesets/policy.py`
- `houdini_side/changeset_executor.py`
- `eee_agent/houdini_bridge/changesets.py` only if strict decode compatibility
  actually requires it
- `tests/runtime/test_changeset_contracts.py`
- `tests/runtime/test_changeset_policy.py`
- `tests/runtime/test_changeset_executor.py`
- `tests/runtime/test_changeset_bridge_preflight.py`
- `tests/runtime/test_changeset_bridge_transport.py`
- `tests/runtime/changeset_houdini_smoke.py`

Do not touch Runtime service/database/protocol, workspace commands, client or
server surfaces, dependencies/lock, UI, or unrelated tests. Do not start B2b
or Task 16-E. Do not add a DTO tag, operation, RPC, delete, arbitrary code, or
another HOM queue/thread.

## Required semantics

Treat the ChangeSet operations as a strict forward sequence. Derive a complete
NodeRef for every create. Any operation reference colliding with any declared
create by stable ID or derived path must exactly match all four derived fields
and must point to an earlier create. Reject forward references, cycles, and
wrong/missing ID/path/type/workspace at ChangeSet construction/parse time.

`OwnedWorkspace` accepts only exact earlier-created refs as non-manifest
internal nodes. Existing refs keep exact manifest checks. `ScopedPatch` still
forbids create; ProjectChange retains its existing explicit rules.

Preflight must prove global ID/path absence for every create target, but must
not require transaction-created nodes to exist or read their parm/wire state.
Mandatory preconditions/checkpoints must be ordered and omit only facts that
cannot exist until earlier operations run.

Inside the accepted one-FIFO, one-undo-group transaction, compare each set's
current value to `expected_old_value` and each connect target's current input
to `expected_old_source` immediately before its write. Before any use of an
earlier-created node, especially create-under-created-parent, verify its exact
current path/type/workspace/node ID plus capability, role, schema version `1`,
and `created_by_run`. Preserve journal-before-mutate behavior.

On a JIT mismatch, the current operation makes zero writes. If earlier writes
exist, use the accepted reverse rollback, reconciliation, receipt, and write
freeze semantics. Do not count the failed op as applied or journal a false
inverse.

## RED-first coverage

Add focused failing tests before production edits for:

- create -> set;
- create -> connect as target and as source;
- create -> child create and a multi-level legal chain;
- forward created target/source/parent references and cycles;
- wrong/missing created path, ID, type, or workspace;
- all create IDs/paths still proven absent globally at preflight;
- no preflight parm/wire lookup for created nodes;
- JIT parm/wire stale mismatches, zero current write, reverse rollback;
- created-parent mirror/path/type tampering before child create;
- rollback failure classification and write freeze;
- cancellation, idempotency, receipt, postcondition, policy-mode, and strict
  transport regressions.

Prefer small shared helpers that encode one invariant. Do not weaken tests or
replace exact assertions with broad success assertions.

## Verification and report

Run the focused D1 files, then the full focused Task 16 gate from the plan.
Do not run or claim the real Houdini acceptance; Codex owns that independent
gate. Stop and report:

- exact changed files;
- RED failures observed before production changes;
- tests run with pass/fail/skip counts;
- any unresolved concern or requested scope expansion.

Do not stage or commit.
