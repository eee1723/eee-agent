# Task 16-E Runtime Apply and Recovery Design

## 1. Scope

Task 16-E connects the accepted Runtime approval ledger to the accepted typed
Houdini ChangeSet executor. It adds trusted service orchestration, durable
receipt/state/event commits, and restart recovery for an interrupted
`Applying` record.

This slice does not add a public `changeset.apply` WebSocket command, accept
operation JSON from a client, expose a write tool to the LLM, add an operation
tag, add a general Bridge RPC, or start Task 17/18 UI/compiler work.

The accepted boundaries remain mandatory:

- Workspace is trusted context, not write permission.
- The canonical ChangeSet digest is the approval identity.
- Approval, Bridge preflight, the single FIFO, transactional execution,
  reconciliation, receipt, and recovery are all required.
- Runtime and Bridge never import `hou`; only the Houdini-side executor does.
- Recovery queries evidence and never automatically replays a write.

## 2. Trusted Interfaces

`ChangeSetService` receives a narrow async `ChangeSetBridgeProvider` seam:

```text
current_binding() -> SceneBinding
preflight(changeset, workspace) -> PreflightResult
apply(changeset, workspace) -> ChangeReceipt
receipt(changeset) -> ChangeReceipt
```

Production uses a discovered, authenticated, short-lived `BridgeClient` for
each call. Offline tests inject deterministic providers. The provider translates
transport errors to bounded `AgentException` values and never returns a token,
HOM proxy, raw traceback, or unbounded payload.

Existing synchronous approval binding providers remain supported for focused
tests. Production approval uses `current_binding()` from the async provider.

`RuntimeService` exposes only trusted Python methods for proposal and apply.
The WebSocket server continues to reject `changeset.apply`. Task 18 may later
connect its typed compiler to the trusted proposal method; Task 17-B may later
render approval and receipt state without receiving raw write authority.

## 3. Durable State Transitions

### 3.1 Begin Apply

The repository performs one transaction before any Bridge I/O:

1. Read and integrity-check the ChangeSet and approval.
2. Require ChangeSet state `Approved`.
3. Require the canonical digest and approval binding to match the ChangeSet.
4. For a fresh apply, require unexpired `Approved` and rewrite it to
   `Consumed`.
5. For an explicit retry after a proven before-state recovery, accept the
   already `Consumed` approval without consuming it again.
6. Transition `Approved -> Applying`.
7. Append `changeset.state_changed` in the same transaction.

No Bridge request is sent unless this transaction commits. Notification occurs
only after commit.

### 3.2 Complete Apply

The repository validates the receipt against the stored ChangeSet and commits
the receipt, terminal state, and durable events in one transaction.

| Receipt | ChangeSet state | Outcome event |
| --- | --- | --- |
| `Applied` | `Applied` | `changeset.applied` |
| `AlreadyApplied` | `Applied` | `changeset.applied` |
| `RolledBack` | `RolledBack` | `changeset.rolled_back` |
| `Partial` | `CriticalRecovery` | `recovery.critical` |
| `CriticalRecovery` | `CriticalRecovery` | `recovery.critical` |

`changeset.state_changed` is appended before the outcome event. Identical
receipt completion is idempotent. A different receipt for the same `change_id`
is a corruption/conflict and cannot replace durable evidence.

The bounded outcome event carries IDs, digest, receipt status, scene binding,
revisions, applied operation IDs, and `scene_may_have_changed`; it does not
carry the full ChangeSet, parameter values, token, traceback, or raw Bridge
error.

## 4. Apply Lifetime and Cancellation

Runtime registers one in-memory Apply task per `change_id`. Concurrent callers
join the same task. A WebSocket/client waiter cancellation does not cancel a
started Apply task.

Before the durable `Applying` transition, cancellation prevents Bridge I/O.
After the transition, the operation is independent of the caller. Runtime
shutdown waits up to the configured graceful timeout for Apply tasks. If the
timeout expires, it cancels the client-side wait/connection and leaves the
durable state as `Applying`; the Bridge FIFO transaction may still finish and
cache a receipt, which the next Runtime process reconciles.

Run Stop/Force Stop cancels the agent reasoning task but never interrupts a
ChangeSet already across the `Applying` boundary. Task 16-E does not add a new
Run state or make Apply a public Run command.

## 5. Recovery Algorithm

Startup examines every durable `Applying` ChangeSet in deterministic
`change_id` order. It performs no write replay.

1. Query `changeset.receipt` using the exact `change_id`, digest, and approved
   scene epoch.
2. If a valid terminal receipt exists, commit it through the normal completion
   transaction.
3. If the receipt is explicitly unavailable, run the accepted read-only
   `changeset.preflight` against the exact stored ChangeSet and Workspace.
4. If the current instance or scene epoch differs from the approved binding,
   persist a synthetic `CriticalRecovery` receipt and freeze writes.
5. If every original precondition still holds, transition
   `Applying -> Approved` with no receipt and no replay. The approval remains
   `Consumed`; a later explicit trusted retry may reuse only this persisted
   recovery state.
6. Otherwise, evaluate every expected postcondition from the returned bounded
   node/parameter/wire facts. If all hold, persist an `AlreadyApplied` receipt.
7. If neither the before state nor the complete post state is provable, persist
   `CriticalRecovery` and freeze writes.

The accepted preflight operation deliberately rejects a create target that is
already present. Therefore, if the Bridge receipt cache is unavailable after a
successful create, the current protocol cannot prove that post-state. Recovery
must classify it as ambiguous and `CriticalRecovery`; it must not manufacture
success. A future protocol revision may add a separately reviewed read-only
reconciliation probe, but Task 16-E does not weaken or reinterpret preflight.

Transient Bridge absence, deadline, or cancellation is not contradictory scene
evidence. Runtime leaves the record `Applying`, blocks all new writes in
memory, and remains available for read-only work. Recovery may be retried by a
trusted service call or a later Runtime restart. It does not permanently label
a transient outage as scene corruption.

## 6. Recovered Receipts

An `AlreadyApplied` receipt reconstructed from post-state evidence uses:

- the stored `change_id` and approved instance/epoch;
- the stored base revision as `before_revision`;
- a SHA-256 over the canonical bounded preflight evidence as `after_revision`;
- all declared operation IDs in declared order;
- exact postcondition results, all passing;
- `scene_may_have_changed=False`.

A synthetic `CriticalRecovery` receipt uses a SHA-256 over the bounded recovery
evidence for `after_revision`, no unproved applied operation IDs, failed
condition evidence where available, and `scene_may_have_changed=True`.

These hashes are recovery evidence hashes, not claims that Runtime reproduced
the executor's private snapshot hash.

## 7. Write Freeze

New Apply begins only when there is no other `Applying` record and no
`CriticalRecovery` record. An unresolved `Applying` record is a temporary
global write block. A durable `CriticalRecovery` record is a persistent global
write freeze. The error is `recovery.critical_required` with
`scene_may_have_changed=True` only for durable ambiguous outcomes.

Read-only Runtime, Workspace inspection, event replay, snapshots, and receipt
queries remain available while writes are blocked.

## 8. Production Wiring

`python -m eee_agent.runtime serve` constructs one production
`BridgeChangeSetProvider` from the Runtime state directory and injects it into
`RuntimeService.open`. It remains separate from Runtime authentication and uses
only Bridge discovery plus `bridge.token`.

The existing `BridgeWorkspaceFactProvider` remains the Workspace inspection
provider. No token or identity is copied into SQLite or discovery JSON.

## 9. Acceptance

Offline tests prove:

- `Applying` and approval consumption commit before Bridge I/O;
- event append failure rolls back both state and approval consumption;
- disconnect/caller cancellation does not cancel an accepted Apply;
- shutdown waits for a short Apply and leaves a timed-out Apply recoverable;
- all five receipt statuses map truthfully and atomically;
- duplicate completion is idempotent and conflicting receipt is rejected;
- receipt recovery, before-state recovery, post-state recovery, and ambiguous
  recovery follow the exact order above;
- instance/epoch drift and unprovable create post-state freeze writes;
- transient Bridge absence leaves `Applying` without automatic replay;
- a consumed approval is not consumed again on explicit recovered retry;
- public `changeset.apply` remains rejected and the agent tool allowlist remains
  read-only;
- Runtime imports do not import `hou`, `rpyc`, or the legacy bridge.

Process tests prove a Runtime crash after Bridge effect/before app receipt can
recover from the Bridge receipt without duplicating the effect. The real
Houdini smoke uses a disposable unsaved scene, applies through Runtime,
restarts Runtime while Bridge remains alive, recovers the receipt, proves
idempotency, and cleans up without saving.
