# Task 16-B2b Trusted Workspace Lifecycle Design

- Date: 2026-07-16
- Scope: Task 16-B2b only
- Status: User-approved design; implementation not started
- Branch: `feature/runtime`
- Baseline: `6a4fd09`; 69 locked packages; 1873 offline tests passed on
  the current machine

## 1. Goal

Task 16-B2b implements the persistent, read-only lifecycle for EEE-owned
Houdini workspaces. A workspace answers four questions without granting write
authority by itself:

1. Which exact EEE-created nodes belong to the logical workspace?
2. Which Session owns that workspace?
3. Which workspace is active for that Session?
4. Does the persisted manifest still match the live Houdini scene?

The slice activates the deferred Runtime commands `workspace.create`,
`workspace.bind`, `workspace.switch`, and `workspace.inspect`. It adds a narrow
Bridge inspection capability, a Workspace service, persistent active-workspace
state, atomic workspace events, strict optimistic concurrency, and offline
plus real-Houdini read-only acceptance.

The user should not need to understand workspace internals. Natural-language
intent remains primary; Houdini selection is contextual evidence, never write
authority.

## 2. Position in the product flow

B2b is infrastructure, not the natural-language modeling entry point.

```text
User intent
    -> Task 18 trusted compiler produces a typed ChangeSet
    -> Task 16-B2a obtains exact-digest approval
    -> Task 16-E applies and reconciles through the accepted Bridge executor
    -> B2b records/rebinds/switches/inspects the resulting workspace
```

The intended intent rules are:

- Explicit create intent does not require a Houdini selection. A later trusted
  compiler may propose an approved `ProjectChange` that creates the first
  EEE-owned nodes and their six identity mirrors. After a successful receipt,
  the workspace is registered from live facts.
- An incidental selection never overrides explicit create intent and never
  authorizes modification of the selected node.
- Explicit modification of an EEE-owned node uses its workspace.
- Explicit modification of a non-owned node may use a one-ChangeSet
  `ScopedPatch`; selection does not permanently adopt the node.
- Ambiguous intent must be clarified rather than inferred from selection.

B2b does not implement intent routing, the compiler, ChangeSet apply/recovery,
or the panel. It provides the durable workspace seam those later slices use.

## 3. Non-goals and hard boundaries

B2b does not add or expose:

- node creation, deletion, connection, parameter mutation, flag mutation, or
  user-data mutation;
- adoption of ordinary user-created nodes into permanent EEE ownership;
- arbitrary Python, HOM, HScript, VEX/source execution, `eval`, or `exec`;
- shell, filesystem, export, capture, HIP save/load/clear, or HDA effects;
- a generic LLM-facing write tool;
- ChangeSet apply, receipt persistence, startup recovery, or write freeze
  orchestration from Task 16-E;
- a modeling compiler, repair loop, visual review, or Task 17 panel;
- a second database or any machine-local state transfer;
- weakening of the accepted `scene.query`, ChangeSet, approval, preflight,
  single-FIFO, transactional executor, rollback, or receipt contracts.

The legacy unrestricted rpyc bridge remains outside the Runtime path.

## 4. Core invariants

1. **Text intent outranks selection.** Selection supplies target context only;
   it is never sufficient authorization for a write.
2. **Runtime never trusts client node facts.** Public commands carry IDs,
   expected revisions/active state, and an optional last-observed scene epoch. They never
   carry a WorkspaceManifest, node list, path list, ownership fields, or live
   scene result.
3. **Only already-owned nodes can be registered.** `workspace.create` accepts
   only live selected nodes that already carry all six accepted EEE identity
   mirrors. B2b never writes those mirrors.
4. **Selection scope is exact.** Selected nodes are the complete manifest
   `roots` and `nodes` set. Existing descendants are not included implicitly.
5. **Stable ID is identity; path is a locator.** Rename/move may refresh path
   and parent path during bind, but ID, type, workspace, schema, capability,
   role, and creating Run must remain exact.
6. **A bind proves the complete set.** Current selection must contain exactly
   the stored manifest node-ID set. Subsets, supersets, mixed workspaces,
   duplicates, and ambiguous identities fail closed.
7. **Active is persistent but not authority.** An active-workspace pointer is a
   Session preference. Every future write still requires accepted policy,
   exact approval, preflight, and executor revalidation.
8. **Switch is live-verified compare-and-set.** The Bridge must prove the target
   manifest against the current scene before the active pointer changes, and
   the caller's expected prior active ID must still match inside the commit.
9. **Inspection may report unavailable but cannot bless stale data.** Cached
   manifests may be displayed with `BridgeUnavailable`; only a live `Healthy`
   result is current evidence.
10. **No durable state precedes live validation.** Create, bind, and switch
    perform Bridge inspection first, then one app-database transaction. Any
    validation or commit failure leaves the previous durable state unchanged.
11. **Workspace mutation and its event are atomic.** A state/event transaction
    commits before notification. No event advertises an uncommitted state.
12. **Bridge inspection is read-only and FIFO-serialized.** It shares the
    accepted main-thread queue with scene queries and ChangeSet operations, so
    HOM reads cannot interleave with a short write transaction.

## 5. Ownership model

### 5.1 Required live mirrors

Every node admitted to a manifest must expose exact string values for:

```text
eee.workspace_id
eee.node_id
eee.capability
eee.role
eee.schema_version
eee.created_by_run
```

The current accepted schema value is `1`. `workspace_id` and
`created_by_run` must be valid repository IDs. `node_id`, `capability`, and
`role` must satisfy the existing bounded identifier contracts.

Houdini node user data is appropriate for these mirrors because SideFX
explicitly supports per-node user data for tool tagging. The mirrors are
evidence, not independent authorization: Runtime must compare them with the
manifest and the Bridge must re-read them before a write.

### 5.2 Who receives EEE identity

In the accepted Task 16 scope, only a node created by the trusted transactional
executor through a typed, approved `CreateNode` receives EEE identity. User
nodes, third-party assets, imported legacy scenes, and merely selected nodes
remain non-owned.

Copying an EEE node may copy user data and create a duplicate stable ID. Such
a copy is an identity conflict, not a second owned node. Create/bind/switch
must reject the ambiguity until a later explicitly designed repair or adoption
workflow resolves it.

### 5.3 Roots and descendants

For public `workspace.create`, every explicitly selected node becomes one
manifest root and one manifest node. No descendants, ancestors, inputs,
outputs, or same-network neighbors are added implicitly. This rule prevents a
single selection from silently expanding durable write scope.

Future automatic registration after Task 16-E may construct a richer manifest
from the exact applied receipt and live inspection, but it must use the same
identity validation and may not be implemented by this public B2b command.

## 6. Architecture and file boundaries

The intended dependency direction is:

```text
RuntimeWebSocketServer
    -> RuntimeService
        -> WorkspaceService
            -> ChangeSetRepository / EventStore / app.sqlite
            -> WorkspaceFactProvider protocol
                -> BridgeClient
                    -> eee.bridge/1 workspace.inspect
                        -> one main-thread FIFO
                            -> HoudiniWorkspaceInspector (read-only)
```

The implementation plan must keep responsibilities focused:

- `eee_agent/changesets/workspace_service.py`: lifecycle orchestration,
  validation, status summaries, optimistic concurrency inputs, and no SQL or
  transport details.
- `eee_agent/changesets/repository.py`: strict manifest persistence plus new
  atomic workspace/event and active-pointer primitives.
- `eee_agent/houdini_bridge/workspaces.py`: frozen Bridge DTOs and strict JSON
  parsers for the additive capability.
- `eee_agent/houdini_bridge/client.py`: one typed inspection method, gated by
  advertised capability.
- `houdini_side/workspace_inspector.py`: lazy-HOM, read-only live fact reader.
- `houdini_side/secure_bridge.py`: strict operation dispatch through the
  accepted queue; no duplicated inspection policy.
- Runtime migration/protocol/service/server files: additive persistence and
  command wiring only.

The implementation plan may adjust exact file names after verifying local
patterns, but it must not fold workspace inspection into the already large
ChangeSet executor or expose raw Bridge responses to WebSocket clients.

## 7. Additive Bridge capability

### 7.1 Capability and operation

The accepted hello remains `eee.bridge/1`. A new sorted capability is
advertised:

```text
workspace.v1
```

The only new operation is:

```text
workspace.inspect
```

Old Bridge processes that do not advertise `workspace.v1` fail closed with
`bridge.capability_unavailable`. There is no fallback to `scene.query`, raw
rpyc, or generated Python because the accepted `scene.query` result omits the
full identity mirrors required by B2b.

### 7.2 Request

The strict Bridge request retains the accepted envelope fields:

```text
protocol: "eee.bridge/1"
kind: "request"
request_id: bounded request ID
operation: "workspace.inspect"
deadline_ms: integer in 1..30000
scene_epoch: integer >= 1 | null
payload:
  mode: "selection" | "manifest"
  manifest: WorkspaceManifest | null
```

Rules:

- `selection` requires `manifest=null` and reads only current explicit
  selection.
- `manifest` requires an exact manifest supplied by the trusted Runtime
  service and resolves its complete node set by stable identity first and path
  second.
- A non-null `scene_epoch` must match the live epoch. Null is allowed only for
  this read-only operation and means "discover the current binding"; it never
  authorizes a write. This avoids a bootstrap loop after Bridge restart or HIP
  load where a client would otherwise need to know the new epoch before it
  could read the new epoch.
- Unknown fields, duplicate JSON keys, non-exact primitive types, wrong schema,
  oversized payloads, stale scene epoch, and inconsistent mode/manifest pairs
  are rejected before HOM work.
- The DTO uses existing canonical JSON, SceneBinding, WorkspaceManifest, and
  ID validation helpers rather than a permissive compatibility parser.

### 7.3 Result

The frozen result contains:

```text
binding: SceneBinding
mode: "selection" | "manifest"
nodes: tuple[WorkspaceNodeObservation, ...]
observed_revision: canonical SHA-256 of binding + ordered observations
scene_may_have_changed: false
```

Each `WorkspaceNodeObservation` contains only bounded facts:

```text
path
node_type
parent_path
is_locked
workspace_id | null
node_id | null
capability | null
role | null
schema_version | null
created_by_run | null
```

Results are deterministically ordered by `(node_id-or-empty, path)`, deeply
immutable, and bounded by the accepted manifest node count and response-size
limits. Missing fields remain explicit nulls so Runtime can distinguish an
unowned selection from malformed or partial identity.

Duplicate stable IDs, multiple live nodes resolving one manifest identity, or
an unbounded/invalid user-data value return a structured identity conflict;
they are not silently reduced to the first match.

### 7.4 Read-only proof

The inspector may call only bounded reads equivalent to:

- current SceneBinding;
- selected node enumeration;
- scene node enumeration required for exact manifest identity resolution;
- node path/type/parent/lock state;
- the six named user-data keys.

It must not call node creation/destruction, parameter setters, input setters,
flag setters, user-data setters, undo mutation, HIP operations, filesystem
operations, source execution, or generic method dispatch.

## 8. Workspace service seam

`WorkspaceService` depends on a small async `WorkspaceFactProvider` protocol,
not on a concrete Bridge class. The provider exposes exact methods for
selection inspection and manifest inspection. Offline tests inject deterministic
providers; production Runtime injects a Bridge-backed provider. Absence of the
provider fails closed for create, bind, and switch.

The service uses the existing repository and EventStore. It does not import
`hou`, `rpyc`, the legacy bridge, WebSocket objects, or the agent graph.

## 9. Runtime command contracts

Public Runtime commands remain exact five-field `eee.runtime/1` envelopes. No
command accepts a manifest, node list, path list, ownership field, capability,
role, or arbitrary JSON metadata.

### 9.1 `workspace.create`

Payload:

```text
session_id: ses_<uuid>
expected_scene_epoch: int >= 1 | null
```

Behavior:

1. Verify the Session exists.
2. Inspect current selection through the provider, enforcing a non-null
   expected epoch or discovering and returning the current binding when null.
3. Reject an empty selection.
4. Require complete, supported identity on every selected node.
5. Require one common workspace ID and one common creating Run.
6. Require the creating Run to exist and belong to the Session.
7. Reject locked nodes for initial registration because the command promises a
   usable owned workspace, while retaining lock checks again at write time.
8. Reject duplicate IDs/paths, mixed workspaces, malformed identity, and any
   selection that cannot produce an exact WorkspaceManifest.
9. Build the manifest with selected nodes as both `roots` and `nodes`, using
   live paths/types/parents and the observed binding.
10. In one transaction, insert the manifest, make it active for the Session,
    and append `workspace.created`.
11. Notify listeners only after commit.

Idempotency:

- If the exact workspace, manifest revision, Session, and active state already
  exist, return the existing summary without a new event.
- If the workspace ID exists with any contradictory persisted fact, return
  `workspace.identity_conflict`; never overwrite it.

### 9.2 `workspace.bind`

Payload:

```text
session_id: ses_<uuid>
workspace_id: ws_<uuid>
expected_manifest_revision: sha256 hex
expected_scene_epoch: int >= 1 | null
```

Behavior:

1. Load the stored manifest and require it to belong to the Session.
2. Require its current revision to equal `expected_manifest_revision`.
3. Inspect current selection, enforcing a non-null expected epoch or
   discovering the current binding when null.
4. Require the selected node-ID set to equal the stored set exactly.
5. Require exact type, workspace, schema, capability, role, and creating-Run
   identity for every node.
6. Permit path, parent path, instance ID, and scene epoch to change.
7. Build a refreshed manifest and revision from the live facts.
8. In one transaction, compare the old revision again, update the manifest,
   and append `workspace.bound`.
9. Notify only after commit.

A bind does not change the active pointer unless the workspace is already
active. A no-op exact rebind returns the current result without a duplicate
event.

Lock state is reported but is not identity. A previously registered workspace
may be rebound while locked for inspection; any later write remains subject to
the accepted lock denial in policy/preflight.

### 9.3 `workspace.switch`

Payload:

```text
session_id: ses_<uuid>
workspace_id: ws_<uuid>
expected_active_workspace_id: ws_<uuid> | null
expected_scene_epoch: int >= 1 | null
```

Behavior:

1. Require the target manifest to belong to the Session.
2. Inspect the complete manifest through the provider, independent of current
   UI selection.
3. Require exact binding, full identity set, paths/types/parents, and manifest
   revision. A renamed/moved manifest must be rebound before switching.
4. In one transaction, require the stored active pointer to equal the caller's
   expected value, set the target active, and append `workspace.updated`.
5. Notify only after commit.

If the target is already active and remains live-healthy, return a no-op result
without another event. A failed switch preserves the prior active pointer.

### 9.4 `workspace.inspect`

Payload:

```text
session_id: ses_<uuid>
workspace_id: ws_<uuid> | null
expected_scene_epoch: int >= 1 | null
```

`workspace_id=null` selects the Session's active workspace. The response
contains bounded summaries for Session workspaces, the active workspace ID,
the target manifest, and one status:

```text
Healthy
Stale
Conflict
BridgeUnavailable
```

- `Healthy`: live binding and all manifest identity facts match.
- `Stale`: the scene binding, path/parent/type, set, or revision no longer
  matches and an explicit bind/refresh decision is required.
- `Conflict`: duplicate/ambiguous identity, mixed ownership, malformed live
  mirrors, or another fact prevents safe resolution.
- `BridgeUnavailable`: cached persistence may be displayed, but no live claim
  is made and the result cannot authorize switching or writing.

Inspect never mutates a manifest, active pointer, or event stream. It returns
`BridgeUnavailable` only for connectivity/capability absence; protocol or
identity corruption remains a structured error rather than being hidden as
ordinary unavailability.

## 10. Persistence

### 10.1 Additive migration

The existing app database is the only store. B2b adds one checksum-protected,
transactional migration after accepted schema v2. The preferred relation is:

```text
session_workspace_state
- session_id TEXT PRIMARY KEY
- active_workspace_id TEXT NOT NULL
- state_revision INTEGER NOT NULL CHECK(state_revision >= 1)
- updated_at TEXT NOT NULL
- FOREIGN KEY(session_id) REFERENCES sessions(session_id) ON DELETE CASCADE
- FOREIGN KEY(active_workspace_id, session_id)
    REFERENCES workspaces(workspace_id, session_id)
```

The migration also adds the parent-side unique key required by SQLite for
`workspaces(workspace_id, session_id)` without changing existing rows. No row
means the Session has no active workspace. The composite foreign key makes
cross-Session activation structurally impossible even if a future service
caller is defective.

`state_revision` supports compare-and-set semantics without depending on wall
clock ordering. Existing schema-v1/v2 checksums and rows remain unchanged.

### 10.2 Repository primitives

The repository adds focused atomic methods rather than exposing raw SQL:

- create manifest + initial active pointer + event;
- compare-and-update manifest + event;
- compare-and-switch active pointer + event;
- get active pointer;
- list Session manifests and active summary.

Every method validates exact DTO types before its first await, rechecks foreign
keys and expected revisions inside the write transaction, and verifies stored
payload digest/canonical JSON on read. Existing simple insert/get/list methods
retain their accepted behavior.

### 10.3 Event atomicity

The repository reuses EventStore's accepted caller-owned transaction append.
The committed events are:

- `workspace.created`;
- `workspace.bound`;
- `workspace.updated`.

Payloads contain only bounded IDs, revision, node count, binding epoch, and
active status. Full manifests, ownership mirrors, HIP contents, tokens, raw
exceptions, and arbitrary user data are excluded.

Broadcast occurs through the existing Runtime post-commit notification path.

## 11. Concurrency and idempotency

- App-database writes retain the accepted serialized transaction boundary.
- Create is idempotent only for an exact existing manifest and active state.
- Bind compares `expected_manifest_revision` both before Bridge I/O for fast
  failure and again inside commit to prevent lost update.
- Switch compares `expected_active_workspace_id` before inspection and again
  inside commit. Another client's switch wins only by committing first; the
  stale caller must refresh.
- Repeating an already-satisfied bind/switch returns a no-op summary and emits
  no duplicate durable event.
- Bridge inspection is an observation, not a lock on the scene. Future writes
  still execute accepted preflight immediately before mutation. A scene change
  after B2b inspection cannot turn active state into write authority.
- Request cancellation before repository commit produces no durable state.
  Cancellation during a database transaction follows the accepted
  cancellation-deferred persistence pattern so state and event cannot split.
- Client disconnect does not cancel a commit that already entered its protected
  persistence region; notification remains post-commit and reconnect replay
  observes the durable event.

## 12. Error taxonomy

B2b adds bounded, actionable errors:

- `workspace.not_found`;
- `workspace.no_selection`;
- `workspace.unowned_selection`;
- `workspace.mixed_selection`;
- `workspace.incomplete_selection`;
- `workspace.identity_conflict`;
- `workspace.session_mismatch`;
- `workspace.revision_conflict`;
- `workspace.active_conflict`;
- `workspace.locked_selection`;
- existing `bridge.capability_unavailable`;
- existing `bridge.stale_scene` and structured transport/deadline errors.

Errors expose no raw traceback, token, arbitrary user-data value, or unbounded
path set. Create/bind/switch errors make no durable change. Inspect may return
the explicit `BridgeUnavailable` status for ordinary connectivity absence, but
corrupt persistence or malformed Bridge data remains an error.

## 13. Security analysis

### 13.1 Prevented confused-deputy path

A malicious Runtime client cannot upload a manifest that names a camera,
light, HDA, or other user's node and ask Runtime to bless it. The public
protocol does not accept node facts. Runtime obtains observations from the
authenticated loopback Bridge, validates exact ownership mirrors, and records
only the observed set.

### 13.2 Workspace is not permission

Possessing a workspace ID, selecting an owned node, or setting a workspace
active grants no direct effect. Task 16 policy, exact digest approval, Bridge
preflight, derived preconditions, FIFO executor, reconciliation, and receipt
remain mandatory for writes.

### 13.3 No implicit adoption

Ordinary nodes cannot receive EEE identity in B2b. A future import/adoption
feature would be a separate typed write effect with preview, approval,
transaction, rollback, receipt, and recovery. It cannot be hidden inside
selection or workspace creation.

### 13.4 No compatibility downgrade

An old Bridge or malformed response is not handled by using `scene.query`,
legacy rpyc, generated code, or client-supplied fields. The capability is
unavailable until the matching strict Bridge is running.

## 14. Acceptance design

### 14.1 Protocol tests

Prove exact payloads for all four commands; null handling; IDs, digests, and
epoch bounds; duplicate-key rejection; unknown/missing fields; strict primitive
types; message-size limits; and the continued behavior of every accepted
Runtime command. Prove that manifest/node/path uploads are impossible.

### 14.2 Repository and migration tests

Prove additive migration/checksums, v1/v2 preservation, foreign keys,
cross-Session denial, active-pointer restart recovery, payload digest checks,
atomic state/event commits, rollback on event failure, create/bind/switch
idempotency, manifest revision conflicts, active compare-and-set conflicts,
and cancellation safety.

### 14.3 Workspace service tests

With deterministic fake providers, prove empty selection, unowned/partial
identity, mixed workspace IDs, invalid schema, Run/Session mismatch, duplicate
IDs/paths, locked initial selection, exact create, exact idempotent create,
contradictory reuse, exact-set bind, subset/superset rejection, legal
rename/move refresh, type/role/capability/Run conflict, live switch, stale
switch preservation, concurrent switch, no-op switch, and all four inspect
statuses.

### 14.4 Bridge contract/inspector/transport tests

Prove capability negotiation, strict DTO round-trip, old-server failure,
selection and manifest modes, all six mirrors, stable-ID-first resolution,
duplicate identity rejection, stale epoch, bounds, timeout/cancellation, token
auth, FIFO ordering with scene query and ChangeSet requests, immutable results,
and no scene mutation in fake-Houdini fingerprints.

### 14.5 Runtime server/process tests

Prove strict WebSocket routing, committed-event notification, replay after
disconnect/restart, Session isolation, structured errors, no half-state after
provider/commit failure, and active state in snapshots or the approved
workspace inspection response without changing existing snapshot compatibility.

### 14.6 Security regression

Statically and dynamically prove no new arbitrary code, shell, filesystem,
HIP/HDA/export, node/parm/wire/userData mutation, forward delete, unrestricted
bridge, LLM write tool, or automatic adoption path. Scan changed fixtures and
documents for tokens and machine-local state.

### 14.7 Real Houdini 21.0.440 smoke

Use the detected local Houdini install. In a disposable unsaved root:

1. Create marked fixture nodes through the already accepted typed executor or
   an isolated test-fixture setup outside the B2b operation under test.
2. Select the exact marked set.
3. Exercise Bridge selection and manifest inspection.
4. Exercise Runtime create, bind after rename/move, switch, and inspect.
5. Verify ordinary unmarked and duplicate-ID selections fail closed.
6. Compare node, parameter, input, flag, and user-data fingerprints before and
   after every B2b operation to prove B2b itself made no Houdini mutation.
7. Clean the disposable root without saving the HIP.

### 14.8 Final gate

The implementation gate includes focused B2b tests, the complete offline
suite, `uv lock --check`, compileall for `eee_agent`, `houdini_side`, and
tests, `git diff --check`, changed-file scope review, secret/local-state scan,
the real Houdini smoke, and a clean focused commit. It may introduce no new
skip or xfail.

## 15. Delivery and sequencing

B2b receives its own executable plan, implementation prompt, authorized-file
list, review checklist, implementation commit, independent Codex review, and
acceptance evidence. It must not be implemented from a D/D1 or 16-E prompt.

After B2b acceptance, Task 16-E may be designed as a separate bounded slice to
connect approved ChangeSets to the accepted executor and startup recovery.
Task 17 and Task 18 remain separate. No merge to `main`, force-push, or
accepted-history rewrite is authorized by this design.

## 16. Decision summary

Task 16-B2b creates a trustworthy ledger and live inspection path for EEE-owned
Houdini nodes. It makes workspace state durable and understandable while
remaining read-only with respect to Houdini. It deliberately does less than a
full modeling agent so the already accepted typed ChangeSet and transaction
boundaries remain the only path to scene mutation.
