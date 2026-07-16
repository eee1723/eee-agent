# Task 16-B2b Trusted Workspace Lifecycle Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Activate a fail-closed, read-only-with-respect-to-Houdini lifecycle for creating, rebinding, switching, and inspecting durable EEE-owned workspaces.

**Architecture:** Add a strict `workspace.v1` Bridge inspection operation over the accepted authenticated main-thread FIFO; place business validation in an async `WorkspaceService`; extend the checksum-protected Runtime database with a per-Session active pointer; expose four exact Runtime commands only after live inspection and atomic repository/event operations are complete. Selection supplies context, never authority, and only nodes carrying all six EEE ownership mirrors can enter a manifest.

**Tech Stack:** Python 3.11, frozen/slotted dataclasses, asyncio, authenticated loopback TCP/WebSocket protocols, Houdini 21.0.440 HOM reads, aiosqlite/SQLite migrations, pytest, uv.

---

## Accepted base, ownership, and stop line

- Branch/worktree: `feature/runtime` in `E:\eee-agent\.worktrees\runtime`.
- Accepted implementation base: `6a4fd09`.
- Accepted B2b design commit: `e135088`.
- Design authority: `docs/superpowers/specs/2026-07-16-task16-b2b-workspace-lifecycle-design.md`.
- Codex owns the plan, prompts, review checklist, independent review, final
  verification, and documentation commits.
- The implementation worker owns only RED/GREEN code and focused test commits
  within each slice's authorized file list.
- Stop after B2b acceptance. Do not start Task 16-E, Task 17, Task 18, UI work,
  push, merge, rebase, amend accepted history, or delete worktrees.

The implementation is deliberately split into three commits. Complete and
independently review each commit before authorizing the next:

1. B2b-1: strict Bridge contracts and read-only Houdini inspection;
2. B2b-2: migration, atomic repository operations, and `WorkspaceService`;
3. B2b-3: Runtime protocol/server/service wiring and process acceptance.

## Shared invariants every task must preserve

- Explicit user intent outranks incidental selection.
- `workspace.create` does not create or mark Houdini nodes. It registers only
  the exact currently selected nodes that already carry all six EEE mirrors:
  `eee.workspace_id`, `eee.node_id`, `eee.capability`, `eee.role`,
  `eee.schema_version`, and `eee.created_by_run`.
- Only nodes produced by the trusted EEE ChangeSet executor receive those
  mirrors. Ordinary nodes are never silently adopted.
- Create uses selected nodes as both `roots` and `nodes`; it does not walk
  descendants, inputs, outputs, siblings, or network neighbors.
- Bind requires the exact complete stable-node-ID set. It may refresh path,
  parent path, Bridge instance, and scene epoch, but no ownership identity.
- Switch ignores UI selection, inspects the stored complete manifest, and
  compare-and-sets the active pointer only after a live healthy result.
- Inspect is read-only and may return cached data with `BridgeUnavailable`;
  create, bind, and switch fail closed when live facts are unavailable.
- Workspace state is context, not write authority. Task 16 policy, approval,
  preflight, and transactional Apply remain the only write path.
- All HOM calls run through the one accepted bounded main-thread FIFO. No new
  worker, queue, generic method dispatch, arbitrary Python, shell, file, HDA,
  HIP, node, parameter, wire, flag, or user-data mutation path is allowed.
- Every persistence mutation and its durable event commit atomically;
  callbacks run only after commit.

## B2b-1 authorized implementation files

Create:

- `eee_agent/houdini_bridge/workspaces.py`
- `eee_agent/houdini_bridge/workspace_provider.py`
- `houdini_side/workspace_inspector.py`
- `tests/runtime/test_workspace_bridge_contracts.py`
- `tests/runtime/test_workspace_bridge_inspector.py`

Modify:

- `eee_agent/houdini_bridge/client.py`
- `eee_agent/houdini_bridge/__init__.py`
- `houdini_side/secure_bridge.py`
- `tests/runtime/test_houdini_bridge_client.py`
- `tests/runtime/test_houdini_bridge_queue.py`
- `tests/runtime/test_houdini_bridge_transport.py`

No Runtime database/protocol/server/service, ChangeSet repository/service,
existing executor, dependency, CLI, agent, UI, or documentation file is
authorized in the B2b-1 implementation commit.

### Task 1: Define strict `workspace.v1` Bridge DTOs

**Files:**

- Create: `eee_agent/houdini_bridge/workspaces.py`
- Modify: `eee_agent/houdini_bridge/__init__.py`
- Create: `tests/runtime/test_workspace_bridge_contracts.py`

- [ ] Write RED tests for frozen/slotted request, observation, result, and
  response DTOs; exact round trips; deep immutability; deterministic ordering;
  duplicate JSON keys; wrong protocol/kind/operation; unknown/missing fields;
  wrong primitive types; bounds; duplicate IDs/paths; invalid mode/manifest
  combinations; malformed mirrors; revision mismatch; and the 1 MiB limit.

- [ ] Define these exact public constants and DTOs:

```python
WORKSPACE_V1 = "workspace.v1"
WORKSPACE_INSPECT_OPERATION = "workspace.inspect"

class WorkspaceInspectionUnavailable(Exception):
    """Ordinary missing/closed/incompatible Bridge, safe to show as offline."""

class WorkspaceInspectionConflict(Exception):
    """Bounded live identity ambiguity, safe to show as Conflict on inspect."""

@dataclass(frozen=True, slots=True)
class WorkspaceNodeObservation:
    path: str
    node_type: str
    parent_path: str
    is_locked: bool
    workspace_id: str | None
    node_id: str | None
    capability: str | None
    role: str | None
    schema_version: int | None
    created_by_run: str | None

@dataclass(frozen=True, slots=True)
class WorkspaceInspectRequest:
    request_id: str
    deadline_ms: int
    scene_epoch: int | None
    mode: str                       # exact "selection" | "manifest"
    manifest: WorkspaceManifest | None

@dataclass(frozen=True, slots=True)
class WorkspaceInspectResult:
    binding: SceneBinding
    mode: str
    observations: tuple[WorkspaceNodeObservation, ...]
    observed_revision: str
    scene_may_have_changed: bool = False

@dataclass(frozen=True, slots=True)
class WorkspaceInspectResponse:
    request_id: str
    result: WorkspaceInspectResult | None
    error: BridgeError | None
```

Construction rules:

- `selection` requires `manifest is None`; `manifest` requires an exact
  `WorkspaceManifest`.
- `scene_epoch` is exact `int >= 1` or `None`; bool is rejected.
- Observations are sorted by `(node_id is None, node_id or "", path)` before
  hashing and must already arrive in that canonical order when parsing.
- A non-null `node_id` or `workspace_id` is validated with the accepted ID /
  identifier helpers; a partial mirror remains representable so the service
  can return an actionable ownership error.
- Duplicate stable IDs or paths are a contract conflict, never last-write-wins.
- `observed_revision` is lowercase SHA-256 over canonical JSON containing the
  binding, mode, and ordered observations.
- Successful results require `scene_may_have_changed is False`; error and
  result are mutually exclusive.
- Reuse `MAX_MESSAGE_BYTES`, `PROTOCOL`, `BridgeError`, `SceneBinding`,
  canonical JSON, and the repository's strict `WorkspaceManifest` decoder
  pattern. Do not accept arbitrary payload dictionaries after construction.

- [ ] Implement exact parsers/serializers:

```python
parse_workspace_inspect_request(raw: str | bytes) -> WorkspaceInspectRequest
parse_workspace_inspect_response(raw: str | bytes) -> WorkspaceInspectResponse
WorkspaceInspectRequest.to_json() -> str
WorkspaceInspectResponse.to_json() -> str
```

- [ ] Add lazy exports to `eee_agent.houdini_bridge.__init__` because the new
  module imports `WorkspaceManifest`, which already depends on Bridge
  `SceneBinding`. Prove importing `eee_agent.houdini_bridge` does not create a
  cycle or import `hou`.

- [ ] Run the focused contract gate and record the initial RED and final GREEN:

```powershell
uv run --extra eval pytest tests/runtime/test_workspace_bridge_contracts.py -q
```

Expected GREEN: all tests in that file pass with no skip/xfail.

### Task 2: Implement the bounded, stable-ID-first Houdini inspector

**Files:**

- Create: `houdini_side/workspace_inspector.py`
- Create: `tests/runtime/test_workspace_bridge_inspector.py`

- [ ] Write RED fake-Houdini tests for selection and manifest modes, complete
  and partial mirrors, empty selection, exact selection only, no descendant
  adoption, rename/move, lock observation, missing nodes, duplicate stable IDs,
  duplicate paths, stale epoch, deterministic ordering/revision, scan bounds,
  unsupported node facts, and zero writes.

- [ ] Implement a lazy-HOM inspector with a narrow constructor:

```python
class WorkspaceInspector:
    def __init__(
        self,
        hou: object,
        *,
        binding_provider: Callable[[], SceneBinding],
        max_scan_nodes: int = 4096,
    ) -> None: ...

    def inspect(self, request: WorkspaceInspectRequest) -> WorkspaceInspectResult: ...
```

The `selection` path reads exactly `hou.selectedNodes()`. The `manifest` path
resolves every stored stable ID independently of current selection. Both paths
perform one bounded scene scan sufficient to detect a duplicate copy of any
in-scope stable ID; path is corroborating evidence, never identity. If more
than `max_scan_nodes` would be inspected, return a bounded structured error.

- [ ] Read only these facts on the main thread: node path/type/parent, hard or
  soft lock, and the six exact user-data keys. Normalize absent keys to `None`.
  Reject HOM proxies, arbitrary user-data dumps, unbounded strings, ambiguous
  IDs, or a scene-epoch mismatch. Return `scene_may_have_changed=False`.

- [ ] Add a mutation-spy fingerprint covering create/destroy, set/setInput,
  setUserData, flags, undo, save/load/clear, file/HDA/export, shell, and code
  installation. Assert every B2b inspection leaves the fingerprint and every
  mutation counter unchanged.

- [ ] Run:

```powershell
uv run --extra eval pytest tests/runtime/test_workspace_bridge_inspector.py -q
```

Expected GREEN: all inspector tests pass with no mutation call and no skip.

### Task 3: Route inspection through the accepted Bridge client and FIFO

**Files:**

- Modify: `eee_agent/houdini_bridge/client.py`
- Create: `eee_agent/houdini_bridge/workspace_provider.py`
- Modify: `houdini_side/secure_bridge.py`
- Modify: `tests/runtime/test_houdini_bridge_client.py`
- Modify: `tests/runtime/test_houdini_bridge_queue.py`
- Modify: `tests/runtime/test_houdini_bridge_transport.py`

- [ ] Write RED tests proving capability advertisement is exact, sorted and
  unique; legacy servers fail before sending an inspect frame; malformed
  capability lists still fail closed; request IDs are checked; structured
  errors remain bounded; token auth applies; and old `scene.query` plus all
  accepted `changeset.v1` operations remain compatible.

- [ ] Add this typed client method, mirroring `preflight()` parsing/error rules:

```python
async def inspect_workspace(
    self, request: WorkspaceInspectRequest
) -> WorkspaceInspectResult:
    if WORKSPACE_V1 not in self._capabilities:
        raise BridgeClientError(
            code="bridge.capability_unavailable",
            category="capability",
            message_for_user="The bridge does not support workspace inspection.",
            retryable=False,
        )
    ...
```

Capability absence sends no frame. A malformed response aborts the connection;
a valid server error keeps it usable, matching the accepted client behavior.

- [ ] In `secure_bridge.py`, add lazy `_make_workspace_inspector(adapter)`,
  advertise `(CHANGESET_V1, WORKSPACE_V1)` by default, dispatch only the exact
  `workspace.inspect` operation, parse before queue admission, and invoke the
  inspector through the same `_run_on_queue` used by scene queries and
  ChangeSet operations. There is no second queue and no direct HOM call in a
  socket handler.

- [ ] Implement a production provider that opens a fresh discovered Bridge
  connection per fact read so a Houdini restart/port change is naturally
  rediscovered:

```python
class BridgeWorkspaceFactProvider:
    def __init__(self, state_dir: Path | str, *, deadline_ms: int = 5000) -> None: ...

    async def inspect_selection(
        self, expected_scene_epoch: int | None
    ) -> WorkspaceInspectResult: ...

    async def inspect_manifest(
        self,
        manifest: WorkspaceManifest,
        expected_scene_epoch: int | None,
    ) -> WorkspaceInspectResult: ...
```

It calls `BridgeClient.from_state_dir(state_dir)`, enters the async context,
sends one typed request, and closes. Missing discovery/token, connection
refusal, timeout, shutdown, and capability absence are normalized to the
shared bounded `WorkspaceInspectionUnavailable`. Exact live identity ambiguity
is normalized to `WorkspaceInspectionConflict`; malformed authentication,
malformed wire data, or protocol corruption remain hard structured errors and
are not mislabeled as ordinary unavailability.

- [ ] Add mixed concurrent operations to queue/transport tests and prove one
  FIFO order across `scene.query`, `workspace.inspect`, preflight, Apply, and
  receipt; preserve capacity, deadline, queued/running cancellation, and
  shutdown semantics.

- [ ] Run B2b-1 gates:

```powershell
uv run --extra eval pytest tests/runtime/test_workspace_bridge_contracts.py tests/runtime/test_workspace_bridge_inspector.py tests/runtime/test_houdini_bridge_client.py tests/runtime/test_houdini_bridge_queue.py tests/runtime/test_houdini_bridge_transport.py tests/runtime/test_changeset_bridge_contracts.py tests/runtime/test_changeset_bridge_preflight.py tests/runtime/test_changeset_bridge_transport.py -q
uv run python -m compileall -q eee_agent houdini_side tests
git diff --check
```

- [ ] Stage only B2b-1 authorized implementation files and commit once:

```text
feat: inspect trusted houdini workspaces
```

- [ ] Stop for Codex review against the B2b-1 section of the independent
  checklist. Do not begin the migration until that commit is accepted.

## B2b-2 authorized implementation files

Create:

- `eee_agent/changesets/workspace_service.py`
- `tests/runtime/test_workspace_service.py`

Modify:

- `eee_agent/runtime/migrations.py`
- `eee_agent/changesets/repository.py`
- `eee_agent/changesets/__init__.py`
- `tests/runtime/test_database.py`
- `tests/runtime/test_changeset_repository.py`

No Bridge transport/inspector/client, Runtime protocol/server/CLI, executor,
agent, UI, dependency, or documentation file is authorized in B2b-2.

### Task 4: Add checksum-protected schema v3 and atomic workspace primitives

**Files:**

- Modify: `eee_agent/runtime/migrations.py`
- Modify: `eee_agent/changesets/repository.py`
- Modify: `tests/runtime/test_database.py`
- Modify: `tests/runtime/test_changeset_repository.py`

- [ ] Write RED migration tests proving v1/v2 checksum preservation, ordered
  v3 application, rollback of a partially failing v3, reopen idempotency,
  foreign keys, cascade behavior, and structural cross-Session denial.

- [ ] Add exactly one migration and do not edit the accepted v1/v2 strings:

```sql
CREATE UNIQUE INDEX workspaces_identity_by_session
    ON workspaces(workspace_id, session_id);

CREATE TABLE session_workspace_state (
    session_id TEXT PRIMARY KEY
        REFERENCES sessions(session_id) ON DELETE CASCADE,
    active_workspace_id TEXT NOT NULL,
    state_revision INTEGER NOT NULL CHECK (state_revision >= 1),
    updated_at TEXT NOT NULL,
    FOREIGN KEY(active_workspace_id, session_id)
        REFERENCES workspaces(workspace_id, session_id)
);
```

Set `SCHEMA_VERSION = 3` and append `(3, MIGRATION_V3_SQL)` only.

- [ ] Write RED repository tests for create+activate+event atomicity,
  idempotent exact create, contradictory reuse, initial active conflict,
  exact read/list ordering, stored-payload digest tampering, bind compare/update,
  bind no-op, switch CAS, switch no-op, concurrent bind/switch winners, event
  rollback, cancellation safety, restart recovery, cross-Session denial, and
  Session deletion cascade.

- [ ] Add frozen/slotted repository results so committed events are explicit:

```python
@dataclass(frozen=True, slots=True)
class WorkspaceStateRecord:
    session_id: str
    active_workspace_id: str
    state_revision: int
    updated_at: datetime

@dataclass(frozen=True, slots=True)
class WorkspaceMutationResult:
    manifest: WorkspaceManifest
    state: WorkspaceStateRecord | None
    events: tuple[EventRecord, ...]
    changed: bool
```

- [ ] Add focused repository methods with exact DTO validation before first
  await and all checks repeated inside the write transaction:

```python
async def create_workspace_lifecycle(
    self, manifest: WorkspaceManifest
) -> WorkspaceMutationResult: ...

async def bind_workspace(
    self,
    manifest: WorkspaceManifest,
    *,
    expected_manifest_revision: str,
) -> WorkspaceMutationResult: ...

async def switch_workspace(
    self,
    session_id: str,
    workspace_id: str,
    *,
    expected_active_workspace_id: str | None,
    updated_at: datetime,
) -> WorkspaceMutationResult: ...

async def get_active_workspace(self, session_id: str) -> WorkspaceStateRecord | None: ...
async def list_workspace_state(
    self, session_id: str
) -> tuple[tuple[WorkspaceManifest, ...], WorkspaceStateRecord | None]: ...
```

Create verifies `created_by_run` belongs to `manifest.session_id`, not merely
that the Run exists. Bind never changes an inactive pointer and may therefore
return `state=None` for a schema-v2 manifest that has not been switched since
the v3 migration. Switch with expected active `None` creates initial state at
revision 1; later switches increment it. Idempotency compares canonical
identity/revision and active state, not a newly sampled `updated_at`: when the
revision is unchanged, return the already stored manifest/time. Exact no-ops
return `changed=False` and an empty `events` tuple.

- [ ] Use `EventStore._append_conn` inside the caller-owned transaction.
  Append exactly `workspace.created`, `workspace.bound`, or
  `workspace.updated`, with `RetentionClass.DURABLE`. Payloads contain only:
  session/workspace IDs, old/new revision where applicable, node count,
  instance ID, scene epoch, active flag, and state revision. Never include a
  full manifest, mirrors, HIP path, token, traceback, or arbitrary user data.

- [ ] Use the repository's established cancellation-deferred transaction
  behavior. Inject an event failure and a cancellation at persistence
  boundaries; prove state and event are both absent or both committed.

- [ ] Run:

```powershell
uv run --extra eval pytest tests/runtime/test_database.py tests/runtime/test_changeset_repository.py -q
```

Expected GREEN: all database/repository tests pass; accepted v1/v2 tests remain
unchanged.

### Task 5: Implement the provider-independent `WorkspaceService`

**Files:**

- Create: `eee_agent/changesets/workspace_service.py`
- Modify: `eee_agent/changesets/__init__.py`
- Create: `tests/runtime/test_workspace_service.py`

- [ ] Write RED tests with a deterministic async fake provider for every rule
  in design sections 8, 9, 11, and 12. Include no selection, incomplete mirrors,
  mixed workspaces/runs, invalid schema, wrong Run Session, duplicate IDs/paths,
  locked create, exact create/no-op/conflict, exact-set bind, subset/superset,
  legal path/parent/instance/epoch refresh, forbidden identity changes, locked
  bind, live switch, stale switch preserving the old pointer, concurrent CAS,
  switch no-op, explicit/active inspect, and all four statuses.

- [ ] Define the async protocol and bounded public records:

```python
class WorkspaceFactProvider(Protocol):
    async def inspect_selection(
        self, expected_scene_epoch: int | None
    ) -> WorkspaceInspectResult: ...
    async def inspect_manifest(
        self,
        manifest: WorkspaceManifest,
        expected_scene_epoch: int | None,
    ) -> WorkspaceInspectResult: ...

class WorkspaceHealth(StrEnum):
    HEALTHY = "Healthy"
    STALE = "Stale"
    CONFLICT = "Conflict"
    BRIDGE_UNAVAILABLE = "BridgeUnavailable"

@dataclass(frozen=True, slots=True)
class WorkspaceSummary:
    workspace_id: str
    revision: str
    node_count: int
    instance_id: str
    scene_epoch: int
    active: bool

@dataclass(frozen=True, slots=True)
class WorkspaceLifecycleSummary:
    workspace: WorkspaceSummary
    active_workspace_id: str | None
    state_revision: int | None
    changed: bool

@dataclass(frozen=True, slots=True)
class WorkspaceInspectionSummary:
    workspaces: tuple[WorkspaceSummary, ...]
    active_workspace_id: str | None
    target_manifest: WorkspaceManifest
    status: WorkspaceHealth
```

Every public record supplies `to_dict()` and only JSON-safe bounded fields.
`WorkspaceInspectionSummary` may serialize the stored target manifest because
the accepted inspect response explicitly returns it; it never serializes live
raw observations or ownership values beyond that trusted persisted manifest.

- [ ] Implement these methods:

```python
async def create(
    self, session_id: str, *, expected_scene_epoch: int | None
) -> WorkspaceLifecycleSummary: ...

async def bind(
    self,
    session_id: str,
    workspace_id: str,
    *,
    expected_manifest_revision: str,
    expected_scene_epoch: int | None,
) -> WorkspaceLifecycleSummary: ...

async def switch(
    self,
    session_id: str,
    workspace_id: str,
    *,
    expected_active_workspace_id: str | None,
    expected_scene_epoch: int | None,
) -> WorkspaceLifecycleSummary: ...

async def inspect(
    self,
    session_id: str,
    workspace_id: str | None,
    *,
    expected_scene_epoch: int | None,
) -> WorkspaceInspectionSummary: ...
```

- [ ] Create validation must first verify the Session without mutating state,
  then inspect current selection, reject empty/locked/partial/mixed/duplicate
  facts, require all six mirrors, require schema version 1, require one common
  workspace and creating Run, require that Run belongs to the Session, build
  `OwnedNodeRef`s from live facts, and call `WorkspaceManifest.build` with the
  exact observations as both roots and nodes.

- [ ] Bind must fail fast on stored Session/revision, inspect current selection,
  compare exact node-ID sets, preserve workspace/node/capability/role/schema/
  creating-Run identity, allow only path/parent/instance/epoch movement, build
  a fresh manifest, then let the repository compare the old revision again.
  Locks are observed but allowed for bind.

- [ ] Switch must inspect the stored manifest, not selection. Require current
  instance/epoch, exact node set, paths, parents, types, every mirror, and the
  derived manifest revision before repository CAS. Any failure leaves the
  previous active pointer intact.

- [ ] Inspect maps only ordinary missing discovery/connection/capability
  failures (`WorkspaceInspectionUnavailable`) to `BridgeUnavailable`. A healthy
  exact match returns `Healthy`; binding/path/parent/type/set/revision drift
  returns `Stale`; bounded duplicate/ambiguous live identity
  (`WorkspaceInspectionConflict`) or representable mixed/incomplete mirrors
  returns `Conflict`. Persistent corruption and malformed protocol data still
  raise structured errors. Inspect appends no event and changes no row.

- [ ] Use exact bounded errors from the design:

```text
workspace.not_found
workspace.no_selection
workspace.unowned_selection
workspace.mixed_selection
workspace.incomplete_selection
workspace.identity_conflict
workspace.session_mismatch
workspace.revision_conflict
workspace.active_conflict
workspace.locked_selection
bridge.capability_unavailable / bridge.stale_scene / bounded transport errors
```

- [ ] Run B2b-2 gates:

```powershell
uv run --extra eval pytest tests/runtime/test_database.py tests/runtime/test_changeset_repository.py tests/runtime/test_workspace_service.py -q
uv run --extra eval pytest tests/runtime/test_changeset_contracts.py tests/runtime/test_changeset_policy.py tests/runtime/test_changeset_service.py -q
uv run python -m compileall -q eee_agent tests
git diff --check
```

- [ ] Stage only B2b-2 authorized implementation files and commit once:

```text
feat: persist trusted workspace lifecycle
```

- [ ] Stop for independent Codex review. Do not activate public commands until
  migration, repository atomicity, and service validation are accepted.

## B2b-3 authorized implementation files

Modify:

- `eee_agent/runtime/protocol.py`
- `eee_agent/runtime/server.py`
- `eee_agent/runtime/service.py`
- `eee_agent/runtime/__main__.py`
- `tests/runtime/test_protocol.py`
- `tests/runtime/test_server.py`
- `tests/runtime/test_service.py`
- `tests/runtime/test_runtime_cli.py`
- `tests/runtime/test_runtime_e2e.py`
- `tests/runtime/runtime_process_fixture.py`
- `tests/runtime/changeset_houdini_smoke.py`

Modify `eee_agent/runtime/__init__.py` only if a stable B2b response record must
be exported. No other implementation/test file is authorized without a
concrete blocker and Codex approval. Documentation remains Codex-owned.

### Task 6: Activate the four exact Runtime commands

**Files:**

- Modify: `eee_agent/runtime/protocol.py`
- Modify: `eee_agent/runtime/server.py`
- Modify: `tests/runtime/test_protocol.py`
- Modify: `tests/runtime/test_server.py`

- [ ] Write RED protocol/server tests for exact shapes, duplicate keys,
  missing/unknown fields, IDs, lowercase digests, nullable values, epoch
  bounds, bool-vs-int rejection, message size, structured service errors, and
  the impossibility of uploading a manifest/node/path/mirror/metadata object.

- [ ] Move only these names from `DEFERRED_COMMAND_TYPES` to `COMMAND_TYPES`:

```python
"workspace.create"
"workspace.bind"
"workspace.switch"
"workspace.inspect"
```

- [ ] Add exact validators for `ses_<32 hex>`, `ws_<32 hex>`, lowercase
  64-character SHA-256, `int >= 1 | None`, and `workspace ID | None`.

- [ ] Route exact payloads only:

```python
# workspace.create
{"session_id": str, "expected_scene_epoch": int | None}

# workspace.bind
{"session_id": str, "workspace_id": str,
 "expected_manifest_revision": sha256,
 "expected_scene_epoch": int | None}

# workspace.switch
{"session_id": str, "workspace_id": str,
 "expected_active_workspace_id": str | None,
 "expected_scene_epoch": int | None}

# workspace.inspect
{"session_id": str, "workspace_id": str | None,
 "expected_scene_epoch": int | None}
```

Server methods call the corresponding service method and serialize only its
`to_dict()` result. The server does not import repositories, Bridge DTOs, or
HOM, and does not reinterpret workspace facts.

- [ ] Run:

```powershell
uv run --extra eval pytest tests/runtime/test_protocol.py tests/runtime/test_server.py -q
```

### Task 7: Wire `WorkspaceService`, production discovery, events, and CLI

**Files:**

- Modify: `eee_agent/runtime/service.py`
- Modify: `eee_agent/runtime/__main__.py`
- Modify: `tests/runtime/test_service.py`
- Modify: `tests/runtime/test_runtime_cli.py`

- [ ] Write RED service tests proving provider injection, production provider
  construction from `paths.state_dir`, exact delegation, post-commit event
  notification, callback isolation, no notification on no-op/failure,
  cancellation-safe commit, and no regression to ChangeSet approval wiring.

- [ ] Extend `RuntimeService.__init__` and `.open()` with an optional
  `workspace_fact_provider: WorkspaceFactProvider | None`. Production CLI
  constructs `BridgeWorkspaceFactProvider(paths.state_dir)` and passes it
  explicitly. Offline tests inject a deterministic fake; absence remains
  fail-closed for create/bind/switch and yields `BridgeUnavailable` for inspect.

- [ ] Construct one `WorkspaceService` over the accepted
  `ChangeSetRepository(database, events=self._events)` or a single shared
  repository instance. Do not create a second EventStore. Add Runtime methods
  named `create_workspace`, `bind_workspace`, `switch_workspace`, and
  `inspect_workspace` that delegate, then notify every returned committed
  event only after the repository call completes. No-op results notify none.

- [ ] Preserve Runtime startup/shutdown order. The provider owns no persistent
  connection, task, or close hook; every provider call closes its short-lived
  Bridge client. Runtime identity and Bridge identity remain separate even
  though both use `paths.state_dir`.

- [ ] Run:

```powershell
uv run --extra eval pytest tests/runtime/test_service.py tests/runtime/test_runtime_cli.py -q
```

### Task 8: Process, replay, security, and real Houdini acceptance

**Files:**

- Modify: `tests/runtime/test_runtime_e2e.py`
- Modify: `tests/runtime/runtime_process_fixture.py`
- Modify: `tests/runtime/changeset_houdini_smoke.py`

- [ ] Extend the process fixture with an injected/provider-controlled workspace
  fact seam or a paired loopback fake Bridge. Do not bypass the public Runtime
  WebSocket command parser/server/service/repository chain.

- [ ] Write RED/GREEN process tests for create event delivery, disconnect and
  replay, restart recovery of active state, exact idempotency, bind after a
  simulated rename/move, live switch, stale failure preserving the old active
  pointer, Session isolation, inspect `BridgeUnavailable`, provider failure,
  commit failure, and no half-state. Assert existing session snapshot wire
  compatibility is unchanged; workspace state is returned by
  `workspace.inspect`, not silently added to old snapshots.

- [ ] Extend the existing disposable Houdini smoke rather than creating a
  second harness. In a fresh unsaved root, create fixture nodes through the
  already accepted typed executor or isolated setup, verify all six mirrors,
  select exactly those nodes, exercise selection/manifest Bridge inspection
  and public Runtime create, rename/move then bind, switch, and inspect. Prove
  ordinary unmarked selection, partial mirrors, duplicate IDs, and stale
  binding fail closed.

- [ ] Before and after every B2b operation compare node/parameter/input/flag/
  user-data and HIP fingerprints. B2b itself must perform zero Houdini writes.
  Clean the disposable test root without saving.

- [ ] Run the B2b-3 focused gate:

```powershell
uv run --extra eval pytest tests/runtime/test_protocol.py tests/runtime/test_server.py tests/runtime/test_service.py tests/runtime/test_runtime_cli.py tests/runtime/test_runtime_e2e.py tests/runtime/test_workspace_service.py tests/runtime/test_workspace_bridge_contracts.py tests/runtime/test_workspace_bridge_inspector.py -q
```

- [ ] Run cross-slice regressions:

```powershell
uv run --extra eval pytest tests/runtime/test_changeset_contracts.py tests/runtime/test_changeset_policy.py tests/runtime/test_changeset_repository.py tests/runtime/test_changeset_service.py tests/runtime/test_changeset_bridge_contracts.py tests/runtime/test_changeset_bridge_preflight.py tests/runtime/test_changeset_executor.py tests/runtime/test_changeset_bridge_transport.py tests/runtime/test_houdini_bridge_client.py tests/runtime/test_houdini_bridge_queue.py tests/runtime/test_houdini_bridge_transport.py -q
```

- [ ] Stage only B2b-3 authorized implementation files and commit once:

```text
feat: expose trusted workspace lifecycle
```

- [ ] Stop for Codex independent review before running or accepting the real
  Houdini smoke.

## Final Codex acceptance

- [ ] Inspect all three implementation diffs against their exact parent commits
  and the independent checklist. Reproduce every reported RED/GREEN result.
- [ ] Scan changed code for generic dispatch and forbidden mutation surfaces:

```powershell
rg -n "eval\(|exec\(|subprocess|os\.system|createNode|destroy\(|setInput|setUserData|hipFile\.(save|load|clear)|hda\.|writeFile|saveToFile" eee_agent/houdini_bridge/workspaces.py eee_agent/houdini_bridge/workspace_provider.py houdini_side/workspace_inspector.py houdini_side/secure_bridge.py eee_agent/changesets/workspace_service.py eee_agent/changesets/repository.py eee_agent/runtime
```

Every hit must be pre-existing, test-only, or an explicit denial/assertion;
there may be no new reachable B2b mutation or arbitrary execution path.

- [ ] Run the complete offline gate from a clean worktree:

```powershell
uv sync --frozen --extra eval --python 3.11
uv run --extra eval pytest -q
uv lock --check
uv run python -m compileall -q eee_agent houdini_side tests
git diff --check
git status --short --branch
```

Expected: the complete suite passes with no new skip/xfail; lock and compile
checks succeed; only intentionally uncommitted Codex documentation is present.

- [ ] Locate the installed Houdini 21.0.440 `hython`, run the extended
  `tests/runtime/changeset_houdini_smoke.py` in a fresh process, and record the
  exact executable, arguments, exit code, assertions, and cleanup result.

- [ ] Verify changed-file scope, event payload bounds, no token/local absolute
  path leakage, no dependency/lock changes, no automatic adoption, no
  selection-as-authority, and no duplicated queue/worker.

- [ ] Only after every blocking item passes, update the B2b review result and
  handoff/status documentation in a separate Codex-owned docs commit. Do not
  push or merge without a new explicit user decision.
