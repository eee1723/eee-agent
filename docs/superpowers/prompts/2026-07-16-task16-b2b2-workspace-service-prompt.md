# Task 16-B2b-2 Implementation Prompt

Use this prompt either for direct Codex implementation or, only when the user
explicitly selects it, an optional external coding worker. Execute only schema
v3, atomic Workspace repository operations, and the provider-independent
`WorkspaceService`. Do not expose Runtime commands or start B2b-3/Task 16-E.

## Prerequisite

Codex must identify an independently accepted B2b-1 commit in the worker launch
message. If that exact commit is absent, not the current parent, or not yet
accepted, stop before editing.

## Required reading

1. `CLAUDE.md`
2. `docs/superpowers/specs/2026-07-16-task16-b2b-workspace-lifecycle-design.md`
3. `docs/superpowers/plans/2026-07-16-task16-b2b-workspace-lifecycle.md`,
   Tasks 4-5
4. Accepted ChangeSet repository/service/event atomicity patterns
5. Accepted B2b-1 Workspace Bridge DTO/provider contracts and tests

Preserve every Codex-owned uncommitted document. Do not edit, stage, restore,
clean, or commit `docs/`.

## Authorized files

- Modify `eee_agent/runtime/migrations.py`
- Modify `eee_agent/changesets/repository.py`
- Create `eee_agent/changesets/workspace_service.py`
- Modify `eee_agent/changesets/__init__.py`
- Modify `tests/runtime/test_database.py`
- Modify `tests/runtime/test_changeset_repository.py`
- Create `tests/runtime/test_workspace_service.py`

No Bridge client/transport/inspector/server, Runtime protocol/server/CLI,
executor, agent, UI, dependency, lock, or documentation file is authorized.

## Schema and repository requirements

- Do not alter accepted migration v1 or v2 text/checksums.
- Append schema v3 with a unique parent key on
  `workspaces(workspace_id, session_id)` and `session_workspace_state` holding
  Session primary key, non-null active workspace, state revision >=1, UTC
  update time, Session cascade FK, and composite active-workspace/Session FK.
- Add atomic primitives for create+initial activate+event,
  compare-and-update bind+event, compare-and-switch+event, active lookup, and
  Session workspace/state listing.
- Validate exact DTOs before first await. Recheck Session, creating Run belongs
  Session, revision, active expectation, and foreign keys inside the write
  transaction. Verify canonical payload digest on every read.
- Use `EventStore._append_conn`; state and event commit or roll back together.
- Exact no-op create/bind/switch returns no event. Idempotency compares
  canonical revision/identity and active state, not a newly sampled
  `updated_at`; return the stored manifest/time on no-op. Contradictory
  workspace reuse never overwrites. Bind does not activate an inactive
  workspace. Switch CAS compares the caller's expected active workspace and
  creates state revision 1 when expected active is null and no state exists,
  then increments state revision only on later pointer changes. Bind may return
  no active state for a schema-v2 manifest that has not yet been switched.
- Event types are exactly `workspace.created`, `workspace.bound`, and
  `workspace.updated`; payloads contain only bounded IDs/revisions/count/
  binding/active-state facts.

## Service requirements

- Depend on an async `WorkspaceFactProvider` protocol, not a concrete Bridge.
- Create registers the exact current selection only after requiring complete
  six-mirror identity, one workspace, one creating Run belonging to the
  Session, no duplicates, schema 1, and no locks. Selected observations become
  both manifest roots and nodes; no traversal or adoption occurs.
- Bind loads the stored manifest and fast-checks Session/revision, then requires
  the exact selected stable-ID set. Only path, parent, Bridge instance, and
  scene epoch may refresh. Locks are allowed for bind. The repository compares
  the old revision again inside commit.
- Switch ignores selection, inspects the stored complete manifest, requires an
  exact live healthy match, then performs active CAS. Failure preserves the old
  active pointer.
- Inspect with null workspace targets the active workspace. Return bounded
  Session summaries, active ID, stored target manifest, and exactly one of
  `Healthy`, `Stale`, `Conflict`, `BridgeUnavailable`. Inspect never writes or
  appends an event.
- Map only `WorkspaceInspectionUnavailable` to ordinary
  `BridgeUnavailable`. Map the bounded `WorkspaceInspectionConflict` (and
  representable mixed/incomplete mirrors) to `Conflict` for read-only inspect,
  while create/bind/switch return the matching structured error. Do not hide
  corrupt persistence or malformed protocol.
- Use the design's exact `workspace.*` error taxonomy and leak no raw
  observation set, traceback, token, HIP content, or arbitrary user data.

## Test-first requirements

Begin with genuine RED tests. Cover migrations/checksums/rollback/reopen,
composite FKs/cascades, digest tampering, atomic event rollback, cancellation,
restart recovery, idempotency/conflicts/CAS/concurrency, every create/bind/
switch invariant, all four inspect statuses, no-op event suppression, and
accepted ChangeSet repository/service regressions.

## Verification

```powershell
uv run --extra eval pytest tests/runtime/test_database.py tests/runtime/test_changeset_repository.py tests/runtime/test_workspace_service.py -q
uv run --extra eval pytest tests/runtime/test_changeset_contracts.py tests/runtime/test_changeset_policy.py tests/runtime/test_changeset_service.py -q
uv run python -m compileall -q eee_agent tests
git diff --check
```

No new skip or xfail is allowed.

## Commit and handoff

Stage only authorized implementation/test files and commit once:

```text
feat: persist trusted workspace lifecycle
```

Return commit/parent, exact files, RED/GREEN evidence, schema/checksum evidence,
atomic rollback/cancellation evidence, service rule coverage, and concerns. Do
not push, merge, amend, clean docs, activate commands, or begin B2b-3.
