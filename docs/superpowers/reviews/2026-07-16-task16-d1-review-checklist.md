# Task 16-D1 Independent Review Checklist

- Base: Task 16-D accepted at `3435f4b`; docs tip `2556b8a`
- Scope: intra-ChangeSet references to nodes created earlier in the same
  ordered transaction only
- Promotion: do not start B2b/16-E/UI; do not push/merge

## Scope and contract

- [ ] Only authorized files changed; docs remain Codex-owned.
- [ ] No DTO/RPC/effect/dependency/general execution surface was added.
- [ ] Created references collide by ID or path, then require exact full
      NodeRef equality and an earlier producer.
- [ ] Forward refs, cycles, and wrong/missing ID/path/type/workspace fail at
      construction and strict wire parse.
- [ ] Existing duplicate-create and canonical digest behavior remains stable.

## Policy and preflight

- [ ] OwnedWorkspace exempts only exact created refs from manifest membership;
      ordinary non-manifest write targets remain denied.
- [ ] ScopedPatch still forbids create; ProjectChange risk/effect rules remain.
- [ ] Every create target's stable ID and path are proven globally absent.
- [ ] Preflight never requires created refs to exist or reads their parm/wire
      state, while existing-node stale facts still fail before writes.
- [ ] Ordered mandatory conditions/checkpoints retain all provable before facts
      and omit only transaction-produced state.

## Transaction and recovery

- [ ] Every HOM read/write remains inside the accepted main-thread FIFO and
      one synchronous transaction callable.
- [ ] Created refs are revalidated against exact identity and all six mirrors
      immediately before use; created parent is checked before child create.
- [ ] Parm and wire expected-old facts are read and compared immediately before
      each write, including repeated operations in one ChangeSet.
- [ ] Failed JIT comparison makes no current write/inverse/applied-op claim.
- [ ] Earlier effects roll back in strict reverse order with truthful evidence.
- [ ] Partial/CriticalRecovery and write freeze behavior remain fail-closed.
- [ ] Cancellation, idempotent replay, receipt query/cache, reconciliation,
      and scene_may_have_changed invariants remain accepted.

## Evidence

- [ ] RED-first tests cover legal chains, all forward/identity failures,
      preflight absence, JIT stale facts, rollback, and mirror tampering.
- [ ] Focused/full tests pass with only the existing WSL skip.
- [ ] `uv lock --check`, compileall, diff check, and scope audit pass.
- [ ] Fresh Houdini 21.0.440 D1 smoke passes, replays idempotently, cleans up,
      and saves nothing.
- [ ] Implementation and Codex docs are committed separately; nothing pushed
      or merged.

Any accepted forward reference, created identity ambiguity, preflight read of
nonexistent state, missing JIT old-value check, created-parent authority gap,
untracked mutation, false-success receipt, incomplete rollback, write outside
the FIFO, fake-only HOM assumption, unauthorized file, or new regression is
blocking.
