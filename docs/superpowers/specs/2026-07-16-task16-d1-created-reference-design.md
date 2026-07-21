# Task 16-D1 Intra-ChangeSet Created-Reference Design

## Purpose and boundary

Task 16-D1 closes one deliberately deferred gap in the accepted Task 16-D
executor: a later operation in one ordered ChangeSet may refer to a node made
by an earlier `CreateNode`. This supports create-then-set, create-then-connect,
and create-under-created-parent without weakening the typed protocol or the
transaction/rollback guarantees accepted at `3435f4b`.

This slice does not add an operation tag, RPC, permission mode, delete effect,
arbitrary code surface, dependency, Runtime orchestration, B2b workspace
commands, Task 16-E recovery, or UI behavior.

## Created identity and dependency rule

For each `CreateNode`, derive exactly one `NodeRef`:

```text
node_id               = CreateNode.node_id
path                  = parent.path + "/" + node_name
expected_type         = CreateNode.node_type
expected_workspace_id = CreateNode.workspace_id
```

The operation list is a strict forward sequence. A reference that collides
with any declared create identity by stable ID or derived path is a
transaction-created reference, not an existing-scene reference. It is valid
only when all four `NodeRef` fields exactly equal the derived reference and its
producer occurs earlier in the operation list.

Therefore the contract rejects:

- forward references, including a child created under a later parent;
- cycles (every cycle contains a non-earlier dependency);
- a created stable ID paired with a different path/type/workspace;
- a created path paired with a missing/different stable ID/type/workspace;
- duplicate created IDs or paths, as already required.

Aggregate references and conditions that claim a created identity must also
use the exact derived `NodeRef`. They cannot turn a created identity into a
path-only or differently-owned scene reference.

## Policy rule

`OwnedWorkspace` treats only an exact derived created reference as internal.
An exact earlier-created node may be a changed target, wire source, or create
parent even though it is absent from the input WorkspaceManifest. Existing
nodes retain exact manifest ownership checks. `ScopedPatch` continues to
forbid every create. `ProjectChange` retains its accepted explicit effect and
risk rules; D1 does not grant a new privilege.

Affected-node and risk-summary coverage remains exact for all created nodes,
changed targets, and wire sources. Sequence validation belongs to the
ChangeSet contract, so every policy and Bridge caller receives the same
fail-closed semantics.

## Preflight versus transaction-time facts

Every create target, including a future parent, is absent during preflight.
Preflight must still prove both its stable ID and derived path are unused in
the complete bounded scene index. It must not require the created node to
exist or attempt to read its parameter/wire state.

Mandatory preconditions are derived in operation order:

- an existing create parent gets `NodeIdentityEquals`; an earlier-created
  parent does not;
- an existing set target gets identity and initial parm facts when readable
  before any transaction write; an earlier-created target does not;
- existing connect endpoints get identity facts; an earlier-created endpoint
  does not;
- a wire/parm old-value condition is omitted when its required state only
  comes into being through earlier operations in the same transaction.

No omission removes the operation's own expected-old requirement. It moves
that check to the only correct point: immediately before that ordered write.
Checkpoint derivation similarly excludes nonexistent created-node state while
retaining before-state coverage for pre-existing nodes.

## Ordered execution invariant

All operations execute in the accepted single main-thread FIFO callable and
one undo group. Immediately before each mutation:

1. Resolve its references against the current in-transaction index.
2. For an earlier-created reference, verify exact path, type, workspace ID,
   node ID, capability, role, schema version `1`, and `created_by_run`.
3. For `SetParm`, read and exactly compare the current value with
   `expected_old_value`.
4. For `ConnectInput`, read and exactly compare the current input source and
   output index with `expected_old_source`.
5. Journal the inverse before the mutating HOM call, preserving the accepted
   mutate-then-raise protection.

For create-under-created-parent, step 2 occurs immediately before the child
`createNode`. This prevents a partially mirrored or substituted parent from
authorizing another write.

An immediate identity or expected-old mismatch is an apply failure. If no
prior write occurred it is zero-write. If prior operations wrote, the existing
strict reverse rollback, receipt classification, reconciliation, and write
freeze rules apply. A failed current comparison does not journal a fictitious
inverse or count that operation as applied.

## Required evidence

Deterministic fake-HOM tests must prove legal multi-level chains and reject
forward, cyclic, and contradictory identities at construction. Failure
injection must prove JIT stale facts cause no current write, all prior writes
roll back in reverse order, receipts remain truthful, and uncertain recovery
still freezes writes. Existing cancellation, FIFO, idempotency, receipt,
postcondition, rollback, and protocol tests remain green.

A fresh Houdini 21.0.440 smoke must execute create parent, create child under
that parent, set a parm on a created node, connect using created endpoints,
query the receipt, replay idempotently with zero duplicate writes, and clean up
without saving a HIP file.
