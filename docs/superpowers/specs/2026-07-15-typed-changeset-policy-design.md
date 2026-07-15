# Typed ChangeSet, Policy, Approval, And Transactional Write Gate

- Date: 2026-07-15
- Scope: Task 16 only
- Status: Codex-reviewed design; implementation not started

## 1. Goal and non-goals

Task 16 adds the smallest auditable write boundary on top of the accepted
read-only Runtime and Secure HoudiniBridge. A model never receives a generic
Houdini write tool. A trusted compiler may propose an immutable, bounded
`ChangeSet`; policy evaluates its effects; a user approves the exact canonical
digest; and only the deterministic executor may apply it on Houdini's main
thread.

This task includes:

- immutable WorkspaceManifest, ChangeSet, policy, approval, precondition,
  receipt, and reconciliation contracts;
- application-database persistence and restart recovery for those records;
- exact Runtime commands/events for workspace inspection and ChangeSet
  approval/rejection;
- additive Bridge capability negotiation, preflight, apply, and receipt query;
- a short transactional executor with deterministic inverse operations; and
- offline tests plus a real hython/Houdini acceptance smoke.

This task does not include:

- an LLM-facing write tool, arbitrary HOM/Python, VEX/source installation,
  shell access, file writes, HIP load/clear/save, HDA definition changes,
  export, capture, or artifact reveal;
- node deletion as a forward operation;
- the docked panel, modeling compiler, repair loop, or visual review; or
- weakening the accepted `scene.query` contract or legacy CLI rollback path.

Task 18 owns production modeling compilation. Task 16 exposes an internal,
typed proposal seam so its policy, persistence, approval, and execution can be
tested without pretending that model output is trusted.

## 2. Security and consistency invariants

1. Default deny is effect-based. Unknown permission modes, operations, fields,
   node references, parameter value types, or effects are rejected before
   policy approval or queue admission.
2. Every write requires an explicit, unexpired, single-use approval for the
   exact canonical SHA-256 ChangeSet digest. There is no permanent global write
   switch, including for an owned workspace.
3. Approval does not authorize changed facts. Immediately before apply, the
   executor re-reads instance ID, scene epoch, workspace revision, target node
   identity/type/ownership, affected parameters, and affected wires. Any
   mismatch produces `changeset.stale` without a write.
4. Runtime persists `Pending` before an external effect. It never persists or
   broadcasts `Applied` until Bridge reconciliation returns bounded read-back
   evidence and the receipt transaction commits.
5. `change_id` is the idempotency key. Repeating an apply either returns the
   existing receipt, proves that all postconditions already hold, or fails into
   recovery. It must not create a second node or repeat a side effect.
6. A write executes as one short main-thread item and one recognizable
   `hou.undos.group`. Automatic rollback uses the executor's captured before
   snapshot and inverse operations, never a blind "undo last" call.
7. If apply or rollback cannot be reconciled, the Bridge returns a partial or
   critical receipt, Runtime freezes subsequent writes for that Houdini
   instance, and the error sets `scene_may_have_changed=true`.
8. The accepted Bridge token remains the only Bridge credential. No ChangeSet,
   approval, receipt, event, log, exception, SQLite row, command line, or
   environment variable contains either full Runtime or Bridge token.
9. DTOs contain JSON facts only. They never contain `hou` objects, callables,
   arbitrary code, filesystem paths other than the bounded HIP path already
   allowed by `SceneBinding`, or unbounded parameter/source values.
10. Read-only clients remain valid. A client must observe the advertised
    `changeset.v1` capability before it may send a write request; absence of the
    capability fails closed with `bridge.capability_unavailable`.

## 3. Ownership and package boundaries

Pure contracts and policy live outside Houdini:

```text
eee_agent/changesets/contracts.py
eee_agent/changesets/policy.py
eee_agent/changesets/repository.py
eee_agent/changesets/service.py
eee_agent/houdini_bridge/changesets.py
```

`contracts.py` and `policy.py` import neither `hou` nor the legacy bridge.
`repository.py` owns only app-database records. `service.py` coordinates
approval and the Bridge client but never performs HOM calls. The Houdini-side
adapter and executor remain in `houdini_side/secure_bridge.py` or a focused
`houdini_side/changeset_executor.py` extracted from it; those modules lazy-load
`hou` only inside Houdini.

The existing `eee_agent.bridge` rpyc path, `eee_agent.tools`, read-only Runtime
tool allowlist, and `eee_agent.app.build_agent()` are not write paths and are
not modified by Task 16.

## 4. Strict contracts

All DTOs are frozen dataclasses with exact primitive types, exact field sets,
deep-frozen JSON, bounded strings/collections, finite numbers, duplicate-key
rejection, and canonical JSON matching the accepted Runtime/Bridge helpers.
The independently persisted top-level records `WorkspaceManifest`,
`ChangeSet`, `ApprovalRecord`, and `ChangeReceipt` have `schema_version=1` and
reject every other value. Nested value objects and the derived
`PolicyDecision` do not carry a redundant schema version.

### 4.1 WorkspaceManifest

```text
WorkspaceManifest
- schema_version: 1
- workspace_id: ws_<uuid>
- session_id: ses_<uuid>
- instance_id: str
- scene_epoch: int >= 1
- revision: sha256 hex
- roots: tuple[OwnedNodeRef, ...]             # 1..16
- nodes: tuple[OwnedNodeRef, ...]             # 1..4096
- created_by_run: run_<uuid>
- updated_at: UTC timestamp

OwnedNodeRef
- node_id: stable non-empty ID
- path: absolute Houdini node path
- node_type: exact Houdini type name
- parent_path: absolute Houdini node path
- capability: bounded identifier
- role: bounded identifier
```

The app DB owns the full manifest. Each owned Houdini node mirrors only:
`eee.workspace_id`, `eee.node_id`, `eee.capability`, `eee.role`,
`eee.schema_version`, and `eee.created_by_run`. Paths are locators, not
identity. Duplicate node IDs or paths are invalid. A manifest revision is the
canonical SHA-256 hash of its identity and ordered owned-node facts, excluding
`updated_at`.

Task 15 did not implement WorkspaceManifest even though the roadmap listed it
as a dependency. Task 16-A therefore delivers the contract and read-only
inspection before any write request is enabled.

### 4.2 Node references and supported operations

Every existing-node reference is exact:

```text
NodeRef
- node_id: str | null
- path: absolute node path
- expected_type: non-empty str
- expected_workspace_id: ws_<uuid> | null
```

`node_id` is mandatory for an owned node and null only for an explicitly
scoped external node. The first Task 16 executor supports exactly:

```text
CreateNode
- kind: "node.create"
- op_id: unique bounded str
- parent: NodeRef
- node_id: new stable non-empty ID
- node_type: non-empty str
- node_name: bounded Houdini-safe name
- workspace_id: ws_<uuid>
- capability: bounded identifier
- role: bounded identifier

SetParm
- kind: "parm.set"
- op_id: unique bounded str
- target: NodeRef
- parm_name: bounded non-empty str
- value: bounded JSON scalar or homogeneous scalar tuple
- expected_old_value: same value contract

ConnectInput
- kind: "wire.connect"
- op_id: unique bounded str
- target: NodeRef
- input_index: int >= 0
- source: NodeRef
- source_output_index: int >= 0
- expected_old_source: WireRef | null
```

Maximums are 256 operations per ChangeSet, 4096 affected/read-dependency node
references, 128 KiB canonical ChangeSet JSON, 16 KiB per parameter value, and
a 30-second Bridge deadline. Forward `node.delete`, user-data mutation outside
the six ownership keys, arbitrary expressions/source, multiparm structural
edits, file paths, and every unknown operation are hard rejected. Executor-
private deletion of a node created by the same failed transaction is allowed
only as a captured rollback action and is never accepted from a DTO.

### 4.3 Preconditions and postconditions

The exact tagged union is:

- `scene.binding_equals`: instance ID and scene epoch;
- `workspace.revision_equals`: workspace ID and manifest revision;
- `node.identity_equals`: NodeRef still resolves to the same path/type/owner;
- `parm.value_equals`: target, parameter name, and canonical value;
- `wire.input_equals`: target input and exact current `WireRef | null`; and
- `node.absent`: path and new node ID do not already exist.

Postconditions use `node.identity_equals`, `parm.value_equals`, and
`wire.input_equals`. The executor derives mandatory pre/postconditions from
every operation and rejects a ChangeSet that omits or contradicts them. Model
or compiler-provided summaries cannot replace derived checks.

### 4.4 ChangeSet

```text
ChangeSet
- schema_version: 1
- change_id: chg_<uuid>
- session_id: ses_<uuid>
- run_id: run_<uuid>
- scene_binding: accepted SceneBinding
- workspace_id: ws_<uuid> | null
- base_revision: sha256 hex
- required_permission: OwnedWorkspace | ScopedPatch | ProjectChange
- scoped_node_ids: tuple[str, ...]
- operations: tuple[TypedOperation, ...]
- affected_nodes: tuple[NodeRef, ...]
- read_dependencies: tuple[NodeRef, ...]
- preconditions: tuple[Precondition, ...]
- expected_postconditions: tuple[Postcondition, ...]
- risk_summary: RiskSummary
- checkpoint_plan: CheckpointPlan
- created_at: UTC timestamp
```

The canonical digest covers the full `schema_version=1` payload. Collection
order is significant and stable; duplicate operation IDs and duplicate
affected nodes are invalid. `base_revision` is evidence from targeted
preflight facts, not a permission by itself.

`RiskSummary` contains only bounded counts, effect names, affected paths, and
boolean flags (`touches_external_nodes`, `changes_wiring`,
`requires_backup`). `CheckpointPlan` names the exact nodes, parameters, and
wires to snapshot. The trusted executor constructs concrete inverse operations
from the re-read before snapshot; it never executes an inverse plan supplied
by a model.

### 4.5 PolicyDecision

Policy evaluates normalized effects, targets, ownership, scope, and operation
support. It returns an immutable decision with:

```text
PolicyDecision
- allowed: bool
- mode: OwnedWorkspace | ScopedPatch | ProjectChange
- normalized_effects: tuple[str, ...]
- approval_required: true
- backup_required: bool
- denial_codes: tuple[str, ...]
- changeset_digest: sha256 hex
```

Rules are exact:

- `OwnedWorkspace`: all created/target nodes must belong to the same manifest;
  external parents may be read dependencies but cannot be changed.
- `ScopedPatch`: every changed existing node must be in the explicit scoped
  node set; scope never expands upstream/downstream. `node.create` is denied.
- `ProjectChange`: every changed node/path and effect must be enumerated in the
  ChangeSet. It never grants a standing project permission.
- unsupported/unknown effects, locked assets, ambiguous ownership, stale
  manifest data, or a requested backup that is unavailable are denied.

All three modes require per-ChangeSet approval in Task 16. This conservative
rule can only be relaxed by a later versioned policy and UI decision.

### 4.6 ApprovalRecord

```text
ApprovalRecord
- schema_version: 1
- approval_id: apr_<uuid>
- change_id: chg_<uuid>
- changeset_digest: sha256 hex
- decision: Pending | Approved | Rejected | Consumed | Expired
- decided_by: "local_user" | null
- requested_at / decided_at / expires_at: UTC timestamps
- approved_instance_id: str | null
- approved_scene_epoch: int | null
```

Approval and rejection commands carry only `change_id` and the digest shown to
the user. Approval is invalid after expiry, scene-instance/epoch change,
ChangeSet replacement, prior consumption, or rejection. Applying consumes the
approval atomically with the Runtime transition to `Applying`; a retry uses the
persisted recovery record, not a second approval consumption.

### 4.7 ChangeReceipt and recovery states

```text
ChangeReceipt
- schema_version: 1
- change_id: chg_<uuid>
- status: Applied | AlreadyApplied | RolledBack | Partial | CriticalRecovery
- instance_id / scene_epoch
- before_revision / after_revision
- applied_op_ids: tuple[str, ...]
- postcondition_results: tuple[ConditionResult, ...]
- rollback_results: tuple[ConditionResult, ...]
- scene_may_have_changed: bool
- completed_at: UTC timestamp
```

`Applied` and `AlreadyApplied` require every expected postcondition to be true.
`RolledBack` requires every before condition to be restored. `Partial` and
`CriticalRecovery` can never be presented as success. Receipt JSON is bounded
and contains no raw traceback or arbitrary parameter/source dump.

## 5. Runtime persistence and state machine

Schema v2 adds `workspaces`, `changesets`, `approvals`, and `change_receipts`.
Foreign keys bind records to sessions/runs; canonical DTO JSON and digest are
stored together; `change_id` and `approval_id` are unique. The migration is
additive, transactional, checksum-protected, and leaves schema v1 data valid.

ChangeSet states are:

```text
Proposed -> AwaitingApproval -> Approved -> Applying -> Applied
                         |          |          |-> RolledBack
                         |          |          |-> CriticalRecovery
                         |          |-> Stale
                         |-> Rejected | Expired
```

Runtime writes the proposed ChangeSet and `changeset.proposed` event in one
transaction. Approval/rejection updates its record and appends the matching
event atomically. Apply persists `Applying` before Bridge I/O. A committed,
reconciled receipt and `changeset.applied`/`changeset.rolled_back` event share
one transaction. Broadcast remains post-commit.

On startup, recovery examines every `Applying` record:

1. query the Bridge by `change_id` and current binding;
2. if a terminal receipt exists, validate and persist it;
3. otherwise preflight all before and postconditions;
4. if all before facts hold, return to `Approved` without replaying a write;
5. if all postconditions hold, persist `AlreadyApplied`;
6. if neither state is provable, freeze writes and persist
   `CriticalRecovery`.

Automatic replay is deliberately excluded from startup recovery in Task 16.

## 6. Runtime protocol and events

The existing deferred commands become implemented only when their service
slices are accepted:

- `workspace.create`, `workspace.bind`, `workspace.switch`,
  `workspace.inspect`;
- `changeset.approve`, `changeset.reject`.

Payload validators enforce exact fields and IDs. No public command accepts raw
operation JSON or directly invokes apply. Proposal/apply remain trusted
service APIs until Task 18 provides the typed compiler and Task 17-B provides
the preview UI.

Durable event names are:

- `workspace.created`, `workspace.bound`, `workspace.updated`;
- `changeset.proposed`, `changeset.state_changed`;
- `approval.requested`, `approval.approved`, `approval.rejected`,
  `approval.expired`;
- `changeset.applied`, `changeset.rolled_back`;
- `recovery.critical`.

Events carry IDs, digest, states, bounded effect/risk summaries, and receipt
status. They never carry the full token, unrestricted DTO, before snapshot, or
raw exception.

## 7. Additive Bridge write capability

The accepted hello remains `eee.bridge/1`. Its success ack adds a sorted exact
`capabilities` list. Existing read-only clients may ignore the extra field.
The new client requires `changeset.v1` before it sends one of:

- `changeset.preflight`: returns targeted binding, manifest, node, parameter,
  and wire facts needed by policy/preconditions;
- `changeset.apply`: accepts the approved canonical ChangeSet plus digest and
  returns a reconciled ChangeReceipt; and
- `changeset.receipt`: queries recovery evidence by `change_id`.

`scene.query` behavior is unchanged. The server strictly dispatches each
operation to a typed parser. Write operations and reads share one bounded FIFO
main-thread operation queue so HOM access cannot interleave with a write.
Queue admission checks capability, token, size, deadline, scene epoch, and
write-freeze state before HOM execution.

The Bridge never trusts Runtime's `PolicyDecision` alone. It independently
validates operation allowlists, binding, ownership/scope facts, digest, and
preconditions. The approval record remains Runtime-owned; Bridge authority is
the narrow typed request plus its own current scene facts, not possession of a
general write credential.

## 8. Transaction algorithm

For one accepted apply request, the Houdini-side executor:

1. resolves NodeRefs by stable owned node ID first and path second; ambiguity
   fails closed;
2. verifies instance ID, scene epoch, manifest revision, permission mode,
   operation allowlist, and every derived precondition;
3. captures a bounded before snapshot and derives inverse operations;
4. enters `hou.undos.group("EEE Agent · <change_id>")`;
5. executes operations in declared order and mirrors ownership keys on created
   nodes;
6. re-reads every postcondition and computes `after_revision`;
7. returns `Applied` only when reconciliation succeeds;
8. on failure, runs inverse operations in reverse order and re-reads the before
   conditions; then returns `RolledBack`, `Partial`, or `CriticalRecovery`.

Cancellation before step 4 prevents all writes. Once step 4 starts, client
cancellation does not interrupt the short transaction; the executor completes
or rolls back, persists in-memory receipt evidence for the process lifetime,
and reconciliation remains queryable. Runtime stays `Stopping` until it has a
terminal receipt.

## 9. Error taxonomy

Task 16 adds bounded codes:

- `changeset.invalid`, `changeset.unsupported_operation`;
- `policy.denied`, `policy.scope_violation`, `policy.ownership_ambiguous`;
- `approval.required`, `approval.digest_mismatch`, `approval.expired`,
  `approval.already_consumed`;
- `changeset.stale`, `changeset.already_applied`;
- `changeset.apply_failed`, `changeset.rollback_failed`;
- `recovery.critical_required`;
- `bridge.capability_unavailable`, `bridge.write_frozen`.

Stale and permission failures assert `scene_may_have_changed=false` unless a
write began. Partial/rollback failures assert it true. Raw HOM errors are
converted to bounded technical detail references; they are not returned or
persisted verbatim.

## 10. Acceptance

Offline acceptance proves strict DTO parsing/canonical hashes, policy matrices,
manifest identity after rename/move, approval digest/expiry/single-use,
transactional migrations, event commit order, wrong-token/capability failures,
stale scene/revision/parm/wire/ownership rejection, duplicate apply,
successful reconciliation, rollback, partial failure, cancellation, shutdown,
and restart recovery. It also proves that Runtime imports do not import `hou`
or the legacy bridge and that the read-only suite remains unchanged.

The real Houdini smoke uses a temporary unsaved fixture and verifies create,
parameter set, connection, idempotent reapply, user-visible undo grouping,
forced postcondition failure with rollback, stale epoch rejection, restart
reconciliation, scene fingerprint expectations, and no file/HIP/HDA effect.

Task 16 is accepted only after focused suites, the full offline suite,
`uv lock --check`, compileall, diff check, secret/local-state scan, the hython
smoke, and a clean reviewed commit for every implementation slice.
