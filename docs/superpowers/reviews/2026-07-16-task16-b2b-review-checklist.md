# Task 16-B2b Independent Review Checklist

- Design authority: `e135088` and
  `docs/superpowers/specs/2026-07-16-task16-b2b-workspace-lifecycle-design.md`
- Execution authority:
  `docs/superpowers/plans/2026-07-16-task16-b2b-workspace-lifecycle.md`
- Promotion: review B2b-1, B2b-2, and B2b-3 separately; a later slice cannot
  compensate for a blocking flaw in an earlier slice.
- Stop line: no Task 16-E, Task 17/18, UI, push, merge, rebase, accepted-history
  rewrite, or worktree deletion.

## B2b-1 changed-file scope

- [ ] B2b-1 creates/modifies only its eleven authorized implementation/test
  files.
- [ ] No Runtime DB/protocol/server/service/CLI, ChangeSet repository/service,
  executor, dependency/lock, agent, UI, or docs file is in the implementation
  commit.
- [ ] The commit has one focused parent and message
  `feat: inspect trusted houdini workspaces`.

## B2b-1 strict contract and capability

- [ ] Capability is exactly `workspace.v1`; operation is exactly
  `workspace.inspect` under unchanged `eee.bridge/1` hello/token rules.
- [ ] Successful capability lists stay exact, sorted, unique strings.
- [ ] Legacy/old Bridge remains valid for old operations but cannot receive a
  workspace frame; client capability failure sends zero inspect frames.
- [ ] Request envelope and payload reject unknown/missing fields, duplicate
  keys, wrong tags, wrong primitive types, bool epochs, invalid nulls, bad IDs,
  bad digests, invalid mode/manifest pairs, and oversized JSON.
- [ ] Response result/error are mutually exclusive and request ID is matched.
- [ ] DTOs are frozen/slotted, deeply immutable, bounded, canonical, and
  deterministic.
- [ ] Observations expose only path/type/parent/lock and nullable six-mirror
  fields; no HOM proxy, callable, arbitrary dict, token, raw user-data map, HIP
  content dump, or traceback crosses the Bridge.
- [ ] Duplicate IDs/paths and non-canonical order fail closed. Observed revision
  is recomputed from binding/mode/ordered observations and verified on parse.
- [ ] Lazy exports avoid the `WorkspaceManifest`/`SceneBinding` import cycle and
  importing bridge modules outside Houdini does not import `hou`.

## B2b-1 identity and no-write behavior

- [ ] Selection mode returns exactly selected nodes and never traverses
  descendants, inputs, outputs, siblings, or neighbors for adoption.
- [ ] Manifest mode ignores UI selection and resolves the complete stored set
  stable-ID-first; path is only corroborating evidence.
- [ ] A bounded scene scan detects copied duplicate in-scope stable IDs. Scan
  exhaustion returns a bounded failure rather than silently accepting an
  incomplete uniqueness proof.
- [ ] All six mirrors are read exactly:
  `eee.workspace_id`, `eee.node_id`, `eee.capability`, `eee.role`,
  `eee.schema_version`, `eee.created_by_run`.
- [ ] Empty/partial/malformed facts remain representable for service diagnosis,
  while duplicate/ambiguous facts fail at the appropriate Bridge boundary.
- [ ] Hard and soft lock states are observed without setting flags.
- [ ] Every HOM call occurs inside the accepted main-thread queue callable.
- [ ] Scene query, workspace inspection, ChangeSet preflight, Apply, and receipt
  share one bounded FIFO, capacity, deadline, queued/running cancellation, and
  shutdown behavior. There is no second queue, task, worker, or concurrent HOM
  path.
- [ ] Static review finds no new generic method dispatch, eval/exec, shell,
  filesystem, HIP/HDA/export, create/delete, parm/wire/flag/user-data mutation.
- [ ] Mutation-spy tests fingerprint nodes, parms, inputs, flags, user data, and
  HIP state before/after and prove zero writes.

## B2b-1 production provider

- [ ] Provider reads only accepted Bridge discovery/token handoff files from
  the supplied `state_dir`; it never uses Runtime bearer token, env token, CLI
  token, database token, or a hard-coded port.
- [ ] Each read opens and closes one discovered client; Houdini restart/port
  change is rediscovered and no persistent task/resource is leaked.
- [ ] Missing/closed/timeout/capability absence maps to
  `WorkspaceInspectionUnavailable`.
- [ ] Exact bounded live identity ambiguity maps to
  `WorkspaceInspectionConflict`; malformed authentication/fingerprint,
  malformed protocol/result, and corruption do not masquerade as ordinary
  offline or identity-conflict state.

## B2b-1 independent commands

```powershell
uv run --extra eval pytest tests/runtime/test_workspace_bridge_contracts.py tests/runtime/test_workspace_bridge_inspector.py tests/runtime/test_houdini_bridge_client.py tests/runtime/test_houdini_bridge_queue.py tests/runtime/test_houdini_bridge_transport.py tests/runtime/test_changeset_bridge_contracts.py tests/runtime/test_changeset_bridge_preflight.py tests/runtime/test_changeset_bridge_transport.py -q
uv run python -m compileall -q eee_agent houdini_side tests
git diff --check HEAD^ HEAD
git status --short --branch
```

- [ ] All focused tests pass with no new skip/xfail; compile/diff/scope checks
  pass. Only then may B2b-2 start.

## B2b-2 changed-file scope and migration

- [ ] B2b-2 changes only its seven authorized implementation/test files.
- [ ] No Bridge client/server/inspector, Runtime protocol/server/CLI, executor,
  dependency/lock, agent, UI, or docs file is in the implementation commit.
- [ ] Commit is exactly `feat: persist trusted workspace lifecycle` on the
  accepted B2b-1 parent.
- [ ] `SCHEMA_VERSION` is 3; accepted v1/v2 migration strings and checksums are
  byte-for-byte unchanged; v3 is appended exactly once.
- [ ] The parent `(workspace_id, session_id)` unique key exists before the
  composite FK is created.
- [ ] `session_workspace_state` has Session PK/cascade, non-null active ID,
  `state_revision >= 1`, UTC update time, and composite active workspace /
  Session FK.
- [ ] Fresh create, v1->v3, v2->v3, reopen, checksum mismatch, partial failure
  rollback, FK enforcement, cross-Session denial, and cascade tests pass.

## B2b-2 repository atomicity and concurrency

- [ ] Every public method validates exact DTO/ID/digest/time types before its
  first await and returns no live connection/row proxy.
- [ ] Every manifest read verifies stored canonical JSON and digest before
  decoding.
- [ ] Create verifies Session and that `created_by_run` belongs to that Session
  inside the write transaction.
- [ ] Create inserts manifest, initial active pointer, and `workspace.created`
  in one transaction. Exact active reuse is a no-op; contradictory reuse or
  another active pointer is a structured conflict and overwrites nothing.
- [ ] Create/bind idempotency compares canonical identity/revision, not a fresh
  `updated_at`; an unchanged revision returns the previously stored record/time
  without rewriting payload or emitting an event.
- [ ] Bind compares expected revision inside the transaction, changes only
  manifest storage, leaves an inactive workspace inactive, and couples
  `workspace.bound` atomically. Exact bind is a no-op. A migrated schema-v2
  workspace may legitimately have no active-state row until an explicit switch.
- [ ] Switch compares expected active ID inside the transaction, changes only
  the Session pointer/state revision, and couples `workspace.updated`
  atomically. Expected null plus no state creates revision 1; exact active
  target is a no-op.
- [ ] State revision increments only on pointer change; concurrent callers have
  one winner and deterministic conflict/no-op outcomes.
- [ ] Event payloads are bounded IDs/revisions/count/binding/active facts only;
  no manifest, mirror set, raw observations, HIP content, token, exception, or
  arbitrary user data appears.
- [ ] Event append failure and cancellation probes prove state/event cannot
  split. Notification is not a repository concern.
- [ ] Restart recovers active state and Session delete cascades it.

## B2b-2 service semantics

- [ ] `WorkspaceService` depends on the async protocol/DTOs/repository, not
  concrete sockets, WebSockets, `hou`, `rpyc`, legacy bridge, agent graph,
  shell, or filesystem.
- [ ] Create verifies Session before Bridge I/O and performs no persistence
  before all live facts pass.
- [ ] Empty selection -> `workspace.no_selection`; ordinary/no mirrors ->
  `workspace.unowned_selection`; partial mirrors ->
  `workspace.incomplete_selection`; mixed workspace/run facts, duplicates, or
  contradictions use the bounded design error taxonomy.
- [ ] Create requires all six mirrors, one workspace, one creating Run belonging
  to the Session, schema 1, no lock, unique IDs/paths, and supported bounded
  identity.
- [ ] Create uses selected observations exactly as both roots and nodes. No
  descendant/neighbor adoption and no generated identity occurs.
- [ ] Bind fast-checks Session/revision before inspection and rechecks revision
  at commit. Selected stable-ID set must equal the stored set exactly; subset
  and superset both fail.
- [ ] Bind allows only path/parent/instance/epoch refresh. It rejects changed
  node type/workspace/node ID/capability/role/schema/creating Run. Locks do not
  prevent a rebind.
- [ ] Switch ignores selection, inspects the entire stored manifest, requires
  exact live identity/binding/path/type/parent/revision, and performs CAS only
  after the proof. Failed/stale switch leaves old active state unchanged.
- [ ] Active/no-op switch still performs live verification and emits no event.
- [ ] Inspect null target resolves active; explicit target stays Session scoped;
  missing active/target is bounded and deterministic.
- [ ] `Healthy`, `Stale`, `Conflict`, and `BridgeUnavailable` mapping matches
  design exactly. Only ordinary `WorkspaceInspectionUnavailable` becomes
  offline; only bounded live ambiguity/representable mirror conflict becomes
  `Conflict`; corruption/malformed protocol remains an error.
- [ ] Inspect returns persisted summaries/target manifest but no raw live
  observation dump; it changes no manifest/state/event.
- [ ] Workspace state is never treated as write permission or a substitute for
  Task 16 policy/preflight/approval.

## B2b-2 independent commands

```powershell
uv run --extra eval pytest tests/runtime/test_database.py tests/runtime/test_changeset_repository.py tests/runtime/test_workspace_service.py -q
uv run --extra eval pytest tests/runtime/test_changeset_contracts.py tests/runtime/test_changeset_policy.py tests/runtime/test_changeset_service.py -q
uv run python -m compileall -q eee_agent tests
git diff --check HEAD^ HEAD
git status --short --branch
```

- [ ] All tests pass with no new skip/xfail and scope/compile/diff checks pass.
  Only then may B2b-3 activate commands.

## B2b-3 changed-file scope and protocol

- [ ] B2b-3 changes only the authorized Runtime and process/smoke test files.
- [ ] `runtime.__init__` changes, if any, are limited to a justified stable B2b
  record export. No other production/test/dependency/docs file is present.
- [ ] Commit is exactly `feat: expose trusted workspace lifecycle` on accepted
  B2b-2.
- [ ] Only the four workspace command names moved from deferred to active.
  Every other accepted/deferred/unknown command retains behavior.
- [ ] All four retain exact five-field `eee.runtime/1` envelopes and exact
  payload keys from design section 9.
- [ ] Validators enforce exact Session/workspace IDs, lowercase SHA-256,
  nullable exact epoch >=1, nullable workspace IDs, and reject bool/numbers/
  subclasses where exact primitives are required.
- [ ] Unknown/missing fields, duplicate keys, oversized messages, manifest,
  node/path list, mirror, role/capability, and arbitrary metadata uploads fail
  before service invocation.
- [ ] Server is a thin adapter: no repository/SQL/Bridge/HOM import and no live
  fact interpretation.

## B2b-3 Runtime wiring and events

- [ ] Runtime constructs exactly one workspace service over the accepted
  database/EventStore/repository pattern; there is no second event stream.
- [ ] Production CLI explicitly injects
  `BridgeWorkspaceFactProvider(paths.state_dir)`; Runtime and Bridge identities
  and bearer tokens remain separate.
- [ ] Tests can inject a deterministic async provider without opening sockets.
  Absent provider behavior stays fail-closed.
- [ ] Provider owns no persistent connection/task and does not alter Runtime
  startup/shutdown order or identity cleanup.
- [ ] Runtime create/bind/switch methods notify exactly returned committed
  events after commit; no-op/failure notifies none; callback failure does not
  corrupt the successful command.
- [ ] Event replay after disconnect/restart observes committed workspace events
  with Session sequence ordering and no duplicates.
- [ ] Existing ChangeSet approval provider/service and all Session/run/event
  behavior remain compatible.
- [ ] Existing snapshot wire shape is unchanged. Workspace state is obtained
  through `workspace.inspect`.

## B2b-3 process and failure behavior

- [ ] Public WebSocket tests traverse parser -> server -> service -> provider /
  repository; they do not call a private repository shortcut.
- [ ] Create/replay/restart, bind refresh, switch, inspect, idempotency,
  concurrent CAS, Session isolation, and offline inspect are covered.
- [ ] Provider failure before commit produces no row/event. Event/commit failure
  produces no half-state. Cancellation during the protected persistence region
  leaves state/event both absent or both committed.
- [ ] Stale/identity-conflict switch preserves previous active workspace and
  state revision.
- [ ] Bounded Runtime errors leak no raw Bridge/HOM exception, token, local path,
  unbounded observation, or traceback.

## Real Houdini 21.0.440 gate

- [ ] Smoke runs in a fresh process against the detected Houdini 21.0.440
  `hython`, disposable unsaved scene/root, and authenticated Bridge.
- [ ] Marked fixtures are created only through the accepted executor or isolated
  setup outside B2b operation under test; B2b never fabricates mirrors.
- [ ] Real selection inspection returns exact marked nodes; manifest inspection
  ignores unrelated selection; Runtime create/bind after rename/move/switch/
  inspect succeeds where specified.
- [ ] Ordinary unmarked, partial identity, duplicate stable ID, stale epoch, and
  stale manifest cases fail closed.
- [ ] Before/after fingerprints prove B2b changes no node, parm, input, flag,
  user data, HIP name/content, undo state, file, or HDA state.
- [ ] Disposable root is cleaned without saving and Bridge identity files are
  removed.

## B2b-3 and full independent commands

```powershell
uv run --extra eval pytest tests/runtime/test_protocol.py tests/runtime/test_server.py tests/runtime/test_service.py tests/runtime/test_runtime_cli.py tests/runtime/test_runtime_e2e.py tests/runtime/test_workspace_service.py tests/runtime/test_workspace_bridge_contracts.py tests/runtime/test_workspace_bridge_inspector.py -q
uv run --extra eval pytest tests/runtime/test_changeset_contracts.py tests/runtime/test_changeset_policy.py tests/runtime/test_changeset_repository.py tests/runtime/test_changeset_service.py tests/runtime/test_changeset_bridge_contracts.py tests/runtime/test_changeset_bridge_preflight.py tests/runtime/test_changeset_executor.py tests/runtime/test_changeset_bridge_transport.py tests/runtime/test_houdini_bridge_client.py tests/runtime/test_houdini_bridge_queue.py tests/runtime/test_houdini_bridge_transport.py -q
uv run --extra eval pytest -q
uv lock --check
uv run python -m compileall -q eee_agent houdini_side tests
git diff --check HEAD^ HEAD
git status --short --branch
```

- [ ] No new skip/xfail; focused and full suites pass; lock/compile/diff/scope
  checks pass; Houdini smoke passes and records its exact executable/result.

## Security and promotion blockers

Any item below blocks acceptance and promotion:

- capability bypass or compatibility downgrade;
- selection treated as authority or text intent overridden by selection;
- automatic adoption/marking of ordinary nodes;
- incomplete six-mirror validation or stable-ID ambiguity accepted;
- descendant/neighbor inclusion during public create;
- subset/superset bind accepted or forbidden identity refreshed;
- switch without live complete proof/CAS, or failed switch changing active state;
- workspace state treated as write permission;
- direct/concurrent/off-main-thread HOM access or a second queue/worker;
- any B2b Houdini mutation or generic execution/file/HIP/HDA/shell path;
- state/event split, notification before commit, duplicate no-op event, or
  cross-Session activation;
- malformed/corrupt identity hidden as `BridgeUnavailable`;
- token, traceback, arbitrary user data, local machine state, or unbounded fact
  leakage;
- unauthorized file, dependency/lock drift, new test failure/skip/xfail, or
  missing real-Houdini gate.

Only after every applicable checkbox passes may Codex write the acceptance
result and B2b handoff in a separate documentation commit. Do not push, merge,
or begin Task 16-E without a new explicit user decision.
